# ai_service.py — Claude API + rate-limit resilience (fixed)
from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

import requests
import config


# ── Model ─────────────────────────────────────────────────────────────────────
# Override with ANTHROPIC_MODEL in .env. Check current IDs: https://docs.claude.com
MODEL_SONNET = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")


# ── Viral style guide ─────────────────────────────────────────────────────────
STYLE_GUIDE_PATH = Path(__file__).parent / "post_style_viral_threads.md"
_style_guide_cache: str | None = None


def load_style_guide() -> str:
    """Reads and caches the viral style guide. Returns '' if the file is absent."""
    global _style_guide_cache
    if _style_guide_cache is None:
        try:
            _style_guide_cache = STYLE_GUIDE_PATH.read_text(encoding="utf-8").strip()
            print(f"📖 [Style] Loaded style guide: {STYLE_GUIDE_PATH.name} "
                  f"({len(_style_guide_cache)} chars)")
        except FileNotFoundError:
            print(f"⚠️ [Style] File {STYLE_GUIDE_PATH.name} not found — generating without style guide.")
            _style_guide_cache = ""
        except Exception as e:
            print(f"⚠️ [Style] Failed to read {STYLE_GUIDE_PATH.name}: {e}")
            _style_guide_cache = ""
    return _style_guide_cache


# ── Concurrency guard ─────────────────────────────────────────────────────────
# Sync callers run each request in their own event loop (asyncio.run), so an
# asyncio.Semaphore cannot be shared between them. A thread-level semaphore
# around the actual HTTP call works for every caller, regardless of loop.
MAX_CONCURRENT = 8
_http_slots = threading.BoundedSemaphore(MAX_CONCURRENT)

MAX_RETRIES = 3
BASE_BACKOFF = 5   # seconds — doubles on each retry (5 → 10 → 20)


# ── Skill → System-Prompt mapping ─────────────────────────────────────────────
SKILL_PROMPTS: dict[str, str] = {
    "neutral": (
        "You are an experienced social-media writer. Your tone is natural, clear and "
        "conversational. You write short, engaging posts with a strong first line, "
        "concrete details instead of filler, and no clichés."
    ),
    "crypto_bro": (
        "You are a cynical, hyper-confident crypto degen who has made and lost fortunes "
        "multiple times. Your writing is aggressive, laced with insider slang, and radiates "
        "toxic positivity. You never hedge — everything is either 'going to zero' or "
        "'going to the moon'. Short punchy sentences. No sugarcoating."
    ),
    "analyst": (
        "You are a data-driven market analyst with 15 years on Wall Street. "
        "Your tone is dry, authoritative, and evidence-based. You cite numbers, ratios, "
        "and trends. You never use hype words. Your posts read like excerpts from a "
        "premium research report — concise, factual, slightly intimidating."
    ),
    "provocateur": (
        "You are a contrarian provocateur who thrives on sparking debates. "
        "You take the unpopular opinion, challenge obvious truths, and write hot-takes "
        "that make people feel compelled to respond. Your posts end with a rhetorical "
        "question or a bold statement that splits the audience."
    ),
    "storyteller": (
        "You are an emotionally intelligent storyteller. You write in the first person, "
        "sharing vulnerable personal moments, failures, and breakthroughs. "
        "Your posts follow a mini hero's-journey arc: setup → conflict → insight. "
        "You write like you're texting a close friend who also happens to be a millionaire."
    ),
    "hustler": (
        "You are a results-obsessed entrepreneur and sales closer. Every post ends with a "
        "clear, irresistible CTA. You speak directly to pain points, create urgency, and "
        "frame everything as an opportunity the reader is missing RIGHT NOW. "
        "High energy, direct, conversion-focused."
    ),
    "philosopher": (
        "You are a modern Stoic philosopher and slow-living advocate. "
        "Your writing is contemplative, rich with metaphor, and invites the reader to pause. "
        "You draw wisdom from ancient philosophy and apply it to modern hustle culture "
        "— gently subverting it. Calm, deep, unhurried."
    ),
}

