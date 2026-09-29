# analytics_service.py — Page-view tracker + Threads insights poller
#
# Used by:
#   • web_app.py   — middleware calls record_view() on every request
#   • stats routes — aggregate_stats(), get_dashboard_data()
#   • APScheduler  — refresh_insights() called every 30 min (optional)

import hashlib
import hmac
import sqlite3
import requests
from datetime import datetime, date, timedelta
from contextlib import contextmanager
from typing import Optional

import config


# ── DB helper (mirrors db_utils but avoids circular import) ──────────────────

@contextmanager
def _db(row_factory: bool = True):
    conn = sqlite3.connect(config.DB_NAME, timeout=15, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
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


# ── View tracking ─────────────────────────────────────────────────────────────

def _hash_ip(ip: str) -> str:
    """One-way salted HMAC hash of IP — raw IPs are never stored."""
    if not ip:
        return ""
    return hmac.new(config.SECRET_AUTH_KEY.encode(), ip.encode(), hashlib.sha256).hexdigest()[:16]


def record_view(
    path: str,
    *,
    tg_user_id: Optional[str] = None,
    client_ip: str = "",
    user_agent: str = "",
    referer: str = "",
) -> None:
    """
    Inserts a single page-view row.  Runs in < 1 ms on WAL SQLite.
    Call this from FastAPI middleware — it will NOT slow requests.
    """
    try:
        ip_hash = _hash_ip(client_ip) if client_ip else None
        with _db(row_factory=False) as conn:
            conn.execute(
                """
                INSERT INTO page_views (path, tg_user_id, ip_hash, user_agent, referer)
                VALUES (?, ?, ?, ?, ?)
                """,
                (path, tg_user_id or None, ip_hash, user_agent[:256] or None, referer[:512] or None),
            )
    except Exception as exc:
        # Never crash the request — just log
        print(f"[analytics] record_view failed: {exc}")


def log_publication(
    *,
    account_id: int,
    tg_user_id: str,
    threads_post_id: Optional[str] = None,
    post_text: str = "",
    post_type: str = "post",
    status: str = "published",
) -> None:
    """
    Called from farm_manager / outreach_bot after every publish attempt.
    Stores a lightweight record so the dashboard can show publication counts.
    """
    try:
        with _db(row_factory=False) as conn:
            conn.execute(
                """
                INSERT INTO publications_log
                    (account_id, tg_user_id, threads_post_id, post_text, post_type, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (account_id, tg_user_id, threads_post_id, post_text[:500], post_type, status),
            )
    except Exception as exc:
        print(f"[analytics] log_publication failed: {exc}")


# ── Threads Insights fetcher ──────────────────────────────────────────────────

_THREADS_BASE = "https://graph.threads.net/v1.0"
_INSIGHT_FIELDS = "views,likes,replies,reposts"


def fetch_post_insights(post_id: str, access_token: str, proxy: Optional[str] = None) -> dict:
    """
    Fetches per-post metrics from the Threads Graph API.
    Returns a dict: {'views': N, 'likes': N, 'replies_count': N, 'reposts': N}
    or empty dict on error.
    """
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        url = f"{_THREADS_BASE}/{post_id}/insights"
        params = {
            "metric": _INSIGHT_FIELDS,
            "access_token": access_token,
        }
        resp = requests.get(url, params=params, proxies=proxies, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        result = {}
        for item in data.get("data", []):
            name = item.get("name")
            value = item.get("values", [{}])[0].get("value", 0)
            if name == "views":
                result["views"] = value
            elif name == "likes":
                result["likes"] = value
            elif name == "replies":
                result["replies_count"] = value
            elif name == "reposts":
                result["reposts"] = value
        return result

    except Exception as exc:
        print(f"[analytics] fetch_post_insights({post_id}) error: {exc}")
        return {}


def fetch_account_insights(
    threads_user_id: str,
    access_token: str,
    since_days: int = 7,
    proxy: Optional[str] = None,
) -> list[dict]:
    """
    Fetches the recent media objects (posts) for a Threads account.
    Returns a list of dicts with id + text for the last `since_days` days.
    """
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        url = f"{_THREADS_BASE}/{threads_user_id}/threads"
        since = (datetime.utcnow() - timedelta(days=since_days)).strftime("%s")
        params = {
            "fields": "id,text,timestamp,media_type",
            "since": since,
            "limit": 25,
            "access_token": access_token,
        }
        resp = requests.get(url, params=params, proxies=proxies, timeout=15)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as exc:
        print(f"[analytics] fetch_account_insights error: {exc}")
        return []


def refresh_insights_for_user(tg_user_id: str) -> int:
    """
    Pulls fresh insight numbers from the Threads API for all recent publications
    belonging to `tg_user_id`.  Returns count of rows updated.
    Updates: views, likes, replies_count, reposts, insights_at.

    Call periodically (e.g., every 30 min via a background thread).
    """
    updated = 0
    with _db() as conn:
        # Get account tokens for this user
        accounts = conn.execute(
            "SELECT id, threads_user_id, access_token, proxy FROM accounts "
            "WHERE tg_user_id = ? AND is_active = 1",
            (tg_user_id,),
        ).fetchall()

        for acc in accounts:
            # Get recent published posts without fresh insights
            pubs = conn.execute(
                """
                SELECT id, threads_post_id FROM publications_log
                WHERE account_id = ?
                  AND tg_user_id = ?
                  AND status = 'published'
                  AND threads_post_id IS NOT NULL
                  AND (insights_at IS NULL
                       OR insights_at < datetime('now', '-30 minutes'))
                ORDER BY published_at DESC LIMIT 20
                """,
                (acc["id"], tg_user_id),
            ).fetchall()

            for pub in pubs:
                metrics = fetch_post_insights(
                    pub["threads_post_id"],
                    acc["access_token"],
                    acc["proxy"],
                )
                if metrics:
                    conn.execute(
                        """
                        UPDATE publications_log
                        SET views         = ?,
                            likes         = ?,
                            replies_count = ?,
                            reposts       = ?,
                            insights_at   = datetime('now')
                        WHERE id = ?
                        """,
                        (
                            metrics.get("views", 0),
                            metrics.get("likes", 0),
                            metrics.get("replies_count", 0),
                            metrics.get("reposts", 0),
                            pub["id"],
                        ),
                    )
                    updated += 1

    return updated


def refresh_insights_for_all_users() -> int:
    """
    Iterates over every user that has at least one active account and calls
    refresh_insights_for_user() for each.  Returns total rows updated.

    Called by the background scheduler in web_app.py every 30 minutes.
    """
    total = 0
    try:
        with _db() as conn:
            users = conn.execute(
                "SELECT DISTINCT tg_user_id FROM accounts WHERE is_active = 1"
            ).fetchall()

        for row in users:
            uid = row["tg_user_id"]
            if not uid:
                continue
            try:
                n = refresh_insights_for_user(uid)
                if n:
                    print(f"[insights] {uid}: updated {n} posts")
                total += n
            except Exception as exc:
                print(f"[insights] error for {uid}: {exc}")

    except Exception as exc:
        print(f"[insights] refresh_insights_for_all_users failed: {exc}")

    return total


def _insights_scheduler_loop(interval_seconds: int = 1800) -> None:
    """
    Infinite loop that refreshes insights for all users every `interval_seconds`
    (default 30 min).  Designed to run inside a daemon thread.
    """
    import time
    print(f"[insights] scheduler started — interval {interval_seconds // 60} min")
    while True:
        time.sleep(interval_seconds)
        print("[insights] running scheduled refresh…")
        try:
            n = refresh_insights_for_all_users()
            print(f"[insights] scheduled refresh done — {n} rows updated")
        except Exception as exc:
            print(f"[insights] scheduled refresh error: {exc}")


# ── Aggregation helpers ───────────────────────────────────────────────────────

def get_dashboard_data(tg_user_id: str) -> dict:
    """
    Returns all data needed by the /stats dashboard in one DB pass.
    Designed to be fast (all queries use indexes).
    """
    with _db() as conn:

        # ── 1. Page views ────────────────────────────────────────────────────

        # Total all-time page views for this user's session
        total_pv = conn.execute(
            "SELECT COUNT(*) as n FROM page_views WHERE tg_user_id = ?",
            (tg_user_id,),
        ).fetchone()["n"]

        # Unique visitors (by ip_hash) across all time
        unique_v = conn.execute(
            "SELECT COUNT(DISTINCT ip_hash) as n FROM page_views WHERE tg_user_id = ?",
            (tg_user_id,),
        ).fetchone()["n"]

        # Last 30 days — daily breakdown
        daily_pv_rows = conn.execute(
            """
            SELECT date(created_at) as day, COUNT(*) as views
            FROM page_views
            WHERE tg_user_id = ?
              AND created_at >= datetime('now', '-30 days')
            GROUP BY day
            ORDER BY day ASC
            """,
            (tg_user_id,),
        ).fetchall()

        # Page-level breakdown (top 10)
        top_pages = conn.execute(
            """
            SELECT path, COUNT(*) as hits
            FROM page_views
            WHERE tg_user_id = ?
            GROUP BY path
            ORDER BY hits DESC
            LIMIT 10
            """,
            (tg_user_id,),
        ).fetchall()

        # ── 2. Publications ──────────────────────────────────────────────────

        # Total posts published
        total_posts = conn.execute(
            "SELECT COUNT(*) as n FROM publications_log WHERE tg_user_id = ? AND status = 'published'",
            (tg_user_id,),
        ).fetchone()["n"]

        # Aggregate engagement
        engagement = conn.execute(
            """
            SELECT
                COALESCE(SUM(views), 0)         as total_views,
                COALESCE(SUM(likes), 0)         as total_likes,
                COALESCE(SUM(replies_count), 0) as total_replies,
                COALESCE(SUM(reposts), 0)       as total_reposts
            FROM publications_log
            WHERE tg_user_id = ? AND status = 'published'
            """,
            (tg_user_id,),
        ).fetchone()

        # Daily posts count (last 30 days)
        daily_posts_rows = conn.execute(
            """
            SELECT date(published_at) as day, COUNT(*) as posts
            FROM publications_log
            WHERE tg_user_id = ?
              AND status = 'published'
              AND published_at >= datetime('now', '-30 days')
            GROUP BY day
            ORDER BY day ASC
            """,
            (tg_user_id,),
        ).fetchall()

        # Recent publications (last 10)
        recent_pubs = conn.execute(
            """
            SELECT
                pl.id, pl.post_text, pl.post_type, pl.status,
                pl.views, pl.likes, pl.replies_count, pl.reposts,
                pl.published_at, pl.threads_post_id,
                a.name as account_name
            FROM publications_log pl
            LEFT JOIN accounts a ON a.id = pl.account_id
            WHERE pl.tg_user_id = ?
            ORDER BY pl.published_at DESC
            LIMIT 10
            """,
            (tg_user_id,),
        ).fetchall()

        # ── 3. Posts by type ────────────────────────────────────────────────
        type_breakdown = conn.execute(
            """
            SELECT post_type, COUNT(*) as n
            FROM publications_log
            WHERE tg_user_id = ? AND status = 'published'
            GROUP BY post_type
            """,
            (tg_user_id,),
        ).fetchall()

        # ── 4. Top performing accounts ───────────────────────────────────────
        top_accounts = conn.execute(
            """
            SELECT a.name, COUNT(pl.id) as posts,
                   COALESCE(SUM(pl.likes), 0) as likes,
                   COALESCE(SUM(pl.views), 0) as views
            FROM publications_log pl
            JOIN accounts a ON a.id = pl.account_id
            WHERE pl.tg_user_id = ?
            GROUP BY a.id
            ORDER BY likes DESC
            LIMIT 5
            """,
            (tg_user_id,),
        ).fetchall()

    # ── Build padded 30-day series ────────────────────────────────────────────
    today = date.today()
    labels = [(today - timedelta(days=29 - i)).isoformat() for i in range(30)]

    pv_map = {row["day"]: row["views"] for row in daily_pv_rows}
    posts_map = {row["day"]: row["posts"] for row in daily_posts_rows}

    pv_series    = [pv_map.get(d, 0)    for d in labels]
    posts_series = [posts_map.get(d, 0) for d in labels]

    # Short labels for the chart (e.g. "Jun 1")
    chart_labels = [
        datetime.strptime(d, "%Y-%m-%d").strftime("%b %-d") for d in labels
    ]

    return {
        # Totals
        "total_pageviews":  total_pv,
        "unique_visitors":  unique_v,
        "total_posts":      total_posts,
        "total_views":      engagement["total_views"]   if engagement else 0,
        "total_likes":      engagement["total_likes"]   if engagement else 0,
        "total_replies":    engagement["total_replies"] if engagement else 0,
        "total_reposts":    engagement["total_reposts"] if engagement else 0,

        # Charts
        "chart_labels":     chart_labels,
        "pv_series":        pv_series,
        "posts_series":     posts_series,

        # Tables
        "top_pages":        [dict(r) for r in top_pages],
        "recent_pubs":      [dict(r) for r in recent_pubs],
        "type_breakdown":   [dict(r) for r in type_breakdown],
        "top_accounts":     [dict(r) for r in top_accounts],
    }
