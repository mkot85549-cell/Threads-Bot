"""
Database migration script for Threads-Bot.

    python migrations/migrate.py [path_to_db] [--clean-placeholders]

• Idempotent: safe to run any number of times (schema lives in schema.py).
• Brings legacy promo_codes tables (used_count) to the canonical layout (uses_left).
• Does NOT import config, so it runs without any API tokens.

--clean-placeholders  ONE-TIME data cleanup: sets accounts.custom_reply to NULL
                      where it contains a known legacy default phrase. It
                      overwrites user text, so it is never run implicitly.
"""

import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from schema import apply_schema  # noqa: E402

load_dotenv(project_root / ".env")
DEFAULT_DB_NAME = os.environ.get("DB_NAME", "farm.db")

LEGACY_PLACEHOLDERS = [
    "Вся информация в шапке профиля",
    "More info in bio",
    "Гайд уже в шапке профиля",
    "Guide in bio",
    "Info in profile",
]


def run_all_migrations(db_path: str = None, clean_placeholders: bool = False) -> None:
    if not db_path:
        args = [a for a in sys.argv[1:] if not a.startswith("--")]
        db_path = args[0] if args else DEFAULT_DB_NAME

    print(f"🚀 Starting database migration for: {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")

        apply_schema(conn)

        cleaned = 0
        if clean_placeholders:
            rows = conn.execute(
                "SELECT id, custom_reply FROM accounts WHERE custom_reply IS NOT NULL"
            ).fetchall()
            for acc_id, custom_reply in rows:
                if any(ph in custom_reply for ph in LEGACY_PLACEHOLDERS):
                    conn.execute("UPDATE accounts SET custom_reply = NULL WHERE id = ?", (acc_id,))
                    cleaned += 1

        conn.commit()
    finally:
        conn.close()

    print("✅ Schema verified/upgraded successfully.")
    if clean_placeholders:
        print(f"🧹 Cleared legacy placeholder custom_reply on {cleaned} account(s).")
    print("🚀 Migration finished.")


if __name__ == "__main__":
    run_all_migrations(clean_placeholders="--clean-placeholders" in sys.argv)