# Used when ai_skill is empty/unknown (was crypto_bro — too aggressive as a default)
_DEFAULT_SKILL = "neutral"

_LANGUAGE_INSTRUCTIONS: dict[str, str] = {
    "ru": "ВАЖНО: Весь текст пиши ТОЛЬКО на русском языке.",
    "en": "IMPORTANT: Write ALL output in English only.",
    "uk": "ВАЖЛИВО: Весь текст пиши ТІЛЬКИ українською мовою.",
}
_DEFAULT_LANGUAGE_INSTRUCTION = _LANGUAGE_INSTRUCTIONS["ru"]

_ANTI_BAN_RULE = (
    "⚠️ HARD RULE: Never use words like 'earnings', 'scheme', 'casino', "
    "'quick money', 'passive income', 'guaranteed profit'. "
    "Write like an authentic personal-brand expert, NOT an info-marketer."
)


# ── Anthropic API helpers ─────────────────────────────────────────────────────
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


def _build_headers() -> dict:
    return {
        "Content-Type":      "application/json",
        "x-api-key":         config.ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
    }


def _post(payload: dict) -> requests.Response:
    """Blocking HTTP call, limited to MAX_CONCURRENT parallel requests."""
    with _http_slots:
        return requests.post(
            ANTHROPIC_API_URL,
            headers=_build_headers(),
            json=payload,
            timeout=40,
        )


def _retry_after_seconds(response: requests.Response, fallback: float) -> float:
    try:
        return float(response.headers.get("retry-after", fallback))
    except (TypeError, ValueError):
        return fallback


def _extract_text(data: dict) -> str:
    """Joins all text blocks (the first block is not guaranteed to be text)."""
    blocks = data.get("content") or []
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


async def _call_claude_async(
    *,
    model: str,
    system: str,
    user_message: str,
    max_tokens: int = 400,
    temperature: float = 0.7,
) -> str:
    """
    Core async Claude call with exponential-backoff retry.

    Returns the generated text, or "" on failure. Never raises — errors are
    logged so one account failure cannot crash the whole batch.
    Callers MUST treat "" as "nothing to publish".
    """
    if not model or not model.strip():
        print("❌ Claude API: model name is empty (check ANTHROPIC_MODEL).")
        return ""

    payload = {
        "model":       model,
        "max_tokens":  max_tokens,
        "temperature": max(0.0, min(1.0, temperature)),
        "system":      system,
        "messages":    [{"role": "user", "content": user_message}],
    }

    loop = asyncio.get_running_loop()

    for attempt in range(1, MAX_RETRIES + 1):
        is_last = attempt == MAX_RETRIES
        try:
            response = await loop.run_in_executor(None, _post, payload)
        except requests.exceptions.Timeout:
            wait = BASE_BACKOFF * attempt
            print(f"⏰ Claude API: timeout (attempt {attempt}/{MAX_RETRIES}).")
            if not is_last:
                await asyncio.sleep(wait)
            continue
        except Exception as exc:
            print(f"❌ Claude API: request error (attempt {attempt}/{MAX_RETRIES}): {exc}")
            if not is_last:
                await asyncio.sleep(BASE_BACKOFF * attempt)
            continue

        status = response.status_code

        if status == 429:
            wait = _retry_after_seconds(response, BASE_BACKOFF * attempt) + attempt * 2
            print(f"🚦 Claude API: rate limit (attempt {attempt}/{MAX_RETRIES}).")
            if not is_last:
                await asyncio.sleep(wait)
            continue

        if status >= 500:
            wait = BASE_BACKOFF * (2 ** (attempt - 1))
            print(f"⚠️  Claude API: server error {status} (attempt {attempt}/{MAX_RETRIES}).")
            if not is_last:
                await asyncio.sleep(wait)
            continue

        if status >= 400:
            # Client errors (bad model, bad key, bad params) will not fix themselves — no retry.
            print(f"❌ Claude API {status}: {response.text[:300]}")
            return ""

        try:
            return _extract_text(response.json())
        except Exception as exc:
            print(f"❌ Claude API: bad response body: {exc}")
            return ""

    print("❌ Claude API: all retries exhausted. Returning empty string.")
    return ""


