# feedback_analyzer.py — RLHF Engine
#
# Runs on a schedule (default every 6 hours).
# 1. Pulls top-10% and bottom-10% posts by engagement rate from publications_log
# 2. Sends them to Claude Sonnet with a meta-prompt: analyse patterns, generate
#    an improved system prompt
# 3. Saves the new prompt to system_prompts table (is_active=1) and deactivates
#    the previous one
#
# Can also be triggered manually:
#   python feedback_analyzer.py
#   python feedback_analyzer.py --tg_user_id=123456 --days=14

import argparse
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional

import requests

import config

# ── DB helper ─────────────────────────────────────────────────────────────────

@contextmanager
def _db(row_factory: bool = True):
    conn = sqlite3.connect(config.DB_NAME, timeout=15, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    if row_factory:
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Engagement rate ───────────────────────────────────────────────────────────

def _engagement_rate(row: sqlite3.Row) -> float:
    """
    Simple engagement rate: (likes + replies*2 + reposts*3) / max(views, 1)
    Replies and reposts are weighted higher than passive likes.
    """
    likes   = row["likes"]   or 0
    replies = row["replies_count"] or 0
    reposts = row["reposts"] or 0
    views   = row["views"]   or 1
    return (likes + replies * 2 + reposts * 3) / views


# ── Fetch posts for analysis ──────────────────────────────────────────────────

def fetch_posts_for_analysis(
    tg_user_id: str,
    days: int = 30,
    sample_percentile: float = 0.10,
) -> tuple[list[dict], list[dict]]:
    """
    Returns (top_posts, bottom_posts) — each a list of dicts with post data.

    top_posts    — top `sample_percentile` by engagement rate (winners)
    bottom_posts — bottom `sample_percentile` by engagement rate (losers)

    Only considers posts that:
      • have been published successfully
      • have a threads_post_id (so insights were fetchable)
      • have insights_at IS NOT NULL (metrics were actually pulled)
      • are within the last `days` days
    """
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    with _db() as conn:
        rows = conn.execute(
            """
            SELECT
                pl.id, pl.post_text, pl.post_type,
                pl.views, pl.likes, pl.replies_count, pl.reposts,
                pl.published_at, pl.prompt_id,
                a.name      AS account_name,
                a.ai_skill  AS ai_skill,
                a.post_language AS post_language
            FROM publications_log pl
            LEFT JOIN accounts a ON a.id = pl.account_id
            WHERE pl.tg_user_id   = ?
              AND pl.status       = 'published'
              AND pl.insights_at  IS NOT NULL
              AND pl.published_at >= ?
              AND pl.post_text    IS NOT NULL
            ORDER BY pl.published_at DESC
            LIMIT 200
            """,
            (tg_user_id, since),
        ).fetchall()

    if not rows:
        return [], []

    scored = sorted(rows, key=_engagement_rate)

    n_sample = max(1, int(len(scored) * sample_percentile))
    bottom   = scored[:n_sample]
    top      = scored[-n_sample:]

    def _to_dict(r: sqlite3.Row) -> dict:
        return {
            "post_text":    r["post_text"],
            "account":      r["account_name"] or "—",
            "ai_skill":     r["ai_skill"]     or "none",
            "language":     r["post_language"] or "ru",
            "views":        r["views"]         or 0,
            "likes":        r["likes"]         or 0,
            "replies":      r["replies_count"] or 0,
            "reposts":      r["reposts"]       or 0,
            "engagement_rate": round(_engagement_rate(r), 6),
            "published_at": r["published_at"]  or "",
        }

    return [_to_dict(r) for r in top], [_to_dict(r) for r in bottom]


# ── Build meta-prompt for Claude ─────────────────────────────────────────────

_META_SYSTEM = """\
You are a world-class social media strategist and copywriting expert \
specialising in viral content for the Threads platform.

Your task: analyse the performance data of published posts, identify \
what separates high-engagement posts from low-engagement ones, \
and produce a single improved system prompt that a language model \
should use to generate future posts.

Rules for the output system prompt you write:
• It must be a complete, self-contained system prompt (not a commentary).
• It must preserve the original language and persona settings.
• It must encode the winning patterns as concrete, actionable instructions.
• It must explicitly forbid the patterns found in the low-performing posts.
• Max 600 words.
• Write it in English regardless of the post language.
• Output ONLY the system prompt text — no preamble, no explanation, no markdown fences.\
"""

def _build_analysis_payload(
    top_posts: list[dict],
    bottom_posts: list[dict],
    account_context: dict,
) -> str:
    """Builds the user message that goes to Claude."""
    return json.dumps({
        "task": (
            "Analyse these Threads posts. "
            "Identify patterns in the HIGH-ENGAGEMENT group and the LOW-ENGAGEMENT group. "
            "Then write an improved system prompt that maximises engagement "
            "based on what you find."
        ),
        "account_context": account_context,
        "high_engagement_posts": top_posts,
        "low_engagement_posts":  bottom_posts,
    }, ensure_ascii=False, indent=2)


# ── Call Claude ───────────────────────────────────────────────────────────────

def _call_claude(user_message: str, max_retries: int = 3) -> Optional[str]:
    """Sends request to Anthropic API, returns response text or None."""
    headers = {
        "Content-Type":         "application/json",
        "x-api-key":            config.ANTHROPIC_API_KEY,
        "anthropic-version":    "2023-06-01",
    }
    body = {
        "model":      "claude-sonnet-4-6",
        "max_tokens": 1024,
        "system":     _META_SYSTEM,
        "messages":   [{"role": "user", "content": user_message}],
    }

    backoff = 5
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=body,
                timeout=60,
            )
            if resp.status_code == 200:
                return resp.json()["content"][0]["text"].strip()

            if resp.status_code in (429, 529):
                print(f"[analyzer] rate-limited, retrying in {backoff}s…")
                time.sleep(backoff)
                backoff *= 2
                continue

            print(f"[analyzer] API error {resp.status_code}: {resp.text[:200]}")
            return None

        except Exception as exc:
            print(f"[analyzer] request error (attempt {attempt}): {exc}")
            time.sleep(backoff)
            backoff *= 2

    return None


