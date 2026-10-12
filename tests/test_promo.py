"""Promo codes and subscription logic (tg_bot.py), against a real temporary SQLite DB."""
import sqlite3
import threading
from datetime import datetime, timedelta

import pytest

import config
import tg_bot
from tg_bot import TS_FMT, _create_promo, _redeem_promo, add_or_update_user


def user_row(tg_id):
    conn = sqlite3.connect(config.DB_NAME)
    try:
        return conn.execute(
            "SELECT sub_end_date, max_accounts FROM users WHERE tg_user_id = ?", (str(tg_id),)
        ).fetchone()
    finally:
        conn.close()


def uses_left(code):
    conn = sqlite3.connect(config.DB_NAME)
    try:
        return conn.execute("SELECT uses_left FROM promo_codes WHERE code = ?", (code,)).fetchone()[0]
    finally:
        conn.close()


def test_create_promo_does_not_overwrite_existing_code(db):
    assert _create_promo("ABC", days=7, max_accounts=1)
    assert not _create_promo("ABC", days=30, max_accounts=5)


def test_redeem_unknown_code(db):
    assert _redeem_promo("NOPE", "1", "u") == {"ok": False, "reason": "not_found"}


def test_redeem_creates_user_with_subscription(db):
    _create_promo("TRIAL", days=7, max_accounts=2)
    result = _redeem_promo("TRIAL", "100", "ann")

    assert result["ok"] is True
    assert result["days"] == 7
    end, max_accs = user_row(100)
    assert max_accs == 2
    delta = datetime.strptime(end, TS_FMT) - datetime.now()
    assert timedelta(days=6, hours=23) < delta <= timedelta(days=7)


def test_code_is_normalised_before_lookup(db):
    _create_promo("TRIAL", days=1, max_accounts=1)
    assert _redeem_promo("  trial  ", "1", None)["ok"] is True


def test_same_user_cannot_redeem_twice(db):
    _create_promo("ONCE", days=7, max_accounts=1, max_uses=5)
    assert _redeem_promo("ONCE", "1", "u")["ok"] is True
    assert _redeem_promo("ONCE", "1", "u") == {"ok": False, "reason": "already_used"}
    assert uses_left("ONCE") == 4  # the failed attempt must not consume a use


def test_code_is_used_up_after_max_uses(db):
    _create_promo("ONE", days=7, max_accounts=1, max_uses=1)
    assert _redeem_promo("ONE", "1", "a")["ok"] is True
    assert _redeem_promo("ONE", "2", "b") == {"ok": False, "reason": "used_up"}
    assert user_row(2) is None  # second user got nothing


def test_expired_code_is_rejected(db):
    _create_promo("OLD", days=7, max_accounts=1)
    past = (datetime.now() - timedelta(days=1)).strftime(TS_FMT)
    conn = sqlite3.connect(config.DB_NAME)
    conn.execute("UPDATE promo_codes SET expires_at = ? WHERE code = 'OLD'", (past,))
    conn.commit()
    conn.close()

    assert _redeem_promo("OLD", "1", "u") == {"ok": False, "reason": "expired"}
    assert user_row(1) is None


def test_promo_extends_active_subscription_instead_of_resetting_it(db):
    add_or_update_user("1", "u", add_days=30, max_accs=1)
    before = datetime.strptime(user_row(1)[0], TS_FMT)

    _create_promo("EXTRA", days=10, max_accounts=1)
    _redeem_promo("EXTRA", "1", "u")

    after = datetime.strptime(user_row(1)[0], TS_FMT)
    assert after - before == timedelta(days=10)


def test_small_promo_never_shrinks_an_active_bigger_plan(db):
    add_or_update_user("1", "u", add_days=30, max_accs=10)
    _create_promo("SMALL", days=5, max_accounts=2)
    _redeem_promo("SMALL", "1", "u")
    assert user_row(1)[1] == 10


def test_new_tier_applies_as_is_after_expiry(db):
    add_or_update_user("1", "u", add_days=30, max_accs=10)
    expired = (datetime.now() - timedelta(days=1)).strftime(TS_FMT)
    conn = sqlite3.connect(config.DB_NAME)
    conn.execute("UPDATE users SET sub_end_date = ? WHERE tg_user_id = '1'", (expired,))
    conn.commit()
    conn.close()

    _create_promo("SMALL", days=5, max_accounts=2)
    _redeem_promo("SMALL", "1", "u")
    assert user_row(1)[1] == 2


def test_concurrent_redemption_of_single_use_code_succeeds_exactly_once(db):
    """The `UPDATE ... WHERE uses_left > 0` guard must hold under parallel redemption."""
    _create_promo("RACE", days=7, max_accounts=1, max_uses=1)
    results, barrier = [], threading.Barrier(8)

    def worker(uid):
        barrier.wait()
        try:
            results.append(_redeem_promo("RACE", str(uid), f"u{uid}"))
        except sqlite3.OperationalError as exc:  # SQLite lock contention is acceptable, double credit is not
            results.append({"ok": False, "reason": f"locked: {exc}"})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert sum(r["ok"] for r in results) == 1
    assert uses_left("RACE") == 0


def test_brute_force_guard_blocks_after_max_failures(monkeypatch):
    monkeypatch.setattr(tg_bot, "_promo_fails", {})
    uid = 555
    assert not tg_bot._promo_blocked(uid)
    for _ in range(tg_bot._PROMO_MAX_FAILS):
        tg_bot._promo_register_fail(uid)
    assert tg_bot._promo_blocked(uid)
    assert not tg_bot._promo_blocked(999)  # other users are unaffected


def test_brute_force_guard_forgets_old_failures(monkeypatch):
    monkeypatch.setattr(tg_bot, "_promo_fails", {})
    old = tg_bot.time.time() - tg_bot._PROMO_WINDOW - 1
    tg_bot._promo_fails[7] = [old] * tg_bot._PROMO_MAX_FAILS
    assert not tg_bot._promo_blocked(7)


def test_generated_codes_avoid_ambiguous_characters():
    for _ in range(50):
        prefix, suffix = tg_bot._generate_code("TRIAL").split("-")
        assert prefix == "TRIAL" and len(suffix) == 8
        assert not set(suffix) & set("01OI")
