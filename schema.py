# schema.py — the ONE place where the database schema lives.
#
# Used by:  db_utils.init_db()   (web app startup)
#           migrations/migrate.py (manual / deploy-time migration)
#           tg_bot.py            (bot startup)
#
# Deliberately does NOT import `config`, so migrations can run without any
# API tokens configured.
import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, col: str, typedef: str) -> None:
    """Adds a column only if it is missing (no blanket try/except)."""
    if col not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")


# ── Column lists for tables that grew over time ──────────────────────────────

_USERS_COLUMNS = [
    ("username",         "TEXT"),
    ("sub_end_date",     "DATETIME"),
    ("max_accounts",     "INTEGER DEFAULT 3"),
    ("farm_enabled",     "INTEGER DEFAULT 0"),
    ("listener_enabled", "INTEGER DEFAULT 0"),
]

_ACCOUNT_COLUMNS = [
    ("post_mode",          "TEXT DEFAULT 'prompts'"),
    ("prompt_1",           "TEXT"),
    ("prompt_2",           "TEXT"),
    ("prompt_3",           "TEXT"),
    ("prompt_4",           "TEXT"),
    ("prompt_5",           "TEXT"),
    ("source_themes",      "TEXT"),
    ("target_theme",       "TEXT"),
    ("mini_prompt",        "TEXT"),
    ("tg_user_id",         "TEXT"),
    ("pub_delay_min",      "INTEGER DEFAULT 15"),
    ("pub_delay_max",      "INTEGER DEFAULT 45"),
    ("outreach_keywords",  "TEXT"),
    ("outreach_delay_min", "INTEGER DEFAULT 10"),
    ("outreach_delay_max", "INTEGER DEFAULT 30"),
    ("post_language",      "TEXT DEFAULT 'ru'"),
    ("ai_skill",           "TEXT DEFAULT ''"),
    ("ai_temperature",     "REAL DEFAULT 0.7"),
    ("token_updated_at",   "DATETIME"),          # NULL = unknown age → refreshed on next cycle
    ("token_status",       "TEXT DEFAULT 'ok'"),  # 'revoked' = user must reconnect
]

_PUBLICATION_COLUMNS = [
    ("views",         "INTEGER DEFAULT 0"),
    ("likes",         "INTEGER DEFAULT 0"),
    ("replies_count", "INTEGER DEFAULT 0"),
    ("reposts",       "INTEGER DEFAULT 0"),
    ("insights_at",   "DATETIME"),
    ("prompt_id",     "TEXT"),
]


def _migrate_promo_codes(conn: sqlite3.Connection) -> None:
    """
    Two incompatible promo_codes layouts exist in the wild:
      • old migrate.py:  used_count, no uses_left / created_by
      • old tg_bot.py:   uses_left + created_by
    Bring either one to the canonical layout (uses_left + created_by).
    """
    cols = _columns(conn, "promo_codes")
    if "uses_left" not in cols:
        conn.execute("ALTER TABLE promo_codes ADD COLUMN uses_left INTEGER NOT NULL DEFAULT 1")
        if "used_count" in cols:
            conn.execute("UPDATE promo_codes SET uses_left = MAX(max_uses - used_count, 0)")
        else:
            conn.execute("UPDATE promo_codes SET uses_left = max_uses")
    if "created_by" not in cols:
        conn.execute("ALTER TABLE promo_codes ADD COLUMN created_by TEXT")


