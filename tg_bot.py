import asyncio
import hashlib
import hmac
import html
import logging
import secrets
import sqlite3
import string
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

from aiocryptopay import AioCryptoPay, Networks
from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
from db_utils import get_db
from schema import apply_schema

# The bot needs only these two (Threads OAuth / Anthropic keys are NOT required here).
config.require("TG_BOT_TOKEN", "CRYPTO_PAY_TOKEN")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("payment_debug.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

bot = Bot(token=config.TG_BOT_TOKEN)
dp = Dispatcher()
crypto = AioCryptoPay(token=config.CRYPTO_PAY_TOKEN, network=Networks.MAIN_NET)

GUIDE_URL = " "
TS_FMT = "%Y-%m-%d %H:%M:%S"

# ── Tariffs: single source for buttons, invoices and crediting ───────────────
TARIFFS = {
    "15_2":  {"price": 15, "accounts": 2,  "days": 30},
    "30_5":  {"price": 30, "accounts": 5,  "days": 30},
    "50_10": {"price": 50, "accounts": 10, "days": 30},
}
INVOICE_TTL_SECONDS = 3600

# ── Administrators ───────────────────────────────────────────────────────────
ADMIN_IDS: set[int] = { }


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ==============================================================================
# Mandatory channel subscription gate (applies to EVERY handler, see middleware)
# ==============================================================================
# The bot must be an Administrator of the channel, otherwise getChatMember fails.
REQUIRED_CHANNEL_ID = " "
REQUIRED_CHANNEL_URL = " "

_SUB_CACHE_TTL = 60
_sub_cache: dict[int, float] = {}  # user_id -> expiry of a positive check


async def is_subscribed(user_id: int) -> bool:
    """
    creator/administrator/member → True; restricted → True only if still a member;
    left/kicked → False.

    On API failure (e.g. the bot lost admin rights) this FAILS OPEN: blocking every
    paying customer because of a channel misconfiguration is worse than letting
    non-subscribers through for a while. The error is logged loudly.
    """
    if not REQUIRED_CHANNEL_ID or "PLACEHOLDER" in REQUIRED_CHANNEL_ID:
        return True
    if _sub_cache.get(user_id, 0) > time.time():
        return True

    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL_ID, user_id=user_id)
    except Exception as e:
        logger.error(f"[Subscription] getChatMember failed for user={user_id}: {e}. "
                     f"Is the bot an admin of {REQUIRED_CHANNEL_ID}? Allowing access.")
        return True

    status = member.status.value if hasattr(member.status, "value") else str(member.status)
    if status in ("creator", "administrator", "member"):
        ok = True
    elif status == "restricted":
        ok = bool(getattr(member, "is_member", False))
    else:
        ok = False

    if ok:
        _sub_cache[user_id] = time.time() + _SUB_CACHE_TTL
    return ok


def _subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=REQUIRED_CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscription")],
    ])


async def _send_subscription_gate(message: Message):
    await message.answer(
        "🔒 <b>Доступ только для подписчиков канала.</b>\n\n"
        "Чтобы пользоваться ботом, подпишись на наш канал и нажми «Я подписался».",
        reply_markup=_subscription_keyboard(),
        parse_mode="HTML",
    )


