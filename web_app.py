# web_app.py — FastAPI web dashboard, REST API, and background worker supervisor
# Signed-session auth (magic link), account management, analytics, TMA routes.
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import httpx
from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

import config
import farm_manager
import listener
import outreach_bot
import token_refresher
from analytics_service import _insights_scheduler_loop, record_view
from db_utils import get_db, init_db
from feedback_analyzer import analyzer_scheduler_loop
from session_auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    current_first_name,
    current_user,
    make_session,
    read_session,
    sub_is_active,
)
from stats_routes import router as stats_router
from tma_routes import router as tma_router

# Set COOKIE_SECURE=0 in .env only for local http:// development.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") != "0"
# Set TRUST_PROXY=0 if the app is NOT behind nginx/caddy (X-Forwarded-For is spoofable then).
TRUST_PROXY = os.environ.get("TRUST_PROXY", "1") == "1"

_worker_lock_fh = None


def _acquire_worker_lock() -> bool:
    """
    Only ONE process may run the background workers. With several uvicorn
    workers each would start its own farm loop and publish duplicates.
    """
    global _worker_lock_fh
    try:
        import fcntl
    except ImportError:  # Windows: no lock available
        return True
    fh = open("workers.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _worker_lock_fh = fh  # keep the handle open for the process lifetime
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Starts background workers on startup; stops them gracefully on shutdown."""
    # TG_BOT_TOKEN is needed to validate Mini App initData (an empty key would be forgeable)
    config.require("ANTHROPIC_API_KEY", "THREADS_APP_ID", "THREADS_APP_SECRET", "TG_BOT_TOKEN")
    init_db()

    started = False
    if _acquire_worker_lock():
        targets = [
            (farm_manager.run_farm_loop,        "farm-dispatcher"),
            (listener.check_and_reply,          "listener"),
            (outreach_bot.run_outreach_loop,    "outreach-dispatcher"),
            (_insights_scheduler_loop,          "insights-scheduler"),
            (analyzer_scheduler_loop,           "feedback-analyzer"),
            (token_refresher.token_refresh_loop, "token-refresher"),
        ]
        for fn, name in targets:
            threading.Thread(target=fn, daemon=True, name=name).start()
        started = True
        print(f"✅ {len(targets)} background workers started")
    else:
        print("ℹ️ Another process already runs the workers — this one serves HTTP only.")

    yield

    if started:
        farm_manager.stop_event.set()
        outreach_bot.stop_event.set()
        listener.stop_event.set()
        token_refresher.stop_event.set()
        print("🛑 Stop signal sent to all workers.")


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

os.makedirs("static/uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(stats_router)
app.include_router(tma_router)


# ── Page-view tracking ───────────────────────────────────────────────────────

_SKIP_PREFIXES = ("/api/", "/tma/api/", "/static/", "/auth/", "/logout", "/favicon")


def _client_ip(request: Request) -> str:
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


@app.middleware("http")
async def track_pageviews(request: Request, call_next):
    response = await call_next(request)
    try:
        if (
            request.method == "GET"
            and response.status_code < 400
            and not request.url.path.startswith(_SKIP_PREFIXES)
            and "text/html" in response.headers.get("content-type", "")
        ):
            sess = read_session(request.cookies.get(SESSION_COOKIE))
            # SQLite write goes to a thread so the event loop is never blocked
            await run_in_threadpool(
                record_view,
                request.url.path,
                tg_user_id=sess["uid"] if sess else None,
                client_ip=_client_ip(request),
                user_agent=request.headers.get("user-agent", ""),
                referer=request.headers.get("referer", ""),
            )
    except Exception as exc:  # analytics must never break a page
        print(f"[analytics] middleware error: {exc}")
    return response


# ── Auth (magic link) ────────────────────────────────────────────────────────

MAGIC_LINK_MAX_AGE_SECONDS = 300


@app.get("/auth/magic")
async def auth_magic(data: str, sign: str):
    expected = hmac.new(
        config.SECRET_AUTH_KEY.encode(), data.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(sign.encode(), expected.encode()):
        return HTMLResponse("❌ Auth error: invalid signature.", status_code=403)

    # Format: "<tg_id>:<first_name>:<timestamp>". The name may itself contain ':'
    # so split the id off the left and the timestamp off the right.
    try:
        tg_id, rest = data.split(":", 1)
        first_name, timestamp_str = rest.rsplit(":", 1)
        if not tg_id.lstrip("-").isdigit():
            raise ValueError
        link_age = int(time.time()) - int(timestamp_str)
    except (ValueError, TypeError):
        return HTMLResponse("❌ Malformed link.", status_code=403)

    if link_age > MAGIC_LINK_MAX_AGE_SECONDS or link_age < -60:
        return HTMLResponse(
            f"❌ Link expired ({max(link_age, 0) // 60} min old). Request a new one via /start.",
            status_code=403,
        )

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=make_session(tg_id, first_name),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
    )
    # Drop the legacy, unsigned cookies
    response.delete_cookie("tg_user_id")
    response.delete_cookie("tg_first_name")
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie("tg_user_id")
    response.delete_cookie("tg_first_name")
    return response


# ── TMA (Telegram Mini App) page ─────────────────────────────────────────────

@app.get("/app", response_class=HTMLResponse)
async def tma_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="tma.html",
        context={"tg_bot_username": config.TG_BOT_USERNAME, "request": request},
    )


# ── Threads OAuth ─────────────────────────────────────────────────────────────

def _oauth_redirect(url: str) -> RedirectResponse:
    resp = RedirectResponse(url=url, status_code=303)
    resp.delete_cookie("oauth_state")
    return resp


@app.get("/auth/threads/login")
async def threads_login(tg_user_id: Optional[str] = Depends(current_user)):
    if not tg_user_id:
        return RedirectResponse(url="/")
    state = secrets.token_urlsafe(32)
    scopes = "threads_basic,threads_content_publish,threads_manage_insights"
    if config.THREADS_KEYWORD_SEARCH:
        scopes += ",threads_keyword_search"
    params = urllib.parse.urlencode({
        "client_id":     config.THREADS_APP_ID,
        "redirect_uri":  config.REDIRECT_URI,
        "scope":         scopes,
        "response_type": "code",
        "state":         state,
    })
    resp = RedirectResponse(url=f"https://threads.net/oauth/authorize?{params}")
    resp.set_cookie("oauth_state", state, max_age=600, httponly=True,
                    secure=COOKIE_SECURE, samesite="lax")
    return resp


@app.get("/auth/threads/callback")
async def threads_callback(
    request:     Request,
    code:        Optional[str] = None,
    error:       Optional[str] = None,
    state:       Optional[str] = None,
    tg_user_id:  Optional[str] = Depends(current_user),
    oauth_state: Optional[str] = Cookie(None),
):
    if not tg_user_id:
        return RedirectResponse(url="/")

    if error or not code:
        return _oauth_redirect("/?status=denied")

    if not state or not oauth_state or not hmac.compare_digest(state, oauth_state):
        return HTMLResponse(
            "❌ OAuth state mismatch — possible CSRF attempt. Please try again via /auth/threads/login.",
            status_code=403,
        )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            token_resp = await client.post(
                "https://graph.threads.net/oauth/access_token",
                data={
                    "client_id":     config.THREADS_APP_ID,
                    "client_secret": config.THREADS_APP_SECRET,
                    "redirect_uri":  config.REDIRECT_URI,
                    "code":          code,
                    "grant_type":    "authorization_code",
                },
            )
            token_resp.raise_for_status()
            short_token = token_resp.json()["access_token"]

            ll_resp = await client.get(
                "https://graph.threads.net/access_token",
                params={
                    "grant_type":    "th_exchange_token",
                    "client_secret": config.THREADS_APP_SECRET,
                    "access_token":  short_token,
                },
            )
            ll_resp.raise_for_status()
            long_token = ll_resp.json()["access_token"]

            me_resp = await client.get(
                "https://graph.threads.net/v1.0/me",
                params={"fields": "id,username", "access_token": long_token},
            )
            me_resp.raise_for_status()
            me = me_resp.json()
            threads_user_id = me["id"]
            username        = me.get("username", threads_user_id)

    except httpx.HTTPStatusError as exc:
        print(f"[OAuth] Meta API error {exc.response.status_code}: {exc.response.text[:200]}")
        return _oauth_redirect("/?status=oauth_error")
    except (httpx.RequestError, KeyError) as exc:
        print(f"[OAuth] Network/parse error: {exc}")
        return _oauth_redirect("/?status=oauth_error")

    with get_db() as conn:
        user_row = conn.execute(
            "SELECT sub_end_date, max_accounts FROM users WHERE tg_user_id = ?",
            (tg_user_id,),
        ).fetchone()
        if not user_row or not sub_is_active(user_row["sub_end_date"]):
            return _oauth_redirect("/?status=sub_expired")
        max_accs = user_row["max_accounts"] or 1

        existing = conn.execute(
            "SELECT id, tg_user_id FROM accounts WHERE threads_user_id = ?",
            (threads_user_id,),
        ).fetchone()

        if existing:
            owner = existing["tg_user_id"]
            if owner and owner != tg_user_id:
                # Never let one user overwrite another user's account/token
                return _oauth_redirect("/?status=account_taken")
            # Re-authorisation of the user's own account: refresh token, ignore the limit
            conn.execute(
                "UPDATE accounts SET access_token = ?, name = ?, tg_user_id = ?, "
                "token_updated_at = datetime('now'), "
                "is_active = CASE WHEN token_status = 'revoked' THEN 1 ELSE is_active END, "
                "token_status = 'ok' WHERE id = ?",
                (long_token, username, tg_user_id, existing["id"]),
            )
            acc_id = existing["id"]
        else:
            cur_accs = conn.execute(
                "SELECT COUNT(*) AS cnt FROM accounts WHERE tg_user_id = ?", (tg_user_id,)
            ).fetchone()["cnt"]
            if cur_accs >= max_accs:
                return _oauth_redirect("/?status=limit_reached")
            cur = conn.execute(
                """
                INSERT INTO accounts (name, threads_user_id, access_token, tg_user_id, is_active,
                                      token_updated_at, token_status)
                VALUES (?, ?, ?, ?, 1, datetime('now'), 'ok')
                """,
                (username, threads_user_id, long_token, tg_user_id),
            )
            acc_id = cur.lastrowid

    return _oauth_redirect(f"/?status=connected&configure={acc_id}")


# ── Dashboard ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def read_dashboard(
    request:       Request,
    tg_user_id:    Optional[str] = Depends(current_user),
    tg_first_name: str           = Depends(current_first_name),
):
    accounts             = []
    sub_expired          = False
    max_accounts         = 0
    current_count        = 0
    user_farm_active     = 0
    user_listener_active = 0
    configure_account_id = None
    configure_account    = None

    if tg_user_id:
        with get_db() as conn:
            user_row = conn.execute(
                "SELECT sub_end_date, max_accounts, farm_enabled, listener_enabled "
                "FROM users WHERE tg_user_id = ?",
                (tg_user_id,),
            ).fetchone()

            if user_row:
                user_farm_active     = user_row["farm_enabled"]     or 0
                user_listener_active = user_row["listener_enabled"] or 0
                max_accounts = user_row["max_accounts"] or 1
                sub_expired  = not sub_is_active(user_row["sub_end_date"])
            else:
                sub_expired = True

            if not sub_expired:
                accounts = conn.execute(
                    "SELECT * FROM accounts WHERE tg_user_id = ?", (tg_user_id,)
                ).fetchall()
                current_count = len(accounts)

                configure_param = request.query_params.get("configure")
                if configure_param and configure_param.isdigit():
                    row = conn.execute(
                        "SELECT * FROM accounts WHERE id = ? AND tg_user_id = ?",
                        (int(configure_param), tg_user_id),
                    ).fetchone()
                    if row:
                        configure_account_id = int(configure_param)
                        configure_account    = row

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request":              request,
            "accounts":             accounts,
            "tg_user_id":           tg_user_id,
            "tg_first_name":        tg_first_name,
            "tg_bot_username":      config.TG_BOT_USERNAME,
            "sub_expired":          sub_expired,
            "max_accounts":         max_accounts,
            "current_count":        current_count,
            "limit_reached":        current_count >= max_accounts,
            "farm_active":          user_farm_active     == 1,
            "listener_active":      user_listener_active == 1,
            "status":               request.query_params.get("status", ""),
            "configure_account_id": configure_account_id,
            "configure_account":    configure_account,
        },
    )


# ── Account management ───────────────────────────────────────────────────────

@app.get("/edit_account/{account_id}", response_class=HTMLResponse)
async def edit_account_form(
    request:    Request,
    account_id: int,
    tg_user_id: Optional[str] = Depends(current_user),
):
    if not tg_user_id:
        return RedirectResponse(url="/", status_code=303)

    with get_db() as conn:
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id = ? AND tg_user_id = ?",
            (account_id, tg_user_id),
        ).fetchone()

    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    return templates.TemplateResponse(
        request=request,
        name="edit.html",
        context={"request": request, "acc": acc},
    )


def _form_error(message: str, status: int = 422) -> HTMLResponse:
    return HTMLResponse(
        f"<h3 style='color:red;text-align:center;margin-top:50px;'>❌ {message}</h3>"
        "<p style='text-align:center'><a href='javascript:history.back()'>← Back</a></p>",
        status_code=status,
    )


@app.post("/update_account/{account_id}")
async def update_account(
    request:            Request,
    account_id:         int,
    name:               str           = Form(...),
    threads_user_id:    str           = Form(...),
    access_token:       str           = Form(...),
    proxy:              Optional[str] = Form(None),
    custom_prompt:      Optional[str] = Form(None),
    custom_reply:       Optional[str] = Form(None),
    post_mode:          str           = Form("prompts"),
    prompt_1:           Optional[str] = Form(None),
    prompt_2:           Optional[str] = Form(None),
    prompt_3:           Optional[str] = Form(None),
    prompt_4:           Optional[str] = Form(None),
    prompt_5:           Optional[str] = Form(None),
    source_themes:      Optional[str] = Form(None),
    target_theme:       Optional[str] = Form(None),
    mini_prompt:        Optional[str] = Form(None),
    pub_delay_min:      int           = Form(15),
    pub_delay_max:      int           = Form(45),
    outreach_keywords:  Optional[str] = Form(None),
    outreach_delay_min: int           = Form(10),
    outreach_delay_max: int           = Form(30),
    post_language:      str           = Form("ru"),
    ai_skill:           str           = Form(""),
    ai_temperature:     float         = Form(0.7),
    tg_user_id:         Optional[str] = Depends(current_user),
):
    if not tg_user_id:
        return RedirectResponse(url="/", status_code=303)

    if not (1 <= pub_delay_min < pub_delay_max):
        return _form_error("Publish delay: min must be ≥ 1 and less than max.")
    if not (1 <= outreach_delay_min < outreach_delay_max):
        return _form_error("Outreach delay: min must be ≥ 1 and less than max.")

    if post_language not in ("ru", "en", "uk"):
        post_language = "ru"
    ai_temperature = max(0.0, min(1.0, ai_temperature))

    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM accounts WHERE id = ? AND tg_user_id = ?",
                (account_id, tg_user_id),
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=403, detail="Access denied")

            conn.execute("""
                UPDATE accounts SET
                    name = ?, threads_user_id = ?, access_token = ?, proxy = ?,
                    custom_prompt = ?, custom_reply = ?, post_mode = ?,
                    prompt_1 = ?, prompt_2 = ?, prompt_3 = ?, prompt_4 = ?, prompt_5 = ?,
                    source_themes = ?, target_theme = ?, mini_prompt = ?,
                    pub_delay_min = ?, pub_delay_max = ?,
                    outreach_keywords = ?, outreach_delay_min = ?, outreach_delay_max = ?,
                    post_language = ?, ai_skill = ?, ai_temperature = ?,
                    token_updated_at = CASE WHEN access_token != ? THEN datetime('now') ELSE token_updated_at END,
                    token_status = CASE WHEN access_token != ? THEN 'ok' ELSE token_status END
                WHERE id = ? AND tg_user_id = ?
            """, (
                name, threads_user_id, access_token, proxy,
                custom_prompt, custom_reply, post_mode,
                prompt_1, prompt_2, prompt_3, prompt_4, prompt_5,
                source_themes, target_theme, mini_prompt,
                pub_delay_min, pub_delay_max,
                outreach_keywords, outreach_delay_min, outreach_delay_max,
                post_language, ai_skill, ai_temperature,
                access_token, access_token,
                account_id, tg_user_id,
            ))
    except sqlite3.IntegrityError:
        # threads_user_id is UNIQUE — it already belongs to another account
        return _form_error("This Threads user ID is already connected to another account.", 409)

    return RedirectResponse(url="/", status_code=303)


@app.post("/toggle_account/{account_id}")
async def toggle_account(
    account_id: int,
    tg_user_id: Optional[str] = Depends(current_user),
):
    if not tg_user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    with get_db() as conn:
        row = conn.execute(
            "SELECT is_active FROM accounts WHERE id = ? AND tg_user_id = ?",
            (account_id, tg_user_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Account not found")

        new_state = 0 if row["is_active"] == 1 else 1
        conn.execute(
            "UPDATE accounts SET is_active = ? WHERE id = ? AND tg_user_id = ?",
            (new_state, account_id, tg_user_id),
        )

    return JSONResponse({"new_state": new_state})


@app.post("/delete_account/{account_id}")
async def delete_account(
    account_id: int,
    tg_user_id: Optional[str] = Depends(current_user),
):
    if not tg_user_id:
        return RedirectResponse(url="/", status_code=303)

    with get_db() as conn:
        conn.execute(
            "DELETE FROM accounts WHERE id = ? AND tg_user_id = ?",
            (account_id, tg_user_id),
        )

    return RedirectResponse(url="/", status_code=303)


# ── Worker control ───────────────────────────────────────────────────────────

def _toggle_user_flag(tg_user_id: str, column: str) -> None:
    if column not in ("farm_enabled", "listener_enabled"):  # column name is interpolated
        raise ValueError(column)
    with get_db() as conn:
        row = conn.execute(
            f"SELECT {column}, sub_end_date FROM users WHERE tg_user_id = ?", (tg_user_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        new_state = 0 if row[column] == 1 else 1
        if new_state == 1 and not sub_is_active(row["sub_end_date"]):
            raise HTTPException(status_code=403, detail="Subscription expired")

        conn.execute(
            f"UPDATE users SET {column} = ? WHERE tg_user_id = ?",
            (new_state, tg_user_id),
        )


@app.post("/start_farm")
async def start_farm(tg_user_id: Optional[str] = Depends(current_user)):
    if not tg_user_id:
        return RedirectResponse(url="/", status_code=303)
    _toggle_user_flag(tg_user_id, "farm_enabled")
    return RedirectResponse(url="/", status_code=303)


@app.post("/start_listener")
async def start_listener(tg_user_id: Optional[str] = Depends(current_user)):
    if not tg_user_id:
        return RedirectResponse(url="/", status_code=303)
    _toggle_user_flag(tg_user_id, "listener_enabled")
    return RedirectResponse(url="/", status_code=303)