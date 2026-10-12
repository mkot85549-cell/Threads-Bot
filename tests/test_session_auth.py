import time
from datetime import datetime, timedelta

import session_auth
from session_auth import make_session, read_session, sub_is_active


def test_session_roundtrip():
    token = make_session("123", "Ann")
    assert read_session(token) == {"uid": "123", "fn": "Ann"}


def test_garbage_and_empty_tokens_are_rejected():
    assert read_session(None) is None
    assert read_session("") is None
    assert read_session("not-a-token") is None


def test_tampered_token_is_rejected():
    token = make_session("123")
    assert read_session(token[:-2] + ("aa" if not token.endswith("aa") else "bb")) is None


def test_expired_session_is_rejected(monkeypatch):
    token = make_session("123")
    monkeypatch.setattr(session_auth, "SESSION_MAX_AGE", -1)
    time.sleep(0.01)
    assert read_session(token) is None


def test_sub_is_active():
    fmt = "%Y-%m-%d %H:%M:%S"
    future = (datetime.now() + timedelta(days=1)).strftime(fmt)
    past = (datetime.now() - timedelta(days=1)).strftime(fmt)
    assert sub_is_active(future)
    assert not sub_is_active(past)
    assert not sub_is_active(None)
    assert not sub_is_active("not a date")
