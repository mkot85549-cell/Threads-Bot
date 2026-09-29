# db_utils.py — SQLite connection manager and database initializer
import sqlite3
from contextlib import contextmanager

import config
from schema import apply_schema


@contextmanager
def get_db(row_factory: bool = True):
    """
    Context manager for SQLite connections.

    • WAL mode + busy_timeout avoid 'database is locked' under concurrent load.
    • row_factory=True enables column-name access: row['id'].
    • Commits on success, rolls back on exception, always closes.
    """
    conn = sqlite3.connect(
        config.DB_NAME,
        timeout=15,
        check_same_thread=False,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")

    if row_factory:
        conn.row_factory = sqlite3.Row

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Creates/upgrades the schema (see schema.py) and prunes stale rows."""
    with get_db(row_factory=False) as conn:
        apply_schema(conn)
        # Keep processed_replies from growing unbounded
        conn.execute(
            "DELETE FROM processed_replies WHERE created_at < datetime('now', '-30 days')"
        )