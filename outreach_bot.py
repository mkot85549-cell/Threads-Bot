#   • Reads acc['ai_skill'] and acc['post_language'] and passes to generate_text()
#   • All other logic unchanged (threading, stop event, WR-05 fix, etc.)

import random
import threading
import config
from ai_service import AIService
from social_service import ThreadsFarmService
from db_utils import get_db
from analytics_service import log_publication
from text_guard import clean_generated, has_content

_stop_event   = threading.Event()
stop_event    = _stop_event          # alias for web_app.py

_outreach_lock    = threading.Lock()
_outreach_threads: dict[int, threading.Thread] = {}

_ANTI_BAN_INSTRUCTION = (
    "RULES: Write natively, no direct advertising. "
    "Never use: 'earnings', 'scheme', 'casino', 'income', 'fast money'. "
    "Text must read like an expert comment from a real human."
)


def worker_for_outreach_account(account_id: int):
    """Isolated mass-commenting thread for one account."""
    print(f"📡 [Outreach] Started for account ID: {account_id}")
    ai = AIService()

    while not _stop_event.is_set():
        try:
            with get_db() as conn:
                cursor = conn.execute("""
                    SELECT accounts.*, users.listener_enabled
                    FROM accounts
                    JOIN users ON accounts.tg_user_id = users.tg_user_id
                    WHERE accounts.id = ?
                """, (account_id,))
                acc = cursor.fetchone()
        except Exception as e:
            print(f"❌ [DB] Error reading account {account_id}: {e}")
            _stop_event.wait(timeout=30)
            continue

        if not acc or acc['is_active'] == 0 or acc['listener_enabled'] == 0:
            print(f"🛑 [Outreach] Account ID {account_id} disabled. Exiting.")
            break

        name          = acc['name']
        ai_skill      = acc['ai_skill'] or None
        post_language = acc['post_language'] or 'ru'

        keywords_str     = acc['outreach_keywords'] or acc['source_themes'] or "crypto, remote work, business"
        keywords_list    = [k.strip() for k in keywords_str.split(',') if k.strip()]
        selected_keyword = random.choice(keywords_list)

        social = ThreadsFarmService(
            user_id=acc['threads_user_id'],
            access_token=acc['access_token'],
            proxy=acc['proxy'],
        )

        if not config.THREADS_KEYWORD_SEARCH:
            print(f"ℹ️ [{name}] Keyword search is off (needs Meta approval). Sleeping 1 h.")
            _stop_event.wait(timeout=3600)
            continue

        print(f"🔍 [{name}] Searching posts for '{selected_keyword}'…")
        found_posts = social.search_posts_with_ids(keyword=selected_keyword, limit=25)

        # Never reply to our own accounts (this was the "comments itself" bug)
        with get_db() as conn:
            own_names = {(r[0] or "").lower().lstrip("@") for r in conn.execute("SELECT name FROM accounts")}
        found_posts = [p for p in found_posts if p.get("username", "").lower() not in own_names]

        if not found_posts:
            print(f"⚠️  [{name}] No posts found. Sleeping 5 min.")
            _stop_event.wait(timeout=300)
            continue

        target_post      = random.choice(found_posts)
        target_post_id   = target_post.get('id')
        target_post_text = target_post.get('text', '')

        base_prompt      = acc['custom_prompt'] or "Reply as an expert, briefly and to the point."
        base_instruction = (
            f"You are a genius marketer. Write ONE comment on someone else's post.\n"
            f"YOUR STYLE: {base_prompt}\n"
            f"TARGET POST: '{target_post_text}'\n"
            f"{_ANTI_BAN_INSTRUCTION}\n"
            f"Strictly under 200 chars. Output ONLY the reply text, no quotes."
        )

        try:
            reply_text = ai.generate_text(
                f"Reply to post: {target_post_text[:50]}",
                base_instruction,
                ai_skill=ai_skill,
                post_language=post_language,
            )
            reply_text = clean_generated(reply_text)
            if not has_content(reply_text, 8):
                _stop_event.wait(timeout=30)
                continue
        except Exception as e:
            print(f"❌ [{name}] Comment generation error: {e}")
            _stop_event.wait(timeout=60)
            continue

        if any(word in reply_text.lower() for word in config.BAN_WORDS):
            print(f"🛑 [{name}] Ban word in comment. Skipping.")
            _stop_event.wait(timeout=30)
            continue

        cta           = (acc['custom_reply'] or "").strip()
        final_comment = f"{reply_text} {cta}" if cta else reply_text

        MAX_LENGTH = 500
        if len(final_comment) > MAX_LENGTH:
            truncated  = final_comment[:MAX_LENGTH - 3]
            last_space = truncated.rfind(' ')
            if last_space > MAX_LENGTH - 100:
                truncated = truncated[:last_space]
            final_comment = truncated + "..."

        print(f"✍️  [{name}] Comment: {final_comment[:100]}…")

        try:
            thr_post_id = None
            if target_post_id:
                result = social.publish_post(text=final_comment, reply_to_id=target_post_id)
                # Extract Threads post ID if available
                if "✅" in result and "(ID:" in result:
                    try:
                        thr_post_id = result.split("(ID: ")[1].rstrip(")")
                    except Exception:
                        pass
            else:
                result = social.publish_post(text=final_comment)

            log_publication(
                account_id=account_id,
                tg_user_id=acc["tg_user_id"],
                threads_post_id=thr_post_id,
                post_text=final_comment,
                post_type="outreach",
                status="published" if "✅" in result else "failed",
            )
            print(f"📊 [{name}] Result: {result}")
        except Exception as e:
            print(f"❌ [{name}] Publish error: {e}")

        del_min    = acc['outreach_delay_min'] or 10
        del_max    = acc['outreach_delay_max'] or 30
        sleep_time = random.randint(del_min * 60, del_max * 60)
        print(f"⏳ [{name}] Sleeping {sleep_time // 60} min.")
        _stop_event.wait(timeout=sleep_time)

    print(f"🔴 [Outreach] Worker for account ID {account_id} finished.")


def run_outreach_loop():
    """Dispatcher for cold neuro-commenting."""
    print("🚀 OUTREACH DISPATCHER STARTED…")

    while not _stop_event.is_set():
        try:
            with get_db() as conn:
                cursor = conn.execute("""
                    SELECT accounts.id
                    FROM accounts
                    JOIN users ON accounts.tg_user_id = users.tg_user_id
                    WHERE accounts.is_active = 1 AND users.listener_enabled = 1
                """)
                active_ids = [row[0] for row in cursor.fetchall()]
        except Exception as e:
            print(f"❌ [Outreach Dispatcher] DB error: {e}")
            _stop_event.wait(timeout=10)
            continue

        with _outreach_lock:
            for aid in active_ids:
                if aid not in _outreach_threads or not _outreach_threads[aid].is_alive():
                    th = threading.Thread(
                        target=worker_for_outreach_account,
                        args=(aid,),
                        daemon=True,
                        name=f"outreach-account-{aid}",
                    )
                    _outreach_threads[aid] = th
                    th.start()

            dead = [aid for aid, th in _outreach_threads.items() if not th.is_alive()]
            for aid in dead:
                del _outreach_threads[aid]

        _stop_event.wait(timeout=20)