# config.py — environment configuration
#
# .env example:
# ─────────────────────────────────────────────────
# SECRET_AUTH_KEY=replace_with_32+_random_chars        (always required)
# ANTHROPIC_API_KEY=sk-ant-...                         (web app / workers)
# ANTHROPIC_MODEL=claude-sonnet-5-5                    # optional
# THREADS_APP_ID=...                                   (web app)
# THREADS_APP_SECRET=...                               (web app)
# TG_BOT_TOKEN=...                                     (bot)
# CRYPTO_PAY_TOKEN=...                                 (bot)
# TG_BOT_USERNAME=ThreadsFarmAuthBot                   # optional
# DB_NAME=farm.db                                      # optional
# BASE_URL=REDACTED              # optional
# ─────────────────────────────────────────────────
#
# Only SECRET_AUTH_KEY is checked at import time. Everything else is validated
# by config.require(...) at the entry point that actually needs it, so e.g. the
# bot no longer needs Threads OAuth keys and utilities can import config freely.

import os

from dotenv import load_dotenv

load_dotenv()


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def require(*names: str) -> None:
    """Raises a clear error if any of the named settings is empty."""
    missing = [n for n in names if not globals().get(n)]
    if missing:
        raise RuntimeError(
            "❌ Missing required environment variable(s): "
            + ", ".join(missing) + ". Check your .env file."
        )


# ── Always required: signs sessions and magic links ──────────────────────────
SECRET_AUTH_KEY = _get("SECRET_AUTH_KEY")
require("SECRET_AUTH_KEY")
if len(SECRET_AUTH_KEY) < 32:
    print("⚠️ SECRET_AUTH_KEY is shorter than 32 characters — use a longer random value.")

# ── Required per entry point (checked via config.require) ────────────────────
ANTHROPIC_API_KEY  = _get("ANTHROPIC_API_KEY")
TG_BOT_TOKEN       = _get("TG_BOT_TOKEN")
CRYPTO_PAY_TOKEN   = _get("CRYPTO_PAY_TOKEN")
THREADS_APP_ID     = _get("THREADS_APP_ID")
THREADS_APP_SECRET = _get("THREADS_APP_SECRET")

# ── Optional / defaulted ─────────────────────────────────────────────────────
TG_BOT_USERNAME = _get("TG_BOT_USERNAME", "ThreadsFarmAuthBot")
DB_NAME         = _get("DB_NAME", "farm.db")
BASE_URL        = _get("BASE_URL", "REDACTED")
# Set to 1 ONLY after Meta approved your app for `threads_keyword_search` (App Review).
# Without approval Meta searches only the account's OWN posts.
THREADS_KEYWORD_SEARCH = _get("THREADS_KEYWORD_SEARCH", "0") == "1"
REDIRECT_URI    = _get("REDIRECT_URI", "REDACTED/auth/threads/callback")

# Ban-words list — ideally migrate to the DB (ban_words table) for hot reloading
BAN_WORDS = [
    "голые", "секс", "порно", "pussy", "бля", "хуй", "проститутки",
    "casino", "казино", "ставки", "vulkan", "вулкан",
    "легкий заработок", "быстрый заработок", "заработок за день",
    "кнопка бабло", "схема заработка", "заработай легко",
    "пассивный доход без вложений",
]