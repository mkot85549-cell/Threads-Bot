# token_refresher.py — keeps Threads long-lived tokens alive.
#
# Meta: a long-lived token lasts 60 days and can be refreshed once it is >= 24 h old
# and not yet expired; a refresh returns a NEW token valid for another 60 days.
# A token that is not refreshed within 60 days dies permanently.
# https://developers.facebook.com/docs/threads/get-started/long-lived-tokens/
import html
import threading
from typing import Optional

import requests

import config
from db_utils import get_db

stop_event = threading.Event()

REFRESH_URL         = "https://graph.threads.net/refresh_access_token"
REFRESH_AFTER_DAYS  = 20      # refresh when token is older than this (leaves ~40 days of retries)
CHECK_INTERVAL_SEC  = 6 * 3600
OAUTH_INVALID_CODE  = 190     # Graph API: "invalid / expired / revoked access token"


def _notify_user(tg_user_id: Optional[str], text: str) -> None:
    """Best-effort Telegram message; the web process has no aiogram Bot, so plain HTTP."""
    if not config.TG_BOT_TOKEN or not tg_user_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": tg_user_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except requests.RequestException:
        pass


def _refresh_one(token: str, proxy: Optional[str], age_days: Optional[float]):
    """
    Returns (outcome, value):
      ("ok", new_token)      – refreshed
      ("revoked", message)   – token is permanently invalid, user must reconnect
      ("retry", message)     – transient / unknown problem, try again next cycle
    """
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = requests.get(
            REFRESH_URL,
            params={"grant_type": "th_refresh_token", "access_token": token},
            proxies=proxies,
            timeout=20,
        )
    except requests.RequestException as exc:
        return "retry", f"network error: {exc.__class__.__name__}"

    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}

    new_token = data.get("access_token")
    if resp.ok and new_token:
        return "ok", new_token

    err = data.get("error") if isinstance(data.get("error"), dict) else {}
    message = f"HTTP {resp.status_code}: {str(err.get('message') or resp.text)[:200]}"

    if err.get("code") == OAUTH_INVALID_CODE:
        return "revoked", message
    # Older than 60 days and refusing → it has expired for good
    if age_days is not None and age_days >= 60 and 400 <= resp.status_code < 500:
        return "revoked", message
    return "retry", message


def refresh_due_tokens() -> dict:
    stats = {"ok": 0, "revoked": 0, "retry": 0}

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, name, tg_user_id, access_token, proxy,
                   CASE WHEN token_updated_at IS NULL THEN NULL
                        ELSE julianday('now') - julianday(token_updated_at) END AS age_days
            FROM accounts
            WHERE access_token IS NOT NULL AND access_token != ''
              AND (token_status IS NULL OR token_status != 'revoked')
              AND (token_updated_at IS NULL OR token_updated_at < datetime('now', ?))
            """,
            (f"-{REFRESH_AFTER_DAYS} days",),
        ).fetchall()

    for row in rows:
        if stop_event.is_set():
            break
        outcome, value = _refresh_one(row["access_token"], row["proxy"], row["age_days"])
        stats[outcome] += 1

        with get_db() as conn:
            if outcome == "ok":
                # Compare-and-set: don't clobber a token the user replaced meanwhile
                conn.execute(
                    "UPDATE accounts SET access_token = ?, token_updated_at = datetime('now'), "
                    "token_status = 'ok' WHERE id = ? AND access_token = ?",
                    (value, row["id"], row["access_token"]),
                )
                print(f"🔄 [Tokens] refreshed account {row['id']} ({row['name']})")
            elif outcome == "revoked":
                conn.execute(
                    "UPDATE accounts SET is_active = 0, token_status = 'revoked' WHERE id = ?",
                    (row["id"],),
                )
                print(f"🚫 [Tokens] account {row['id']} ({row['name']}) token invalid: {value}")
                _notify_user(
                    row["tg_user_id"],
                    f"⚠️ Токен Threads-аккаунта <b>{html.escape(row['name'] or '')}</b> "
                    f"больше не действует, аккаунт отключён.\n"
                    f"Откройте панель и подключите его заново.",
                )
            else:
                print(f"⏳ [Tokens] account {row['id']} ({row['name']}) will retry: {value}")

        stop_event.wait(1)  # be gentle with the API
    return stats


def token_refresh_loop() -> None:
    print("🔄 [Tokens] refresher started")
    stop_event.wait(60)  # let the app finish starting
    while not stop_event.is_set():
        try:
            stats = refresh_due_tokens()
            if any(stats.values()):
                print(f"🔄 [Tokens] cycle done: {stats}")
        except Exception as exc:
            print(f"❌ [Tokens] cycle error: {exc}")
        stop_event.wait(CHECK_INTERVAL_SEC)