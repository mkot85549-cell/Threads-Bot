#   • Uses new Claude-based AIService (ai_service.py rewrite)
#   • Reads acc['ai_skill'] and acc['post_language'] from DB and passes them
#     to generate_text() so persona + language are always applied
#   • generate_text() now accepts keyword args ai_skill / post_language
#   • All other logic (threading, stop event, WAL DB) unchanged

import time
import random
import threading
import config
from ai_service import AIService
from social_service import ThreadsFarmService
from db_utils import get_db
from analytics_service import log_publication
from text_guard import clean_generated, has_content

_stop_event = threading.Event()

_threads_lock = threading.Lock()
active_account_threads: dict[int, threading.Thread] = {}

_ANTI_BAN_INSTRUCTION = (
    "HARD RULE: Never use words like 'заработок', 'деньги', 'схема', 'быстро', "
    "'легко', 'казино', 'пассивный доход'. No info-marketer language. "
    "Write like an authentic expert personal brand: productivity, remote work, "
    "personal branding, business mindset, freedom, lifestyle, career growth."
)

# ── One-liner posts (Format B from post_style_viral_threads.md) ──────────────
# Dynamically generates short one-liner punchy posts with a variable probability.
# For each generation, the threshold is randomized between [10%, 60%], ensuring
# organic variety in account post lengths rather than a static percentage.
_ONELINER_CHANCE_MIN = 0.10   # 10%
_ONELINER_CHANCE_MAX = 0.60   # 60%

_ONELINER_INSTRUCTION = (
    "FORMAT — ONE-LINER: Write the post in exactly ONE sentence on a SINGLE line. "
    "No line breaks. Maximum 100 characters. Punchy, complete thought "
    "(paradox, contrast, or sharp observation)."
)
_MULTILINE_INSTRUCTION = (
    "FORMAT — EXPANDED POST: 3–6 short lines. First line is a hook "
    "(under 8 words), followed by development, and ending with an unexpected punchline/conclusion."
)


def _pick_post_format() -> tuple[str, bool]:
    """
    Randomly decides whether the post will be a one-liner punchline or expanded format.

    Returns (format_instruction, is_oneliner).
    """
    chance = random.uniform(_ONELINER_CHANCE_MIN, _ONELINER_CHANCE_MAX)
    is_oneliner = random.random() < chance
    instruction = _ONELINER_INSTRUCTION if is_oneliner else _MULTILINE_INSTRUCTION
    return instruction, is_oneliner

# Random angles to force variety in every post
_POST_ANGLES = [
    "Write from the angle of a personal mistake or lesson learned.",
    "Write as a hot controversial take that challenges common beliefs.",
    "Write as a short story with a surprising twist at the end.",
    "Write as a list of brutal honest truths most people ignore.",
    "Write as a direct question that makes the reader stop and think.",
    "Write as a comparison: what most people do vs what winners do.",
    "Write as a confession — something you used to believe but don't anymore.",
    "Write as a prediction about where things are headed in 1 year.",
    "Write as a myth-busting post — one common belief that is completely wrong.",
    "Write as a 'nobody talks about this but...' revelation.",
    "Write as a micro case study: one specific situation, one specific result.",
    "Write as an open letter to your past self 3 years ago.",
    "Write as a contrarian: everyone says X, but actually Y is true.",
    "Write as a behind-the-scenes look at how something really works.",
    "Write starting with a shocking statistic or unexpected fact.",
]


