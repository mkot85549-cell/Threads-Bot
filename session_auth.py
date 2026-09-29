# session_auth.py — signed web session (replaces the plain `tg_user_id` cookie)
#
# pip install itsdangerous
#
# The cookie value is a signed, timestamped token. Without SECRET_AUTH_KEY
# nobody can forge a session for someone else's Telegram ID.
from datetime import datetime
from typing import Optional

from fastapi import Cookie
from itsdangerous import BadSignature, URLSafeTimedSerializer

import config

SESSION_COOKIE = "session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days

# Separate salt: a magic-link signature can never be reused as a session token.
_signer = URLSafeTimedSerializer(config.SECRET_AUTH_KEY, salt="web-session-v1")


def make_session(tg_user_id: str, first_name: str = "") -> str:
    return _signer.dumps({"uid": str(tg_user_id), "fn": first_name})


def read_session(token: Optional[str]) -> Optional[dict]:
    """Returns {'uid': ..., 'fn': ...} or None if missing / forged / expired."""
    if not token:
        return None
    try:
        data = _signer.loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:  # SignatureExpired is a subclass
        return None
    if not isinstance(data, dict) or not data.get("uid"):
        return None
    return data


# ── FastAPI dependencies ─────────────────────────────────────────────────────

def current_user(session: Optional[str] = Cookie(None)) -> Optional[str]:
    """tg_user_id of the logged-in user, or None."""
    data = read_session(session)
    return data["uid"] if data else None


def current_first_name(session: Optional[str] = Cookie(None)) -> str:
    data = read_session(session)
    return (data.get("fn") or "") if data else ""


# ── Small shared helper ──────────────────────────────────────────────────────

def sub_is_active(sub_end_date: Optional[str]) -> bool:
    if not sub_end_date:
        return False
    try:
        return datetime.strptime(sub_end_date, "%Y-%m-%d %H:%M:%S") > datetime.now()
    except (ValueError, TypeError):
        return False