def apply_schema(conn: sqlite3.Connection) -> None:
    """Creates / upgrades every table and index. Idempotent; safe on every start."""

    # ── users ────────────────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            tg_user_id       TEXT PRIMARY KEY,
            username         TEXT,
            sub_end_date     DATETIME,
            max_accounts     INTEGER DEFAULT 3,
            farm_enabled     INTEGER DEFAULT 0,
            listener_enabled INTEGER DEFAULT 0
        )
    """)
    for col, typedef in _USERS_COLUMNS:
        _add_column(conn, "users", col, typedef)

    # ── accounts ─────────────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            name               TEXT,
            threads_user_id    TEXT,
            access_token       TEXT,
            style              TEXT,
            proxy              TEXT,
            custom_prompt      TEXT,
            custom_reply       TEXT,
            is_active          INTEGER DEFAULT 1
        )
    """)
    for col, typedef in _ACCOUNT_COLUMNS:
        _add_column(conn, "accounts", col, typedef)

    # ── ban_words / processed_replies ────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ban_words (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT UNIQUE NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_replies (
            reply_id   TEXT PRIMARY KEY,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── analytics ────────────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS page_views (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            path         TEXT    NOT NULL,
            tg_user_id   TEXT,
            ip_hash      TEXT,
            user_agent   TEXT,
            referer      TEXT,
            created_at   DATETIME DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS publications_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id      INTEGER NOT NULL,
            tg_user_id      TEXT    NOT NULL,
            threads_post_id TEXT,
            post_text       TEXT,
            post_type       TEXT    DEFAULT 'post',
            status          TEXT    DEFAULT 'published',
            published_at    DATETIME DEFAULT (datetime('now'))
        )
    """)
    for col, typedef in _PUBLICATION_COLUMNS:
        _add_column(conn, "publications_log", col, typedef)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS stats_cache (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_user_id    TEXT    NOT NULL,
            stat_date     DATE    NOT NULL,
            page_views    INTEGER DEFAULT 0,
            unique_users  INTEGER DEFAULT 0,
            posts_count   INTEGER DEFAULT 0,
            total_likes   INTEGER DEFAULT 0,
            total_views   INTEGER DEFAULT 0,
            updated_at    DATETIME DEFAULT (datetime('now')),
            UNIQUE (tg_user_id, stat_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS system_prompts (
            id           TEXT PRIMARY KEY,
            account_id   INTEGER,
            tg_user_id   TEXT,
            prompt_text  TEXT NOT NULL,
            is_active    INTEGER DEFAULT 0,
            score        REAL DEFAULT 0.0,
            generated_by TEXT DEFAULT 'manual',
            created_at   DATETIME DEFAULT (datetime('now'))
        )
    """)

    # ── promo codes ──────────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            code          TEXT PRIMARY KEY,
            days          INTEGER NOT NULL,
            max_accounts  INTEGER NOT NULL DEFAULT 3,
            max_uses      INTEGER NOT NULL DEFAULT 1,
            uses_left     INTEGER NOT NULL DEFAULT 1,
            expires_at    DATETIME,
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            created_by    TEXT
        )
    """)
    _migrate_promo_codes(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS promo_uses (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            code       TEXT NOT NULL,
            tg_user_id TEXT NOT NULL,
            used_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(code, tg_user_id)
        )
    """)

    # ── bot: menu message + payments ─────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_menu_messages (
            tg_user_id  TEXT PRIMARY KEY,
            message_id  INTEGER NOT NULL
        )
    """)
    # status: pending → done (credited exactly once) | expired
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            invoice_id    INTEGER PRIMARY KEY,
            tg_user_id    TEXT    NOT NULL,
            tariff_key    TEXT    NOT NULL,
            amount        REAL    NOT NULL,
            days          INTEGER NOT NULL,
            max_accounts  INTEGER NOT NULL,
            status        TEXT    NOT NULL DEFAULT 'pending',
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            paid_at       DATETIME
        )
    """)

    # ── indexes ──────────────────────────────────────────────────────────────
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_accounts_tg_user_id ON accounts (tg_user_id)",
        "CREATE INDEX IF NOT EXISTS idx_accounts_active ON accounts (is_active, tg_user_id)",
        "CREATE INDEX IF NOT EXISTS idx_pv_path_time ON page_views (path, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_pv_time ON page_views (created_at)",
        "CREATE INDEX IF NOT EXISTS idx_pub_tg_user ON publications_log (tg_user_id, published_at)",
        "CREATE INDEX IF NOT EXISTS idx_pub_account ON publications_log (account_id, published_at)",
        "CREATE INDEX IF NOT EXISTS idx_pub_prompt ON publications_log (prompt_id)",
        "CREATE INDEX IF NOT EXISTS idx_sp_account_active ON system_prompts (account_id, is_active)",
        "CREATE INDEX IF NOT EXISTS idx_invoices_user ON invoices (tg_user_id, status)",
    ):
        conn.execute(sql)

    # Unique index can fail if legacy data already contains duplicates — warn, don't crash.
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_threads_user_id "
            "ON accounts (threads_user_id)"
        )
    except sqlite3.IntegrityError:
        print("⚠️ [schema] Duplicate threads_user_id rows exist in `accounts`; "
              "unique index NOT created. Remove duplicates and restart.")