# ── Active system-prompt loader ───────────────────────────────────────────────

def get_active_system_prompt(tg_user_id: str) -> Optional[str]:
    """
    Returns the active AI-generated system prompt for this user,
    or None if no analyzer prompt exists yet.
    """
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT prompt_text FROM system_prompts
            WHERE tg_user_id = ? AND is_active = 1
            ORDER BY created_at DESC LIMIT 1
            """,
            (tg_user_id,),
        ).fetchone()
        return row["prompt_text"] if row else None
    except Exception as exc:
        print(f"[ai_service] get_active_system_prompt error: {exc}")
        return None
    finally:
        if conn is not None:
            conn.close()


# ── Public interface ──────────────────────────────────────────────────────────

class AIService:
    """
    Sync callers (farm_manager, outreach_bot) use generate_text();
    async callers use generate_text_async(). Both build the prompt identically.
    """

    @staticmethod
    def build_system_prompt(
        *,
        base_instruction:    str,
        ai_skill:            str | None = None,
        post_language:       str | None = None,
        anti_ban:            bool = True,
        include_style_guide: bool = True,
        tg_user_id:          str | None = None,
    ) -> str:
        """
        Builds the system prompt.

        If `tg_user_id` has an active analyzer prompt in system_prompts, it
        replaces the static skill persona. Style guide, language instruction and
        anti-ban rules are always appended so a dynamic prompt cannot override them.
        """
        lang_note = _LANGUAGE_INSTRUCTIONS.get(
            post_language or "ru", _DEFAULT_LANGUAGE_INSTRUCTION
        )

        persona = None
        if tg_user_id:
            persona = get_active_system_prompt(tg_user_id)
        if not persona:
            skill_key = ai_skill or _DEFAULT_SKILL
            persona = SKILL_PROMPTS.get(skill_key, SKILL_PROMPTS[_DEFAULT_SKILL])

        parts = [persona]

        if include_style_guide:
            guide = load_style_guide()
            if guide:
                parts.append(
                    "Ниже — обязательное руководство по написанию постов для Threads. "
                    "СТРОГО следуй ему (крючки, форматы, тон, запреты):\n\n" + guide
                )

        parts.extend([base_instruction, lang_note])

        if anti_ban:
            parts.append(_ANTI_BAN_RULE)

        return "\n\n".join(p.strip() for p in parts if p and p.strip())

    async def generate_text_async(
        self,
        *,
        topic:            str,
        base_instruction: str,
        ai_skill:         str | None = None,
        post_language:    str | None = None,
        max_tokens:       int = 400,
        temperature:      float = 0.7,
        tg_user_id:       str | None = None,
    ) -> str:
        """Async post generator. Preferred when called from async context."""
        # build_system_prompt touches SQLite / disk — keep it off the event loop
        system = await asyncio.to_thread(
            self.build_system_prompt,
            base_instruction=base_instruction,
            ai_skill=ai_skill,
            post_language=post_language,
            tg_user_id=tg_user_id,
        )
        return await _call_claude_async(
            model=MODEL_SONNET,
            system=system,
            user_message=topic,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def generate_text(
        self,
        topic: str,
        system_prompt: str,
        *,
        ai_skill:      str | None = None,
        post_language: str | None = None,
        temperature:   float = 0.7,
        tg_user_id:    str | None = None,
        max_tokens:    int = 400,
    ) -> str:
        """Synchronous wrapper for thread-based callers."""
        system = self.build_system_prompt(
            base_instruction=system_prompt,
            ai_skill=ai_skill,
            post_language=post_language,
            tg_user_id=tg_user_id,
        )
        return _run_sync(_call_claude_async(
            model=MODEL_SONNET,
            system=system,
            user_message=topic,
            max_tokens=max_tokens,
            temperature=temperature,
        ))


# ── Event-loop helper for sync callers ────────────────────────────────────────

def _run_sync(coro) -> str:
    """
    Runs a coroutine from synchronous code.
    No running loop in this thread → asyncio.run().
    Running loop (e.g. called from async code) → run in a separate thread.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()