class SubscriptionGate(BaseMiddleware):
    """Blocks every message/callback (except the re-check button) for non-subscribers."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or _is_admin(user.id):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "check_subscription":
            return await handler(event, data)

        if await is_subscribed(user.id):
            return await handler(event, data)

        if isinstance(event, Message):
            await _send_subscription_gate(event)
        elif isinstance(event, CallbackQuery):
            await event.answer("🔒 Сначала подпишись на канал — отправь /start.", show_alert=True)
        return None


dp.message.middleware(SubscriptionGate())
dp.callback_query.middleware(SubscriptionGate())


# ==============================================================================
# Users / subscriptions
# ==============================================================================

def _parse_ts(value) -> Optional[datetime]:
    try:
        return datetime.strptime(value, TS_FMT) if value else None
    except (ValueError, TypeError):
        return None


def _apply_subscription(conn, tg_id: str, username: Optional[str],
                        add_days: int, max_accs: int) -> datetime:
    """
    Extends (or creates) a subscription INSIDE the caller's transaction, so that
    crediting and the thing that triggered it (invoice / promo use) commit together.

    Tier rule: while the current subscription is still active the account limit
    never goes DOWN (a 2-account promo cannot shrink a 10-account plan). Once the
    subscription has expired the new tier applies as-is.
    """
    now = datetime.now()
    row = conn.execute(
        "SELECT sub_end_date, max_accounts FROM users WHERE tg_user_id = ?", (tg_id,)
    ).fetchone()

    if row:
        current_end = _parse_ts(row[0]) or now
        still_active = current_end > now
        new_end = max(current_end, now) + timedelta(days=add_days)
        new_max = max(row[1] or 0, max_accs) if still_active else max_accs
        conn.execute(
            "UPDATE users SET username = COALESCE(?, username), sub_end_date = ?, "
            "max_accounts = ? WHERE tg_user_id = ?",
            (username, new_end.strftime(TS_FMT), new_max, tg_id),
        )
    else:
        new_end = now + timedelta(days=add_days)
        conn.execute(
            "INSERT INTO users (tg_user_id, username, sub_end_date, max_accounts, "
            "farm_enabled, listener_enabled) VALUES (?, ?, ?, ?, 0, 0)",
            (tg_id, username, new_end.strftime(TS_FMT), max_accs),
        )
    return new_end


def add_or_update_user(tg_id, username, add_days=0, max_accs=1) -> datetime:
    with get_db(row_factory=False) as conn:
        return _apply_subscription(conn, str(tg_id), username, add_days, max_accs)


def generate_magic_link(tg_id, first_name):
    data = f"{tg_id}:{first_name}:{int(time.time())}"
    signature = hmac.new(config.SECRET_AUTH_KEY.encode(), data.encode(), hashlib.sha256).hexdigest()
    return f"{config.BASE_URL}/auth/magic?{urlencode({'data': data, 'sign': signature})}"


def _save_menu_message(tg_user_id: str, message_id: int):
    with get_db(row_factory=False) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_menu_messages (tg_user_id, message_id) VALUES (?, ?)",
            (str(tg_user_id), message_id),
        )


# ==============================================================================
# Promo codes
# ==============================================================================
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def _generate_code(prefix: str = "TRIAL") -> str:
    suffix = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{prefix}-{suffix}"


def _create_promo(code: str, days: int, max_accounts: int, max_uses: int = 1,
                  expires_days: Optional[int] = None, created_by: Optional[str] = None) -> bool:
    """Returns False if a promo with this code already exists (never overwrites)."""
    expires_at = None
    if expires_days:
        expires_at = (datetime.now() + timedelta(days=expires_days)).strftime(TS_FMT)
    try:
        with get_db(row_factory=False) as conn:
            conn.execute(
                "INSERT INTO promo_codes (code, days, max_accounts, max_uses, uses_left, "
                "expires_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (code, days, max_accounts, max_uses, max_uses, expires_at, created_by),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def _redeem_promo(code: str, tg_user_id: str, username: Optional[str]) -> dict:
    """
    Activates a promo code ATOMICALLY: one transaction covers the use-counter,
    the per-user record and the subscription extension.

    • `UPDATE ... WHERE uses_left > 0` + rowcount makes concurrent redemptions safe.
    • UNIQUE(code, tg_user_id) blocks double redemption by the same user.
    """
    code = code.upper().strip()
    with get_db(row_factory=False) as conn:
        row = conn.execute(
            "SELECT days, max_accounts, expires_at FROM promo_codes WHERE code = ?", (code,)
        ).fetchone()
        if not row:
            return {"ok": False, "reason": "not_found"}
        days, max_accounts, expires_at = row

        exp = _parse_ts(expires_at)
        if exp and datetime.now() > exp:
            return {"ok": False, "reason": "expired"}

        if conn.execute(
            "SELECT 1 FROM promo_uses WHERE code = ? AND tg_user_id = ?", (code, tg_user_id)
        ).fetchone():
            return {"ok": False, "reason": "already_used"}

        cur = conn.execute(
            "UPDATE promo_codes SET uses_left = uses_left - 1 WHERE code = ? AND uses_left > 0",
            (code,),
        )
        if cur.rowcount == 0:
            return {"ok": False, "reason": "used_up"}

        try:
            conn.execute(
                "INSERT INTO promo_uses (code, tg_user_id) VALUES (?, ?)", (code, tg_user_id)
            )
        except sqlite3.IntegrityError:  # lost a race with ourselves
            conn.rollback()
            return {"ok": False, "reason": "already_used"}

        new_end = _apply_subscription(conn, tg_user_id, username, days, max_accounts)

    return {"ok": True, "days": days, "max_accounts": max_accounts, "new_end": new_end}


# Brute-force guard for /code: max failed attempts per user per window
_PROMO_WINDOW = 600
_PROMO_MAX_FAILS = 5
_promo_fails: dict[int, list] = {}


def _promo_blocked(user_id: int) -> bool:
    cutoff = time.time() - _PROMO_WINDOW
    fails = [t for t in _promo_fails.get(user_id, []) if t > cutoff]
    _promo_fails[user_id] = fails
    return len(fails) >= _PROMO_MAX_FAILS


def _promo_register_fail(user_id: int) -> None:
    _promo_fails.setdefault(user_id, []).append(time.time())


# ==============================================================================
# Admin commands
# ==============================================================================

def _int_arg(args: list, idx: int, default, lo: int, hi: int) -> int:
    """Parses args[idx] as int within [lo, hi]; raises ValueError otherwise."""
    if len(args) <= idx:
        return default
    value = int(args[idx])
    if not lo <= value <= hi:
        raise ValueError
    return value


@dp.message(Command("mk_boss"))
async def admin_cmd(message: Message):
    if not _is_admin(message.from_user.id):
        return
    new_date = add_or_update_user(
        str(message.from_user.id), message.from_user.username, add_days=3650, max_accs=100
    )
    await message.answer(
        f"👑 <b>Режим БОССА активирован!</b>\n\n"
        f"Лимит: 100 аккаунтов.\n"
        f"Действует до {new_date.strftime('%Y')} года.\n\n"
        f"Нажмите /start, чтобы открыть меню.",
        parse_mode="HTML",
    )


@dp.message(Command("trial"))
async def trial_cmd(message: Message):
    if not _is_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 3:
        await message.answer(
            "<b>📋 Использование:</b>\n"
            "<code>/trial &lt;tg_id&gt; &lt;дней&gt; [аккаунтов]</code>\n\n"
            "<code>/trial 987654321 7</code> — 7 дней, 2 акк.\n"
            "<code>/trial 987654321 14 5</code> — 14 дней, 5 акк.",
            parse_mode="HTML",
        )
        return

    target_id = args[1].strip()
    if not target_id.lstrip("-").isdigit():
        await message.answer("❌ Некорректный Telegram ID.")
        return
    try:
        days = _int_arg(args, 2, None, 1, 365)
    except ValueError:
        await message.answer("❌ Дней: от 1 до 365.")
        return
    try:
        max_accs = _int_arg(args, 3, 2, 1, 20)
    except ValueError:
        await message.answer("❌ Аккаунтов: от 1 до 20.")
        return

    with get_db(row_factory=False) as conn:
        existing = conn.execute(
            "SELECT username FROM users WHERE tg_user_id = ?", (target_id,)
        ).fetchone()

    new_date = add_or_update_user(
        tg_id=target_id,
        username=existing[0] if existing else None,
        add_days=days,
        max_accs=max_accs,
    )
    status = "продлена" if existing else "создана"
    await message.answer(
        f"✅ <b>Подписка {status}!</b>\n\n"
        f"👤 Пользователь: <code>{html.escape(target_id)}</code>\n"
        f"📅 До: <b>{new_date.strftime('%d.%m.%Y')}</b>\n"
        f"📊 Аккаунтов: <b>{max_accs}</b> (не меньше текущего лимита)\n"
        f"⏳ Дней: <b>{days}</b>",
        parse_mode="HTML",
    )


@dp.message(Command("gencode"))
async def gencode_cmd(message: Message):
    if not _is_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 3:
        await message.answer(
            "<b>📋 Создать промокод:</b>\n"
            "<code>/gencode &lt;дней&gt; &lt;акк&gt; [использований] [срок_дней] [КОД]</code>\n\n"
            "<b>Примеры:</b>\n"
            "<code>/gencode 7 2</code> — одноразовый, 7 дней\n"
            "<code>/gencode 14 3 10</code> — 10 использований, 14 дней\n"
            "<code>/gencode 30 5 10 60</code> — 10 исп., истекает через 60 дней\n"
            "<code>/gencode 7 2 1 0 WELCOME</code> — кастомный код WELCOME",
            parse_mode="HTML",
        )
        return

    try:
        days = _int_arg(args, 1, None, 1, 365)
        max_accs = _int_arg(args, 2, None, 1, 20)
    except ValueError:
        await message.answer("❌ Дней: 1–365. Аккаунтов: 1–20.")
        return
    try:
        max_uses = _int_arg(args, 3, 1, 1, 10000)
    except ValueError:
        await message.answer("❌ Использований: 1–10000.")
        return
    try:
        expires_days = _int_arg(args, 4, 0, 0, 3650) or None
    except ValueError:
        await message.answer("❌ Срок действия кода: 1–3650 дней (0 = бессрочно).")
        return

    if len(args) >= 6:
        code = args[5].upper().strip()
        if not code.replace("-", "").replace("_", "").isalnum() or len(code) > 30:
            await message.answer("❌ Код: только буквы, цифры, дефис. Макс. 30 символов.")
            return
    else:
        code = _generate_code()

    if not _create_promo(code, days, max_accs, max_uses, expires_days,
                         created_by=str(message.from_user.id)):
        await message.answer(
            f"⚠️ Код <code>{html.escape(code)}</code> уже существует. "
            f"Удалите его через /delcode или выберите другой.",
            parse_mode="HTML",
        )
        return

    exp_text = f"{expires_days} дней" if expires_days else "бессрочно"
    uses_text = "одноразовый" if max_uses == 1 else f"{max_uses} раз"
    await message.answer(
        f"✅ <b>Промокод создан!</b>\n\n"
        f"🎟 Код: <code>{html.escape(code)}</code>\n"
        f"📅 Даёт: <b>{days} дней</b>, <b>{max_accs} акк.</b>\n"
        f"🔁 Использований: <b>{uses_text}</b>\n"
        f"⏳ Срок действия кода: <b>{exp_text}</b>\n\n"
        f"Пользователь вводит: <code>/code {html.escape(code)}</code>",
        parse_mode="HTML",
    )


@dp.message(Command("codes"))
async def codes_cmd(message: Message):
    if not _is_admin(message.from_user.id):
        return

    with get_db(row_factory=False) as conn:
        rows = conn.execute("""
            SELECT code, days, max_accounts, max_uses, uses_left, expires_at
            FROM promo_codes WHERE uses_left > 0
            ORDER BY created_at DESC LIMIT 20
        """).fetchall()

    now = datetime.now()
    lines = ["🎟 <b>Активные промокоды:</b>\n"]
    for code, days, max_accs, max_uses, uses_left, expires_at in rows:
        exp = "∞"
        exp_dt = _parse_ts(expires_at)
        if exp_dt:
            if exp_dt < now:
                continue
            exp = exp_dt.strftime("%d.%m.%Y")
        lines.append(
            f"<code>{html.escape(code)}</code> — {days}д / {max_accs}акк "
            f"| исп: {uses_left}/{max_uses} | до: {exp}"
        )

    if len(lines) == 1:
        await message.answer("📭 Активных промокодов нет.")
        return
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("delcode"))
async def delcode_cmd(message: Message):
    if not _is_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("📋 Использование: <code>/delcode КОД</code>", parse_mode="HTML")
        return

    code = args[1].upper().strip()
    with get_db(row_factory=False) as conn:
        cur = conn.execute("DELETE FROM promo_codes WHERE code = ?", (code,))
        if cur.rowcount:
            conn.execute("DELETE FROM promo_uses WHERE code = ?", (code,))

    if cur.rowcount:
        await message.answer(f"🗑 Промокод <code>{html.escape(code)}</code> удалён.", parse_mode="HTML")
    else:
        await message.answer(f"⚠️ Код <code>{html.escape(code)}</code> не найден.", parse_mode="HTML")


@dp.message(Command("revoke"))
async def revoke_cmd(message: Message):
    if not _is_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("📋 Использование: <code>/revoke tg_id</code>", parse_mode="HTML")
        return

    target_id = args[1].strip()
    if not target_id.lstrip("-").isdigit():
        await message.answer("❌ Некорректный Telegram ID.")
        return

    with get_db(row_factory=False) as conn:
        cur = conn.execute("""
            UPDATE users
            SET sub_end_date = datetime('now', '-1 day'),
                farm_enabled = 0, listener_enabled = 0
            WHERE tg_user_id = ?
        """, (target_id,))

    if not cur.rowcount:
        await message.answer(f"⚠️ Пользователь <code>{html.escape(target_id)}</code> не найден.",
                             parse_mode="HTML")
        return
    await message.answer(
        f"🚫 <b>Доступ отозван.</b>\n\n👤 <code>{html.escape(target_id)}</code>\n"
        f"Подписка истекла. Фарм остановлен.",
        parse_mode="HTML",
    )


@dp.message(Command("users"))
async def users_cmd(message: Message):
    if not _is_admin(message.from_user.id):
        return

    with get_db(row_factory=False) as conn:
        rows = conn.execute("""
            SELECT tg_user_id, username, sub_end_date, max_accounts, farm_enabled
            FROM users ORDER BY sub_end_date DESC LIMIT 20
        """).fetchall()

    if not rows:
        await message.answer("📭 Пользователей пока нет.")
        return

    now = datetime.now()
    lines = ["👥 <b>Пользователи (последние 20):</b>\n"]
    for tg_id, username, sub_end, max_accs, farm_on in rows:
        end_dt = _parse_ts(sub_end)
        if end_dt:
            status = f"✅ {(end_dt - now).days}д" if end_dt > now else "❌ истёк"
        else:
            status = "❓"
        name = html.escape(f"@{username}" if username else tg_id)
        farm = "🟢" if farm_on else "⚫"
        lines.append(f"{farm} <code>{html.escape(tg_id)}</code> {name} — {status}, {max_accs}акк.")

    await message.answer("\n".join(lines), parse_mode="HTML")


# ==============================================================================
# User commands
# ==============================================================================

@dp.message(Command("code"))
async def code_cmd(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("🎟 Введите промокод:\n<code>/code ВАШ-КОД</code>", parse_mode="HTML")
        return

    user_id = message.from_user.id
    if _promo_blocked(user_id):
        await message.answer("⏳ Слишком много неверных попыток. Попробуйте через 10 минут.")
        return

    result = _redeem_promo(args[1], str(user_id), message.from_user.username)

    if not result["ok"]:
        if result["reason"] == "not_found":
            _promo_register_fail(user_id)
        reason_map = {
            "not_found":    "❌ Промокод не найден. Проверьте правильность ввода.",
            "expired":      "⏰ Срок действия этого промокода истёк.",
            "used_up":      "😔 Промокод уже использован максимальное количество раз.",
            "already_used": "⚠️ Вы уже активировали этот промокод.",
        }
        await message.answer(reason_map.get(result["reason"], "❌ Ошибка активации."))
        return

    await message.answer(
        f"🎉 <b>Промокод активирован!</b>\n\n"
        f"📅 Подписка до: <b>{result['new_end'].strftime('%d.%m.%Y')}</b>\n"
        f"📊 Доступно аккаунтов: <b>{result['max_accounts']}</b>\n\n"
        f"Нажмите /start, чтобы открыть панель.",
        parse_mode="HTML",
    )


async def _show_main_menu(chat_id: int, user: types.User):
    """Renders and sends the main menu. Works from /start and from callbacks."""
    with get_db(row_factory=False) as conn:
        row = conn.execute(
            "SELECT sub_end_date, max_accounts FROM users WHERE tg_user_id = ?",
            (str(user.id),),
        ).fetchone()

    if not row:
        add_or_update_user(str(user.id), user.username, add_days=-1, max_accs=0)
        end_date = None
    else:
        end_date = _parse_ts(row[0])

    if end_date is None and row is None:
        status_text = ("❌ <b>У вас нет активной подписки.</b>\n"
                       "Пожалуйста, выберите тарифный план для начала работы:")
    elif end_date and end_date > datetime.now():
        status_text = (f"📅 Ваша подписка действует до: <b>{end_date.strftime('%d.%m.%Y')}</b>\n"
                       f"📊 Ваш лимит аккаунтов: <b>{row[1]}</b>")
    else:
        status_text = "❌ <b>Подписка закончилась!</b>\nПожалуйста, продлите тариф:"

    link = generate_magic_link(user.id, user.first_name)

    rows = [
        [InlineKeyboardButton(text="💻 Открыть панель Threads", url=link)],
        [InlineKeyboardButton(text="📖 Инструкция по подключению", url=GUIDE_URL)],
        [InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="enter_promo")],
    ]
    for key, t in TARIFFS.items():
        rows.append([InlineKeyboardButton(
            text=f"🛒 ${t['price']} — {t['accounts']} аккаунтов ({t['days']} дней)",
            callback_data=f"buy_{key}",
        )])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    text = (
        f"👋 Привет, {html.escape(user.first_name or '')}!\n\n"
        f"🤖 Это менеджер твоей Threads-Фермы.\n\n"
        f"{status_text}\n\n"
        f"👇 Ваша панель управления:"
    )

    try:
        sent = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
        _save_menu_message(str(user.id), sent.message_id)
    except Exception as e:
        logger.error(f"[Menu] send_message failed for user={user.id}: {e}")


@dp.message(Command("start"))
async def start_cmd(message: Message):
    # Subscription gate is enforced by SubscriptionGate middleware.
    await _show_main_menu(message.chat.id, message.from_user)
    try:
        await message.delete()
    except Exception:
        pass


@dp.message(Command("help"))
async def help_cmd(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Открыть инструкцию", url=GUIDE_URL)],
    ])
    await message.answer(
        "📖 Полная инструкция по подключению Threads-аккаунта — со скриншотами на каждом шаге:",
        reply_markup=kb,
    )


@dp.callback_query(F.data == "check_subscription")
async def check_subscription_callback(callback: CallbackQuery):
    if not await is_subscribed(callback.from_user.id):
        await callback.answer("❌ Подписка не найдена. Подпишись на канал и нажми снова.",
                              show_alert=True)
        return

    await callback.answer("✅ Подписка подтверждена!", show_alert=False)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _show_main_menu(callback.message.chat.id, callback.from_user)


@dp.callback_query(F.data == "enter_promo")
async def enter_promo_callback(callback: CallbackQuery):
    await callback.message.answer(
        "🎟 <b>Введите промокод командой:</b>\n\n"
        "<code>/code ВАШ-ПРОМОКОД</code>\n\n"
        "Например: <code>/code WELCOME7</code>",
        parse_mode="HTML",
    )
    await callback.answer()


# ==============================================================================
# Purchase flow (CryptoPay) — every invoice is stored and credited exactly once
# ==============================================================================

@dp.callback_query(F.data.startswith("buy_"))
async def process_buy(callback: CallbackQuery):
    tariff = TARIFFS.get(callback.data[len("buy_"):])
    if not tariff:
        await callback.answer("Неизвестный тариф. Откройте меню заново через /start.", show_alert=True)
        return
    tariff_key = callback.data[len("buy_"):]
    user_id = str(callback.from_user.id)

    with get_db(row_factory=False) as conn:
        row = conn.execute(
            "SELECT sub_end_date FROM users WHERE tg_user_id = ?", (user_id,)
        ).fetchone()
    end_date = _parse_ts(row[0]) if row else None
    if end_date and end_date > datetime.now():
        await callback.answer(
            f"✅ У вас уже есть активная подписка до {end_date.strftime('%d.%m.%Y %H:%M')}.\n\n"
            f"Вы можете продлить её позже через /start",
            show_alert=True,
        )
        return

    try:
        invoice = await crypto.create_invoice(
            asset="USDT",
            amount=tariff["price"],
            expires_in=INVOICE_TTL_SECONDS,
            payload=f"{user_id}:{tariff_key}",
        )
    except Exception as e:
        logger.error(f"[CryptoPay] create_invoice failed: {e}", exc_info=True)
        await callback.answer("⚠️ Не удалось создать счёт. Попробуйте через минуту.", show_alert=True)
        return

    with get_db(row_factory=False) as conn:
        conn.execute(
            "INSERT INTO invoices (invoice_id, tg_user_id, tariff_key, amount, days, max_accounts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (invoice.invoice_id, user_id, tariff_key, tariff["price"],
             tariff["days"], tariff["accounts"]),
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {tariff['price']} USDT", url=invoice.bot_invoice_url)],
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_{invoice.invoice_id}")],
    ])
    await callback.message.edit_text(
        f"⏳ Сформирован счёт на сумму <b>{tariff['price']} USDT</b>.\n\n"
        f"Тариф: {tariff['accounts']} аккаунтов на {tariff['days']} дней.\n"
        f"Счёт действителен {INVOICE_TTL_SECONDS // 60} минут.\n\n"
        f"Нажмите кнопку ниже, чтобы оплатить через CryptoBot. "
        f"После оплаты нажмите «Проверить оплату».",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


async def _fetch_invoice_status(invoice_id: int) -> Optional[str]:
    """Returns 'active' | 'paid' | 'expired' (lower-case) or None if it could not be determined."""
    try:
        result = await crypto.get_invoices(invoice_ids=[invoice_id])
    except Exception as e:
        logger.error(f"[CryptoPay] get_invoices({invoice_id}) failed: {e}", exc_info=True)
        return None

    if isinstance(result, list):
        items = result
    elif hasattr(result, "status"):          # a single Invoice object
        items = [result]
    else:
        items = list(getattr(result, "items", None) or [])
    if not items:
        return None

    status = items[0].status
    return str(getattr(status, "value", status)).strip().lower()


# Regex on purpose: "check_subscription" must never reach this handler.
@dp.callback_query(F.data.regexp(r"^check_\d+"))
async def check_payment(callback: CallbackQuery):
    invoice_id = int(callback.data.split("_")[1])
    user_id = str(callback.from_user.id)

    # Tariff and amount come from OUR database, never from callback_data.
    with get_db(row_factory=False) as conn:
        inv = conn.execute(
            "SELECT status, amount, days, max_accounts FROM invoices "
            "WHERE invoice_id = ? AND tg_user_id = ?",
            (invoice_id, user_id),
        ).fetchone()

    if not inv:
        await callback.answer("Счёт не найден. Создайте новый через /start.", show_alert=True)
        return
    status, amount, days, max_accounts = inv

    if status == "done":
        await callback.answer("✅ Этот счёт уже зачислен. Нажмите /start.", show_alert=True)
        return

    remote = await _fetch_invoice_status(invoice_id)
    if remote is None:
        await callback.answer("⚠️ Не удалось проверить статус оплаты. Попробуйте через минуту.",
                              show_alert=True)
        return

    if remote == "paid":
        # Exactly-once: only the caller whose UPDATE flips pending→done gets to credit.
        with get_db(row_factory=False) as conn:
            cur = conn.execute(
                "UPDATE invoices SET status = 'done', paid_at = datetime('now') "
                "WHERE invoice_id = ? AND tg_user_id = ? AND status = 'pending'",
                (invoice_id, user_id),
            )
            new_end = None
            if cur.rowcount == 1:
                new_end = _apply_subscription(
                    conn, user_id, callback.from_user.username, days, max_accounts
                )

        if new_end is None:
            await callback.answer("✅ Этот счёт уже зачислен. Нажмите /start.", show_alert=True)
            return

        logger.info(f"[Payment] invoice={invoice_id} user={user_id} credited {days}d/{max_accounts}acc")
        await callback.message.edit_text(
            f"✅ <b>Оплата {amount:g}$ успешно получена!</b>\n\n"
            f"🎉 Ваш тарифный план обновлён.\n"
            f"Теперь вам доступно {max_accounts} аккаунтов.\n"
            f"Подписка продлена до {new_end.strftime('%d.%m.%Y %H:%M')}.\n\n"
            f"Нажмите /start, чтобы получить новую ссылку для входа в панель.",
            parse_mode="HTML",
        )
        return

    if remote == "expired":
        with get_db(row_factory=False) as conn:
            conn.execute(
                "UPDATE invoices SET status = 'expired' WHERE invoice_id = ? AND status = 'pending'",
                (invoice_id,),
            )
        await callback.answer("⌛ Счёт истёк. Создайте новый через /start.", show_alert=True)
        return

    await callback.answer("❌ Счёт ещё не оплачен. Завершите оплату и попробуйте снова.",
                          show_alert=True)


# ==============================================================================
# Entry point
# ==============================================================================

async def main():
    with get_db(row_factory=False) as conn:
        apply_schema(conn)

    user_commands = [
        types.BotCommand(command="start", description="🏠 Главное меню"),
        types.BotCommand(command="help",  description="📖 Инструкция по подключению"),
        types.BotCommand(command="code",  description="🎟 Ввести промокод"),
    ]
    await bot.set_my_commands(user_commands, scope=types.BotCommandScopeDefault())

    admin_commands = user_commands + [
        types.BotCommand(command="trial",   description="⏳ Выдать пробный доступ"),
        types.BotCommand(command="gencode", description="🎟 Создать промокод"),
        types.BotCommand(command="codes",   description="📋 Список промокодов"),
        types.BotCommand(command="delcode", description="🗑 Удалить промокод"),
        types.BotCommand(command="revoke",  description="🚫 Отозвать доступ"),
        types.BotCommand(command="users",   description="👥 Список пользователей"),
        types.BotCommand(command="mk_boss", description="👑 Безлимитный доступ себе"),
    ]
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                admin_commands, scope=types.BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception as e:
            logger.warning(f"Could not set bot commands for admin {admin_id}: {e}")

    logger.info("🤖 Telegram Bot with CryptoPay initialized and running!")
    try:
        await dp.start_polling(bot)
    finally:
        try:
            await crypto.close()
        except Exception:
            pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())