# listener.py — Comment monitoring and auto-replies worker

import time
import random
import threading
import requests

from db_utils import get_db
from social_service import ThreadsFarmService

# Number of the account's latest posts to inspect for leads.
CHECK_LAST_N_POSTS = 10

# Threading event allowing web_app.py to stop the worker gracefully without blocking on sleep.
stop_event = threading.Event()


def _mark_reply_processed(reply_id: str) -> None:
    """Atomically records the processed comment ID into SQLite."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_replies (reply_id) VALUES (?)",
            (reply_id,)
        )


def _is_reply_processed(reply_id: str) -> bool:
    """Checks if the comment has already been processed."""
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT 1 FROM processed_replies WHERE reply_id = ?",
            (reply_id,)
        )
        return cursor.fetchone() is not None


def _make_session(proxy: str | None) -> requests.Session:
    """
    Creates a requests.Session configured with proxies and retries.
    Extracted as a helper to keep account processing clean and isolated.
    """
    session = requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    adapter = requests.adapters.HTTPAdapter(max_retries=1)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _process_account(acc: dict, base_url: str) -> None:
    """
    Processes a single account: checks for comment leads under recent posts and sends auto-replies.
    """
    name         = acc['name']
    user_id      = acc['threads_user_id']
    access_token = acc['access_token']
    proxy        = acc['proxy']
    safe_reply   = (acc['custom_reply'] or "").strip()
    
    # If user hasn't configured a reply, skip automatic replies
    if not safe_reply:
        print(f"⏭️  [{name}] No custom_reply configured — skipping auto-replies.")
        return
    
    # Ensure safe_reply does not exceed the Threads API limit (500 characters).
    MAX_LENGTH = 500
    if len(safe_reply) > MAX_LENGTH:
        safe_reply = safe_reply[:MAX_LENGTH-3] + "..."

    social  = ThreadsFarmService(user_id, access_token, proxy)
    session = _make_session(proxy)

    try:
        # Step 1: fetch the account's latest posts.
        response = session.get(
            f"{base_url}/{user_id}/threads",
            params={"access_token": access_token},
            timeout=20
        )
        response.raise_for_status()
        threads_data = response.json().get("data", [])

        for thread in threads_data[:CHECK_LAST_N_POSTS]:
            # Step 2: fetch replies to each post.
            replies_response = session.get(
                f"{base_url}/{thread['id']}/replies",
                params={"access_token": access_token},
                timeout=20
            )
            replies_response.raise_for_status()

            for reply in replies_response.json().get("data", []):
                reply_id = reply["id"]
                text     = reply.get("text", "").strip()

                # A lead is a comment containing "+" that has not been processed.
                if "+" not in text or _is_reply_processed(reply_id):
                    continue

                print(f"🎯 LEAD! '+' detected (Account: {name})")

                if random.choice([True, False]):
                    print("🎲 Decision: Replying to lead!")
                    result = social.publish_post(text=safe_reply, reply_to_id=reply_id)
                    print(f"💬 Status: {result}")
                else:
                    print("🎲 Decision: Skipping this lead.")

                # Always record it so this reply is not processed again.
                _mark_reply_processed(reply_id)
                stop_event.wait(timeout=random.randint(5, 15))

    except requests.exceptions.Timeout:
        print(f"⏰ [{name}] Request timeout to Threads API. Skipping.")
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            print(f"🚦 [{name}] Rate limit from Meta! Sleeping 15 minutes.")
            stop_event.wait(timeout=900)
        else:
            print(f"❌ [{name}] HTTP error: {e}")
    except Exception as e:
        print(f"❌ Error while checking account '{name}': {e}")
    finally:
        session.close()


def check_and_reply() -> None:
    """Main auto-reply loop. Runs as a background daemon thread from web_app.py."""
    print("🟢 Auto-reply module started successfully!")
    base_url = "https://graph.threads.net/v1.0"

    while not stop_event.is_set():
        print(f"\n[{time.strftime('%H:%M:%S')}] 🎧 Listener searching for leads...")

        try:
            with get_db() as conn:
                accounts = conn.execute("""
                    SELECT
                        accounts.id,
                        accounts.name,
                        accounts.threads_user_id,
                        accounts.access_token,
                        accounts.proxy,
                        accounts.custom_reply,
                        accounts.is_active
                    FROM accounts
                    JOIN users ON accounts.tg_user_id = users.tg_user_id
                    WHERE accounts.is_active = 1 AND users.listener_enabled = 1
                """).fetchall()
        except Exception as e:
            print(f"❌ [Listener] Database error reading accounts: {e}")
            stop_event.wait(timeout=30)
            continue

        for acc in accounts:
            if stop_event.is_set():
                break
            _process_account(acc, base_url)
            # Pause between accounts to reduce rate limiting / ban risks.
            stop_event.wait(timeout=random.randint(10, 30))

        print("💤 Listener sleeping for 5 minutes...")
        stop_event.wait(timeout=300)

    print("🔴 [Listener] Worker stopped.")


if __name__ == "__main__":
    check_and_reply()