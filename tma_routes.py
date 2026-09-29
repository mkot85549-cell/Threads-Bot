"""
Telegram Mini App (TMA) — API routes.

STATUS: experimental / WIP (see tg_bot.py for the WebApp launch button).

Authentication: Telegram WebApp initData, validated on EVERY request via the
`X-TMA-Init-Data` header. There is deliberately NO cookie fallback: a custom
header cannot be sent cross-site, so these endpoints are not CSRF-able, and a
forged cookie can never impersonate a user.

The frontend (templates/tma.html) must send the header on every fetch().
"""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import config
from db_utils import get_db
from session_auth import sub_is_active
from tma_auth import validate_init_data

router = APIRouter(prefix="/tma/api", tags=["tma"])


# ── Auth dependency ──────────────────────────────────────────────────────────

def _get_tg_user_id(request: Request) -> str:
    init_data = request.headers.get("X-TMA-Init-Data", "")
    user_info = validate_init_data(init_data) if init_data else None
    if not user_info:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return str(user_info["tg_user_id"])


# ── Models ───────────────────────────────────────────────────────────────────

class AccountUpdate(BaseModel):
    name:               Optional[str] = Field(None, max_length=100)
    proxy:              Optional[str] = Field(None, max_length=300)
    custom_prompt:      Optional[str] = Field(None, max_length=4000)
    custom_reply:       Optional[str] = Field(None, max_length=500)
    post_mode:          Optional[str] = Field(None, max_length=32)
    prompt_1:           Optional[str] = Field(None, max_length=4000)
    prompt_2:           Optional[str] = Field(None, max_length=4000)
    prompt_3:           Optional[str] = Field(None, max_length=4000)
    prompt_4:           Optional[str] = Field(None, max_length=4000)
    prompt_5:           Optional[str] = Field(None, max_length=4000)
    source_themes:      Optional[str] = Field(None, max_length=2000)
    target_theme:       Optional[str] = Field(None, max_length=500)
    mini_prompt:        Optional[str] = Field(None, max_length=4000)
    pub_delay_min:      Optional[int] = Field(None, ge=1, le=1440)
    pub_delay_max:      Optional[int] = Field(None, ge=1, le=1440)
    outreach_keywords:  Optional[str] = Field(None, max_length=2000)
    outreach_delay_min: Optional[int] = Field(None, ge=1, le=1440)
    outreach_delay_max: Optional[int] = Field(None, ge=1, le=1440)
    post_language:      Optional[str] = Field(None, pattern="^(ru|en|uk)$")
    ai_skill:           Optional[str] = Field(None, max_length=32)
    ai_temperature:     Optional[float] = Field(None, ge=0.0, le=1.0)


_TEXT_FIELDS = (
    "custom_prompt", "custom_reply",
    "prompt_1", "prompt_2", "prompt_3", "prompt_4", "prompt_5",
    "source_themes", "target_theme", "mini_prompt", "outreach_keywords",
)


# ── Dashboard data ───────────────────────────────────────────────────────────

@router.get("/dashboard")
async def tma_dashboard(request: Request):
    tg_user_id = _get_tg_user_id(request)

    with get_db() as conn:
        user_row = conn.execute(
            "SELECT sub_end_date, max_accounts, farm_enabled, listener_enabled "
            "FROM users WHERE tg_user_id = ?",
            (tg_user_id,),
        ).fetchone()

        if not user_row:
            return {
                "authenticated": True, "sub_expired": True, "accounts": [],
                "max_accounts": 0, "current_count": 0,
                "farm_active": False, "listener_active": False,
            }

        sub_expired  = not sub_is_active(user_row["sub_end_date"])
        max_accounts = user_row["max_accounts"] or 1

        accounts = []
        if not sub_expired:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE tg_user_id = ? ORDER BY id", (tg_user_id,)
            ).fetchall()
            for r in rows:
                acc = {
                    "id":                 r["id"],
                    "name":               r["name"],
                    "threads_user_id":    r["threads_user_id"],
                    "is_active":          r["is_active"] == 1,
                    "post_mode":          r["post_mode"] or "prompts",
                    "pub_delay_min":      r["pub_delay_min"] or 15,
                    "pub_delay_max":      r["pub_delay_max"] or 45,
                    "outreach_delay_min": r["outreach_delay_min"] or 10,
                    "outreach_delay_max": r["outreach_delay_max"] or 30,
                    "proxy":              r["proxy"] or None,
                    "post_language":      r["post_language"] or "ru",
                    "ai_skill":           r["ai_skill"] or "",
                    "ai_temperature":     r["ai_temperature"] if r["ai_temperature"] is not None else 0.7,
                }
                for f in _TEXT_FIELDS:
                    acc[f] = r[f] or ""
                accounts.append(acc)  # access_token is intentionally never returned

    return {
        "authenticated":   True,
        "sub_expired":     sub_expired,
        "sub_end_date":    user_row["sub_end_date"] or None,
        "max_accounts":    max_accounts,
        "current_count":   len(accounts),
        "farm_active":     (user_row["farm_enabled"] or 0) == 1,
        "listener_active": (user_row["listener_enabled"] or 0) == 1,
        "accounts":        accounts,
        "tg_bot_username": config.TG_BOT_USERNAME,
    }


