"""Shared pytest fixtures.

The env vars are set BEFORE any project module is imported, because config.py
validates SECRET_AUTH_KEY at import time. No real API credentials are needed:
all external services (Meta, Anthropic, CryptoPay) are either mocked or never
touched by the code under test.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("SECRET_AUTH_KEY", "test_secret_key_0123456789_abcdefghij")
os.environ.setdefault("TG_BOT_TOKEN", "123456:TEST_BOT_TOKEN")
os.environ.setdefault("CRYPTO_PAY_TOKEN", "1:TEST_CRYPTO_TOKEN")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")

# The project keeps its modules in the repo root, so make them importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import config  # noqa: E402
from db_utils import init_db  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh, isolated SQLite database with the real schema applied."""
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "test.db"))
    init_db()
    return config.DB_NAME