# ── Persist new prompt ────────────────────────────────────────────────────────

def _save_new_prompt(
    tg_user_id: str,
    prompt_text: str,
    account_id: Optional[int] = None,
    score: float = 0.0,
) -> str:
    """
    Deactivates all previous prompts for this user/account,
    inserts the new one with is_active=1, returns the new prompt id.
    """
    new_id = str(uuid.uuid4())

    with _db() as conn:
        # Deactivate old prompts for this scope
        if account_id is not None:
            conn.execute(
                "UPDATE system_prompts SET is_active = 0 "
                "WHERE tg_user_id = ? AND account_id = ?",
                (tg_user_id, account_id),
            )
        else:
            conn.execute(
                "UPDATE system_prompts SET is_active = 0 WHERE tg_user_id = ?",
                (tg_user_id,),
            )

        conn.execute(
            """
            INSERT INTO system_prompts
                (id, account_id, tg_user_id, prompt_text, is_active, score, generated_by)
            VALUES (?, ?, ?, ?, 1, ?, 'analyzer')
            """,
            (new_id, account_id, tg_user_id, prompt_text, round(score, 6)),
        )

    return new_id


# ── Average engagement score helper ──────────────────────────────────────────

def _avg_engagement(posts: list[dict]) -> float:
    if not posts:
        return 0.0
    return sum(p["engagement_rate"] for p in posts) / len(posts)


# ── Main analysis entry point ─────────────────────────────────────────────────

def run_analysis_for_user(
    tg_user_id: str,
    days: int = 30,
    account_id: Optional[int] = None,
) -> Optional[str]:
    """
    Full pipeline for one user:
      fetch → build payload → call Claude → save prompt
    Returns the new prompt_id on success, None if skipped/failed.
    """
    print(f"[analyzer] starting analysis for user {tg_user_id} (last {days} days)")

    top_posts, bottom_posts = fetch_posts_for_analysis(tg_user_id, days=days)

    if not top_posts and not bottom_posts:
        print(f"[analyzer] no posts with insights found for {tg_user_id} — skipping")
        return None

    print(
        f"[analyzer] {len(top_posts)} top posts, "
        f"{len(bottom_posts)} bottom posts → sending to Claude"
    )

    account_context = {
        "tg_user_id":      tg_user_id,
        "analysis_period": f"last {days} days",
        "top_avg_er":      round(_avg_engagement(top_posts), 4),
        "bottom_avg_er":   round(_avg_engagement(bottom_posts), 4),
    }

    payload = _build_analysis_payload(top_posts, bottom_posts, account_context)
    new_prompt = _call_claude(payload)

    if not new_prompt:
        print(f"[analyzer] Claude returned no result for {tg_user_id}")
        return None

    score = _avg_engagement(top_posts)
    prompt_id = _save_new_prompt(tg_user_id, new_prompt, account_id=account_id, score=score)

    print(
        f"[analyzer] ✅ new prompt saved — id={prompt_id} "
        f"score={score:.4f} len={len(new_prompt)} chars"
    )
    return prompt_id


def run_analysis_for_all_users(days: int = 30) -> int:
    """Runs analysis for every user that has published posts with insights."""
    with _db() as conn:
        users = conn.execute(
            """
            SELECT DISTINCT tg_user_id FROM publications_log
            WHERE status = 'published' AND insights_at IS NOT NULL
            """
        ).fetchall()

    total = 0
    for row in users:
        uid = row["tg_user_id"]
        if not uid:
            continue
        try:
            result = run_analysis_for_user(uid, days=days)
            if result:
                total += 1
        except Exception as exc:
            print(f"[analyzer] error for {uid}: {exc}")

    print(f"[analyzer] done — {total}/{len(users)} users updated")
    return total


# ── Background scheduler loop ─────────────────────────────────────────────────

def analyzer_scheduler_loop(interval_seconds: int = 21600) -> None:
    """
    Infinite daemon loop — runs analysis for all users every `interval_seconds`
    (default 6 hours).  Register this in web_app.py lifespan just like the
    insights scheduler.
    """
    print(f"[analyzer] scheduler started — interval {interval_seconds // 3600}h")
    while True:
        time.sleep(interval_seconds)
        print("[analyzer] running scheduled analysis…")
        try:
            run_analysis_for_all_users()
        except Exception as exc:
            print(f"[analyzer] scheduled run error: {exc}")


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RLHF Feedback Analyzer")
    parser.add_argument("--tg_user_id", default=None, help="Run for a single user")
    parser.add_argument("--days",       default=30,   type=int, help="Lookback window in days")
    args = parser.parse_args()

    if args.tg_user_id:
        run_analysis_for_user(args.tg_user_id, days=args.days)
    else:
        run_analysis_for_all_users(days=args.days)
