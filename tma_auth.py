"""
Telegram Mini App (TMA) authentication.

Validates the initData string sent by the Telegram WebApp SDK.
See: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

STATUS: experimental / WIP.
"""

import hashlib
import hmac
import json
import time
from typing import Optional
from urllib.parse import parse_qsl

import config


def validate_init_data(init_data_raw: str, max_age_seconds: int = 86400) -> Optional[dict]:
    """
    Returns {'tg_user_id', 'first_name', ...} if initData is authentic and fresh,
    otherwise None. Never raises.
    """
    token = config.TG_BOT_TOKEN
    # With an empty token the HMAC key is publicly computable → anyone could forge initData.
    if not init_data_raw or not token:
        return None

    try:
        pairs = parse_qsl(init_data_raw, keep_blank_values=True)
    except ValueError:
        return None

    params: dict[str, str] = {}
    for key, value in pairs:
        if key in params:            # Telegram never sends duplicate keys
            return None
        params[key] = value

    received_hash = params.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    # Compare as bytes: str compare_digest raises TypeError on non-ASCII input
    if not hmac.compare_digest(computed_hash.encode(), received_hash.encode()):
        return None

    # Freshness (auth_date is part of the signed data, so it can't be stripped or edited)
    try:
        auth_date = int(params["auth_date"])
    except (KeyError, ValueError, TypeError):
        return None
    now = time.time()
    if now - auth_date > max_age_seconds or auth_date > now + 60:
        return None

    try:
        user = json.loads(params.get("user", ""))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(user, dict):
        return None
    tg_id = user.get("id")
    if not isinstance(tg_id, int) or isinstance(tg_id, bool) or tg_id <= 0:
        return None

    return {
        "tg_user_id": str(tg_id),
        "first_name": user.get("first_name", ""),
        "last_name":  user.get("last_name", ""),
        "username":   user.get("username", ""),
        "photo_url":  user.get("photo_url", ""),
        "auth_date":  str(auth_date),
    }