# ── Toggle workers ───────────────────────────────────────────────────────────

def _toggle_flag(tg_user_id: str, column: str) -> bool:
    if column not in ("farm_enabled", "listener_enabled"):
        raise ValueError(column)
    with get_db() as conn:
        row = conn.execute(
            f"SELECT {column}, sub_end_date FROM users WHERE tg_user_id = ?", (tg_user_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        new_state = 0 if row[column] == 1 else 1
        if new_state == 1 and not sub_is_active(row["sub_end_date"]):
            raise HTTPException(403, "Subscription expired")
        conn.execute(
            f"UPDATE users SET {column} = ? WHERE tg_user_id = ?", (new_state, tg_user_id)
        )
    return new_state == 1


@router.post("/toggle_farm")
async def tma_toggle_farm(request: Request):
    return {"ok": True, "farm_active": _toggle_flag(_get_tg_user_id(request), "farm_enabled")}


@router.post("/toggle_listener")
async def tma_toggle_listener(request: Request):
    return {"ok": True, "listener_active": _toggle_flag(_get_tg_user_id(request), "listener_enabled")}


# ── Account toggle ───────────────────────────────────────────────────────────

@router.post("/toggle_account/{account_id}")
async def tma_toggle_account(account_id: int, request: Request):
    tg_user_id = _get_tg_user_id(request)
    with get_db() as conn:
        row = conn.execute(
            "SELECT is_active FROM accounts WHERE id = ? AND tg_user_id = ?",
            (account_id, tg_user_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Account not found")
        new_state = 0 if row["is_active"] == 1 else 1
        conn.execute(
            "UPDATE accounts SET is_active = ? WHERE id = ? AND tg_user_id = ?",
            (new_state, account_id, tg_user_id),
        )
    return {"ok": True, "is_active": new_state == 1}


# ── Account update ───────────────────────────────────────────────────────────

@router.post("/update_account/{account_id}")
async def tma_update_account(account_id: int, body: AccountUpdate, request: Request):
    tg_user_id = _get_tg_user_id(request)
    # Keys come from the pydantic model only, so they are safe to use as column names.
    update_data = body.model_dump(exclude_none=True)
    if not update_data:
        return {"ok": True, "message": "No changes"}

    with get_db() as conn:
        cur = conn.execute(
            "SELECT pub_delay_min, pub_delay_max, outreach_delay_min, outreach_delay_max "
            "FROM accounts WHERE id = ? AND tg_user_id = ?",
            (account_id, tg_user_id),
        ).fetchone()
        if not cur:
            raise HTTPException(403, "Access denied")

        # min < max must hold for the merged result, not just for the submitted fields
        for lo, hi in (("pub_delay_min", "pub_delay_max"),
                       ("outreach_delay_min", "outreach_delay_max")):
            lo_v = update_data.get(lo, cur[lo])
            hi_v = update_data.get(hi, cur[hi])
            if lo_v is not None and hi_v is not None and lo_v >= hi_v:
                raise HTTPException(422, f"{lo} must be less than {hi}")

        fields = [f"{k} = ?" for k in update_data]
        values = list(update_data.values()) + [account_id, tg_user_id]
        conn.execute(
            f"UPDATE accounts SET {', '.join(fields)} WHERE id = ? AND tg_user_id = ?",
            values,
        )

    return {"ok": True}


# ── Delete account ───────────────────────────────────────────────────────────

@router.post("/delete_account/{account_id}")
async def tma_delete_account(account_id: int, request: Request):
    tg_user_id = _get_tg_user_id(request)
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM accounts WHERE id = ? AND tg_user_id = ?",
            (account_id, tg_user_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Account not found")
    return {"ok": True}


# ── Auth validate (called once when the Mini App opens) ──────────────────────

@router.post("/auth")
async def tma_auth(request: Request):
    """Validates initData and makes sure a users row exists. Sets no cookie."""
    init_data = request.headers.get("X-TMA-Init-Data", "")
    user_info = validate_init_data(init_data) if init_data else None
    if not user_info:
        raise HTTPException(401, "Invalid initData")

    tg_user_id = str(user_info["tg_user_id"])

    with get_db() as conn:
        row = conn.execute(
            "SELECT tg_user_id FROM users WHERE tg_user_id = ?", (tg_user_id,)
        ).fetchone()
        if not row:
            expired = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """INSERT INTO users
                   (tg_user_id, username, sub_end_date, max_accounts, farm_enabled, listener_enabled)
                   VALUES (?, ?, ?, 0, 0, 0)""",
                (tg_user_id, user_info.get("username") or "", expired),
            )

    return {"ok": True, "user": user_info}