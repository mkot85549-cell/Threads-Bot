# stats_routes.py — analytics routes (already registered in web_app.py).
# The page-view middleware now lives in web_app.py, so no integration steps remain.

import json
import threading
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from analytics_service import get_dashboard_data, log_publication, refresh_insights_for_user
from db_utils import get_db
from session_auth import current_user

router = APIRouter()
templates = Jinja2Templates(directory="templates")

_REFRESH_COOLDOWN = 60  # seconds between manual refreshes per user
_last_refresh: dict[str, float] = {}
_refresh_lock = threading.Lock()


# ── /stats — main dashboard page ─────────────────────────────────────────────

@router.get("/stats", response_class=HTMLResponse)
async def stats_dashboard(
    request:    Request,
    tg_user_id: Optional[str] = Depends(current_user),
):
    if not tg_user_id:
        return RedirectResponse(url="/", status_code=303)

    data = await run_in_threadpool(get_dashboard_data, tg_user_id)

    farm_active = False
    listener_active = False
    with get_db() as conn:
        user_row = conn.execute(
            "SELECT farm_enabled, listener_enabled FROM users WHERE tg_user_id = ?",
            (tg_user_id,),
        ).fetchone()
        if user_row:
            farm_active     = (user_row["farm_enabled"]    or 0) == 1
            listener_active = (user_row["listener_enabled"] or 0) == 1

    return templates.TemplateResponse(
        request=request,
        name="stats.html",
        context={
            "request":         request,
            "tg_user_id":      tg_user_id,
            "farm_active":     farm_active,
            "listener_active": listener_active,
            **data,
            "chart_labels_json":   json.dumps(data["chart_labels"]),
            "pv_series_json":      json.dumps(data["pv_series"]),
            "posts_series_json":   json.dumps(data["posts_series"]),
            "type_breakdown_json": json.dumps([
                {"label": r["post_type"], "value": r["n"]}
                for r in data["type_breakdown"]
            ]),
        },
    )


# ── /api/stats/data — JSON endpoint for live refresh ─────────────────────────

@router.get("/api/stats/data")
async def stats_data_api(tg_user_id: Optional[str] = Depends(current_user)):
    if not tg_user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = await run_in_threadpool(get_dashboard_data, tg_user_id)
    return JSONResponse(data)


# ── /api/stats/refresh — trigger Threads insights sync ───────────────────────

@router.post("/api/stats/refresh")
async def refresh_insights(tg_user_id: Optional[str] = Depends(current_user)):
    if not tg_user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Cooldown: without it every click spawns another thread hammering the Threads API
    now = time.time()
    with _refresh_lock:
        if now - _last_refresh.get(tg_user_id, 0) < _REFRESH_COOLDOWN:
            return JSONResponse({"status": "too_soon"}, status_code=429)
        _last_refresh[tg_user_id] = now

    def _bg_refresh(uid: str):
        try:
            n = refresh_insights_for_user(uid)
            print(f"[stats] refreshed {n} insights for user {uid}")
        except Exception as exc:
            print(f"[stats] refresh error: {exc}")

    threading.Thread(target=_bg_refresh, args=(tg_user_id,), daemon=True).start()
    return JSONResponse({"status": "refresh_started"})


# ── /api/stats/log_pub — log a publication (session-authenticated) ───────────

@router.post("/api/stats/log_pub")
async def log_pub_endpoint(
    request:    Request,
    tg_user_id: Optional[str] = Depends(current_user),
):
    """
    JSON body: {account_id, threads_post_id, post_text, post_type, status}
    Worker threads should call analytics_service.log_publication() directly.
    """
    if not tg_user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        body = await request.json()
        account_id = int(body.get("account_id"))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=422, detail="Invalid body / account_id")

    # The account must belong to the caller — otherwise anyone could pollute
    # another user's statistics.
    with get_db() as conn:
        owns = conn.execute(
            "SELECT 1 FROM accounts WHERE id = ? AND tg_user_id = ?",
            (account_id, tg_user_id),
        ).fetchone()
    if not owns:
        raise HTTPException(status_code=403, detail="Access denied")

    await run_in_threadpool(
        log_publication,
        account_id=account_id,
        tg_user_id=tg_user_id,
        threads_post_id=body.get("threads_post_id"),
        post_text=str(body.get("post_text", ""))[:500],
        post_type=str(body.get("post_type", "post"))[:32],
        status=str(body.get("status", "published"))[:32],
    )
    return JSONResponse({"status": "logged"})