"""Payment crediting (tg_bot.check_payment) with the CryptoPay API mocked out.

Only `_fetch_invoice_status` (the single place that talks to CryptoPay) is replaced;
the DB logic, exactly-once crediting and subscription update are the real code.
"""
import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
import tg_bot


def make_callback(invoice_id, user_id=1):
    return SimpleNamespace(
        data=f"check_{invoice_id}",
        from_user=SimpleNamespace(id=user_id, username="ann"),
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )


def insert_invoice(invoice_id=10, user_id="1", status="pending", amount=5.0, days=30, max_accounts=3):
    conn = sqlite3.connect(config.DB_NAME)
    conn.execute(
        "INSERT INTO invoices (invoice_id, tg_user_id, tariff_key, amount, days, max_accounts, status) "
        "VALUES (?, ?, 'basic', ?, ?, ?, ?)",
        (invoice_id, user_id, amount, days, max_accounts, status),
    )
    conn.commit()
    conn.close()


def fetch(sql, *params):
    conn = sqlite3.connect(config.DB_NAME)
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def mock_remote(monkeypatch, status):
    monkeypatch.setattr(tg_bot, "_fetch_invoice_status", AsyncMock(return_value=status))


def run(coro):
    return asyncio.run(coro)


def test_paid_invoice_credits_the_user_once(db, monkeypatch):
    insert_invoice()
    mock_remote(monkeypatch, "paid")
    cb = make_callback(10)

    run(tg_bot.check_payment(cb))

    assert fetch("SELECT status FROM invoices WHERE invoice_id = 10")[0] == "done"
    assert fetch("SELECT max_accounts FROM users WHERE tg_user_id = '1'")[0] == 3
    cb.message.edit_text.assert_awaited_once()


def test_clicking_check_twice_does_not_credit_twice(db, monkeypatch):
    insert_invoice()
    mock_remote(monkeypatch, "paid")

    run(tg_bot.check_payment(make_callback(10)))
    end_after_first = fetch("SELECT sub_end_date FROM users WHERE tg_user_id = '1'")[0]

    second = make_callback(10)
    run(tg_bot.check_payment(second))

    assert fetch("SELECT sub_end_date FROM users WHERE tg_user_id = '1'")[0] == end_after_first
    second.message.edit_text.assert_not_awaited()
    second.answer.assert_awaited_once()


def test_concurrent_request_crediting_during_api_call_does_not_cause_double_credit(db, monkeypatch):
    """The real race: request A reads the invoice as 'pending', awaits CryptoPay, and meanwhile
    request B credits it. A's atomic `UPDATE ... WHERE status = 'pending'` must then affect 0 rows.
    (The early `status == 'done'` check cannot catch this, only the atomic UPDATE can.)"""
    insert_invoice()

    async def remote_status_while_other_request_credits(invoice_id):
        conn = sqlite3.connect(config.DB_NAME)
        conn.execute("UPDATE invoices SET status = 'done' WHERE invoice_id = ?", (invoice_id,))
        conn.commit()
        conn.close()
        return "paid"

    monkeypatch.setattr(tg_bot, "_fetch_invoice_status", remote_status_while_other_request_credits)
    cb = make_callback(10)

    run(tg_bot.check_payment(cb))

    assert fetch("SELECT 1 FROM users WHERE tg_user_id = '1'") is None  # A must NOT credit again
    cb.message.edit_text.assert_not_awaited()


def test_unpaid_invoice_is_not_credited(db, monkeypatch):
    insert_invoice()
    mock_remote(monkeypatch, "active")
    cb = make_callback(10)

    run(tg_bot.check_payment(cb))

    assert fetch("SELECT status FROM invoices WHERE invoice_id = 10")[0] == "pending"
    assert fetch("SELECT 1 FROM users WHERE tg_user_id = '1'") is None
    cb.answer.assert_awaited_once()


def test_expired_invoice_is_marked_expired(db, monkeypatch):
    insert_invoice()
    mock_remote(monkeypatch, "expired")

    run(tg_bot.check_payment(make_callback(10)))

    assert fetch("SELECT status FROM invoices WHERE invoice_id = 10")[0] == "expired"
    assert fetch("SELECT 1 FROM users WHERE tg_user_id = '1'") is None


def test_api_failure_changes_nothing(db, monkeypatch):
    insert_invoice()
    mock_remote(monkeypatch, None)  # _fetch_invoice_status returns None when CryptoPay is unreachable

    run(tg_bot.check_payment(make_callback(10)))

    assert fetch("SELECT status FROM invoices WHERE invoice_id = 10")[0] == "pending"


def test_user_cannot_claim_someone_elses_invoice(db, monkeypatch):
    insert_invoice(user_id="1")
    mock_remote(monkeypatch, "paid")
    cb = make_callback(10, user_id=2)

    run(tg_bot.check_payment(cb))

    assert fetch("SELECT status FROM invoices WHERE invoice_id = 10")[0] == "pending"
    assert fetch("SELECT 1 FROM users WHERE tg_user_id = '2'") is None


def test_unknown_invoice_is_reported(db, monkeypatch):
    mock_remote(monkeypatch, "paid")
    cb = make_callback(999)
    run(tg_bot.check_payment(cb))
    cb.answer.assert_awaited_once()
    assert fetch("SELECT COUNT(*) FROM users")[0] == 0


@pytest.mark.parametrize(
    "raw, expected",
    [("paid", "paid"), ("PAID", "paid"), (" Active ", "active")],
)
def test_fetch_invoice_status_normalises_api_response(monkeypatch, raw, expected):
    """The CryptoPay client is replaced by a fake: no network involved."""
    fake_invoice = SimpleNamespace(status=SimpleNamespace(value=raw))
    monkeypatch.setattr(tg_bot, "crypto", SimpleNamespace(get_invoices=AsyncMock(return_value=[fake_invoice])))
    assert run(tg_bot._fetch_invoice_status(1)) == expected


def test_fetch_invoice_status_returns_none_on_api_error(monkeypatch):
    monkeypatch.setattr(tg_bot, "crypto", SimpleNamespace(get_invoices=AsyncMock(side_effect=RuntimeError("boom"))))
    assert run(tg_bot._fetch_invoice_status(1)) is None


def test_fetch_invoice_status_returns_none_for_empty_result(monkeypatch):
    monkeypatch.setattr(tg_bot, "crypto", SimpleNamespace(get_invoices=AsyncMock(return_value=[])))
    assert run(tg_bot._fetch_invoice_status(1)) is None
