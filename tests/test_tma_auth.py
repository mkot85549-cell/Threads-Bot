"""Telegram Mini App initData validation.

The tests build *valid* initData with the same algorithm Telegram uses
(https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)
and a fake bot token, so no network access or real credentials are involved.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

import config
import tma_auth


def build_init_data(user=None, auth_date=None, token=None, tamper_hash=False, extra=None):
    token = token or config.TG_BOT_TOKEN
    user = {"id": 42, "first_name": "Ann", "username": "ann"} if user is None else user
    params = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "user": json.dumps(user),
        "query_id": "AAH-test",
    }
    if extra:
        params.update(extra)

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if tamper_hash:
        digest = ("0" if digest[0] != "0" else "1") + digest[1:]
    params["hash"] = digest
    return urlencode(params)


def test_valid_init_data_is_accepted():
    result = tma_auth.validate_init_data(build_init_data())
    assert result is not None
    assert result["tg_user_id"] == "42"
    assert result["first_name"] == "Ann"
    assert result["username"] == "ann"


def test_wrong_signature_is_rejected():
    assert tma_auth.validate_init_data(build_init_data(tamper_hash=True)) is None


def test_data_signed_with_another_token_is_rejected():
    forged = build_init_data(token="999999:SOME_OTHER_TOKEN")
    assert tma_auth.validate_init_data(forged) is None


def test_modified_payload_is_rejected():
    data = build_init_data()
    tampered = data.replace("Ann", "Eve")
    assert tampered != data
    assert tma_auth.validate_init_data(tampered) is None


def test_expired_init_data_is_rejected():
    old = int(time.time()) - 2 * 86400
    assert tma_auth.validate_init_data(build_init_data(auth_date=old)) is None


def test_custom_max_age_is_respected():
    five_minutes_ago = int(time.time()) - 300
    data = build_init_data(auth_date=five_minutes_ago)
    assert tma_auth.validate_init_data(data, max_age_seconds=600) is not None
    assert tma_auth.validate_init_data(data, max_age_seconds=60) is None


def test_auth_date_from_the_future_is_rejected():
    future = int(time.time()) + 3600
    assert tma_auth.validate_init_data(build_init_data(auth_date=future)) is None


def test_duplicate_keys_are_rejected():
    data = build_init_data() + "&auth_date=1"
    assert tma_auth.validate_init_data(data) is None


@pytest.mark.parametrize("raw", ["", "garbage", "hash=abc", "auth_date=1&user={}"])
def test_malformed_input_returns_none_and_never_raises(raw):
    assert tma_auth.validate_init_data(raw) is None


@pytest.mark.parametrize("bad_id", [0, -5, "42", True, None])
def test_invalid_user_id_is_rejected(bad_id):
    data = build_init_data(user={"id": bad_id, "first_name": "X"})
    assert tma_auth.validate_init_data(data) is None


def test_empty_bot_token_never_validates(monkeypatch):
    """With an empty token the HMAC key would be publicly computable, so refuse everything."""
    data = build_init_data(token="")
    monkeypatch.setattr(config, "TG_BOT_TOKEN", "")
    assert tma_auth.validate_init_data(data) is None