def worker_for_single_account(account_id: int):
    """Isolated posting loop for ONE account."""
    print(f"⚙️  [Farm] Worker started for account ID: {account_id}")
    ai = AIService()

    while not _stop_event.is_set():
        try:
            with get_db() as conn:
                cursor = conn.execute("""
                    SELECT accounts.*, users.farm_enabled
                    FROM accounts
                    JOIN users ON accounts.tg_user_id = users.tg_user_id
                    WHERE accounts.id = ?
                """, (account_id,))
                acc = cursor.fetchone()
        except Exception as e:
            print(f"❌ [DB] Error reading account {account_id}: {e}")
            _stop_event.wait(timeout=30)
            continue

        if not acc or acc['is_active'] == 0 or acc['farm_enabled'] == 0:
            print(f"🛑 [Farm] Account ID {account_id} disabled. Stopping worker.")
            break

        name           = acc['name']
        ai_skill       = acc['ai_skill']      or None
        post_language  = acc['post_language'] or 'ru'
        ai_temperature = float(acc['ai_temperature'] if acc['ai_temperature'] is not None else 0.7)
        tg_user_id     = acc['tg_user_id']
        social = ThreadsFarmService(
            user_id=acc['threads_user_id'],
            access_token=acc['access_token'],
            proxy=acc['proxy'],
        )
        post_text = ""
        is_oneliner = False

        try:
            if acc['post_mode'] == 'prompts':
                all_prompts = [
                    acc['prompt_1'], acc['prompt_2'], acc['prompt_3'],
                    acc['prompt_4'], acc['prompt_5'],
                ]
                available = [p for p in all_prompts if p and p.strip()]
                selected  = random.choice(available) if available else "You are an expert in your niche."
                angle     = random.choice(_POST_ANGLES)
                fmt_instruction, is_oneliner = _pick_post_format()

                base_instruction = (
                    f"You are a genius copywriter. Write exactly ONE post for Threads.\n"
                    f"INSTRUCTION: {selected}\n"
                    f"ANGLE — use this specific angle for this post: {angle}\n"
                    f"{fmt_instruction}\n"
                    f"{_ANTI_BAN_INSTRUCTION}\n"
                    f"OUTPUT ONLY the post text (up to 350 chars). No quotes."
                )
                post_text = ai.generate_text(
                    "Write a post following the instruction",
                    base_instruction,
                    ai_skill=ai_skill,
                    post_language=post_language,
                    temperature=ai_temperature,
                    tg_user_id=tg_user_id,
                )

            else:  # ai_adapt mode
                safe_target = acc['target_theme'] or "Business & remote work"
                safe_mini   = acc['mini_prompt']  or "Write naturally, like a real person."
                angle       = random.choice(_POST_ANGLES)
                fmt_instruction, is_oneliner = _pick_post_format()

                base_instruction = (
                    f"You are a trendsetter and CMO. Write a viral, unique post for Threads.\n"
                    f"YOUR NICHE: {safe_target}\n"
                    f"STYLE NOTE: {safe_mini}\n"
                    f"ANGLE — use this specific angle for this post: {angle}\n"
                    f"{fmt_instruction}\n"
                    f"{_ANTI_BAN_INSTRUCTION}\n"
                    f"OUTPUT ONLY the post text (strictly under 350 chars). No quotes."
                )
                print(f"🧠 [{name}] Topic: {safe_target} | Angle: {angle[:35]}… | "
                      f"{'однострочник' if is_oneliner else 'развёрнутый'}")
                post_text = ai.generate_text(
                    f"Viral post about {safe_target}",
                    base_instruction,
                    ai_skill=ai_skill,
                    post_language=post_language,
                    temperature=ai_temperature,
                    tg_user_id=tg_user_id,
                )

        except Exception as e:
            print(f"❌ [{name}] Generation error: {e}")
            _stop_event.wait(timeout=60)
            continue
        post_text = clean_generated(post_text)
        min_len = 10 if is_oneliner else 20
        if not has_content(post_text, min_len):
            print(f"⚠️ [{name}] Generation returned too little text. Retrying.")
            _stop_event.wait(timeout=60)
            continue

        # Ban-word filter
        lower_text = post_text.lower()
        if any(word in lower_text for word in config.BAN_WORDS):
            print(f"🛑 [{name}] Ban word detected. Regenerating.")
            _stop_event.wait(timeout=10)
            continue

        # For one-liners, do not append custom_reply to preserve single-line punchiness
        final_text = f"{post_text}\n\n{acc['custom_reply']}" if (acc['custom_reply'] and not is_oneliner) else post_text
        if not has_content(final_text, min_len):
            _stop_event.wait(timeout=60)
            continue

        MAX_LENGTH = 500
        if len(final_text) > MAX_LENGTH:
            truncated  = final_text[:MAX_LENGTH - 3]
            last_space = truncated.rfind(' ')
            if last_space > MAX_LENGTH - 100:
                truncated = truncated[:last_space]
            final_text = truncated + "..."

        print(f"📝 [{name}] Post text: {final_text[:80]}…")

        try:
            result = social.publish_post(text=final_text)

            # Extract Threads post ID from result for stats
            thr_post_id = None
            if "✅" in result and "(ID:" in result:
                try:
                    thr_post_id = result.split("(ID: ")[1].rstrip(")")
                except Exception:
                    pass

            # Log publication for analytics
            log_publication(
                account_id=account_id,
                tg_user_id=acc["tg_user_id"],
                threads_post_id=thr_post_id,
                post_text=final_text,
                post_type="post",
                status="published" if "✅" in result else "failed",
            )
            print(f"📊 [{name}] Publish result: {result}")
        except Exception as e:
            print(f"❌ [{name}] Publish error: {e}")

        del_min    = acc['pub_delay_min'] or 15
        del_max    = acc['pub_delay_max'] or 45
        sleep_time = random.randint(del_min * 60, del_max * 60)
        print(f"⏳ [{name}] Sleeping {sleep_time // 60} min (range: {del_min}–{del_max} min).")
        _stop_event.wait(timeout=sleep_time)

    print(f"🔴 [Farm] Worker for account ID {account_id} finished.")


def run_farm_loop():
    """Main dispatcher: starts per-account worker threads."""
    global active_account_threads
    print("🚀 FARM DISPATCHER STARTED…")

    while not _stop_event.is_set():
        try:
            with get_db() as conn:
                cursor = conn.execute("""
                    SELECT accounts.id
                    FROM accounts
                    JOIN users ON accounts.tg_user_id = users.tg_user_id
                    WHERE accounts.is_active = 1 AND users.farm_enabled = 1
                """)
                active_db_ids = [row[0] for row in cursor.fetchall()]
        except Exception as e:
            print(f"❌ [Dispatcher] DB error: {e}")
            _stop_event.wait(timeout=10)
            continue

        with _threads_lock:
            for acc_id in active_db_ids:
                if acc_id not in active_account_threads or not active_account_threads[acc_id].is_alive():
                    th = threading.Thread(
                        target=worker_for_single_account,
                        args=(acc_id,),
                        daemon=True,
                        name=f"farm-account-{acc_id}",
                    )
                    active_account_threads[acc_id] = th
                    th.start()

            dead = [aid for aid, th in active_account_threads.items() if not th.is_alive()]
            for aid in dead:
                del active_account_threads[aid]

        _stop_event.wait(timeout=15)


def stop_farm():
    """Signal all workers to stop gracefully."""
    _stop_event.set()
    print("🛑 Stop signal sent. Workers will finish their current iteration.")


# Alias so web_app.py can reference farm_manager.stop_event directly
stop_event = _stop_event