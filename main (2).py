"""
Deelo — bot + Mini App в одном процессе.

Что делает этот файл:
1. Поднимает Telegram-бота (aiogram 3.x) на вебхуке.
2. Отдаёт web/index.html как страницу Mini App по адресу "/".
3. При старте сам прописывает боту кнопку меню (Menu Button),
   которая открывает Mini App — руками в BotFather ничего делать не нужно.
4. На /start отправляет фото + приветствие + инлайн-кнопки (как у Playerok).

Переменные окружения (уже есть на bothost.tech, ничего добавлять не надо):
  BOT_TOKEN / TOKEN     — токен бота
  DOMAIN                — домен бота, например playerok.cc
                          WEBHOOK_URL вычисляется автоматически из DOMAIN
  PORT                  — порт, на котором слушать (задаёт хостинг)
"""

# ============================================================
# 01. ИМПОРТЫ И ЗАВИСИМОСТИ
# ============================================================

import os
import asyncio
import logging
import sqlite3
import json
import re
import hmac
import hashlib
import uuid
import random
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, quote, urlparse
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message,
    MenuButtonWebApp,
    WebAppInfo,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    LabeledPrice,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web, ClientSession, BasicAuth

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("deelo")

MSK_TZ = ZoneInfo("Europe/Moscow")

def _msk_time_label(ts_ms) -> str:
    """Human-readable Moscow time for bot/admin notifications."""
    try:
        value = float(ts_ms or 0)
        if value <= 0:
            return "—"
        if value < 10_000_000_000:
            value *= 1000
        dt = datetime.fromtimestamp(value / 1000, tz=MSK_TZ)
        return dt.strftime("%d.%m.%Y · %H:%M МСК")
    except Exception:
        return "—"

def _deal_timeline_text(deal: dict) -> str:
    created = deal.get("acceptedAt") or deal.get("createdAt")
    ended = deal.get("completedAt") or deal.get("payoutAt") or deal.get("refundedAt")
    lines = [
        f"🕒 <b>Начало:</b> {_msk_time_label(created)}",
        f"🔒 <b>Резерв:</b> {_msk_time_label(deal.get('paidAt'))}",
        f"📦 <b>Передача:</b> {_msk_time_label(deal.get('transferredAt'))}",
        f"✅ <b>Подтверждение:</b> {_msk_time_label(deal.get('receivedAt'))}",
        f"💸 <b>Выплата:</b> {_msk_time_label(deal.get('payoutAt'))}",
    ]
    if ended:
        lines.append(f"🏁 <b>Завершение:</b> {_msk_time_label(ended)}")
    return "\n".join(lines)

ADMIN_IDS = {8712419494}

# ============================================================
# ОБЯЗАТЕЛЬНАЯ ПОДПИСКА: ShadowTeamReserve · v38 group-topic fix
# ============================================================
REQUIRED_SUB_USERNAME = "ShadowTeamReserve"
REQUIRED_SUB_CHAT = f"@{REQUIRED_SUB_USERNAME}"
REQUIRED_SUB_URL = f"https://t.me/{REQUIRED_SUB_USERNAME}"
# Если PROTECTED_GROUP_ID не задан, защита действует во всех группах/супергруппах,
# где этот бот получает сообщения. При желании можно задать конкретный -100... ID в env.
# Основной чат ShadowTeam из ссылки https://t.me/c/4295268219/25
# Для forum-группы 4295268219 -> chat_id = -1004295268219, а 25 — message_thread_id темы.
PROTECTED_GROUP_ID = os.getenv("PROTECTED_GROUP_ID", "-1004295268219").strip()
PROTECTED_GROUP_THREAD_ID = int(os.getenv("PROTECTED_GROUP_THREAD_ID", "25"))


# ============================================================
# 02. ОСНОВНАЯ КОНФИГУРАЦИЯ И ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ============================================================
# ---------- конфиг из окружения ----------
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не найден токен бота (BOT_TOKEN / TOKEN / TELEGRAM_BOT_TOKEN)")

# IMPORTANT: use DOMAIN as the single source of truth.
# A stale WEBHOOK_URL in BotHost variables used to make Telegram reject
# the webhook and prevent the whole aiohttp app from starting.
DEFAULT_DOMAIN = "playerok.cc"
DOMAIN = (os.getenv("DOMAIN") or DEFAULT_DOMAIN).strip()
DOMAIN = re.sub(r"^https?://", "", DOMAIN).rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"https://{DOMAIN}{WEBHOOK_PATH}"
WEBAPP_URL = f"https://{DOMAIN}/"
BOT_PUBLIC_USERNAME = os.getenv("BOT_PUBLIC_USERNAME", "PlayerokMapketBot").strip().lstrip("@")
BOT_PUBLIC_URL = f"https://t.me/{BOT_PUBLIC_USERNAME}"

PORT = int(os.getenv("PORT", "3000"))

# ============================================================
# 03. НАСТРОЙКИ ПЛАТЁЖЕЙ: TOME.GE И ЮMONEY
# ============================================================
# ---------- Tome.ge ----------
TOME_SHOP_ID = os.getenv("TOME_SHOP_ID", "").strip()
TOME_SECRET_KEY = os.getenv("TOME_SECRET_KEY", "").strip()
TOME_API_URL = "https://tome.ge/api/v1"
TOME_WEBHOOK_PATH = "/tome/webhook"

# ---------- ЮMoney ----------
# Номер кошелька и секрет HTTP-уведомлений задаются только через env на BotHost.
# Никогда не помещай секрет в GitHub или frontend.
YOOMONEY_WALLET = os.getenv("YOOMONEY_WALLET", "").strip()
YOOMONEY_HTTP_SECRET = os.getenv("YOOMONEY_HTTP_SECRET", "").strip()
YOOMONEY_WEBHOOK_PATH = "/yoomoney/webhook"
YOOMONEY_PAY_PATH = "/yoomoney/pay"
YOOMONEY_QUICKPAY_URL = "https://yoomoney.ru/quickpay/confirm"
YOOMONEY_MIN_AMOUNT = Decimal("100.00")
YOOMONEY_MAX_AMOUNT = Decimal(os.getenv("YOOMONEY_MAX_AMOUNT", "50000.00"))

# ---------- Telegram Stars ----------
# Сколько рублей зачислять на внутренний баланс за 1 Star.
# Меняй только через env, чтобы курс не был захардкожен во frontend.
STARS_RUB_PER_STAR = Decimal("2.00")
STARS_MIN_RUB = Decimal("100.00")
STARS_MAX_RUB = Decimal(os.getenv("STARS_MAX_RUB", "50000.00"))

# ---------- Crypto Pay / @CryptoBot ----------
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "").strip()
CRYPTO_PAY_API_URL = "https://pay.crypt.bot/api"
CRYPTO_MIN_RUB = Decimal("100.00")
CRYPTO_MAX_RUB = Decimal(os.getenv("CRYPTO_MAX_RUB", "50000.00"))

# ---------- AI Support ----------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip()
OPENAI_API_URL = "https://api.openai.com/v1/responses"
SUPPORT_KNOWLEDGE_FILE = Path(__file__).resolve().parent / "support_knowledge.md"

# ---------- URL Mini App ----------

WEB_DIR = Path(__file__).parent / "web"
INDEX_FILE = WEB_DIR / "index.html"

# ============================================================
# 04. SQLITE: ПОСТОЯННОЕ ХРАНЕНИЕ ДАННЫХ MINI APP
# ============================================================
# Persistent Mini App storage. The old build only used browser localStorage,
# so balances/deals could disappear when Telegram opened the app in another
# WebView/device. Keep the data on the same host as the bot.
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / "playerok.sqlite3"

def db_connect():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, shared INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT (strftime('%s','now')))" )
    conn.commit()
    return conn


# ============================================================
# 05. ФАЙЛЫ И МЕДИА БОТА
# ============================================================
# Картинка, которая отправляется вместе с приветствием на /start.
# Положи свой файл рядом, в папку web/, под этим именем (или поменяй имя тут).
START_PHOTO = WEB_DIR / "playerok_welcome.png"
HOW_PHOTO = WEB_DIR / "playerok_how.jpg"


# ============================================================
# 06. ПОИСК ФАЙЛОВ И РЕСУРСОВ
# ============================================================
def resolve_asset(name: str):
    """Find bundled web assets even when the host starts the script from another cwd."""
    candidates = [
        WEB_DIR / name,
        Path(__file__).resolve().parent / "web" / name,
        Path.cwd() / "web" / name,
        Path.cwd() / name,
    ]
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            pass
    return None


# ============================================================
# 07. БЕЗОПАСНОСТЬ: ПРОВЕРКА TELEGRAM MINI APP initData
# ============================================================
def telegram_user_from_init_data(init_data: str):
    """Validate Telegram Mini App initData and return the authenticated user id."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", "")
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, received_hash):
            return None
        user_raw = pairs.get("user", "")
        user = json.loads(user_raw) if user_raw else {}
        return str(user.get("id")) if user.get("id") is not None else None
    except Exception:
        return None

# ============================================================
# 08. ВНЕШНИЕ ССЫЛКИ И НАСТРОЙКИ КНОПОК
# ============================================================
# ---------- ссылки для нижних кнопок ----------
# Замени на свои реальные адреса/страницы мини-аппа.
SITE_URL = "https://playerok.com"
CHANNEL_URL = "https://t.me/playerok"

# ============================================================
# 09. ИНИЦИАЛИЗАЦИЯ TELEGRAM-БОТА И DISPATCHER
# ============================================================
# ---------- бот ----------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ============================================================
# АВТООЧИСТКА СИСТЕМНЫХ СООБЩЕНИЙ О ВСТУПЛЕНИИ В ГРУППУ
# ============================================================

@dp.message(F.new_chat_members)
async def delete_join_messages(message: Message):
    try:
        await message.delete()
    except Exception:
        log.exception("Не удалось удалить сообщение о вступлении в группу")

def _subscription_required(user_id: int | str) -> bool:
    """Only users who have activated the promo at least once are gated in private bot/Mini App."""
    uid = str(user_id)
    if uid in _admin_ids():
        return False
    rec = db_get_json(f"deelo_user_{uid}", {}) or {}
    return bool(rec.get("subscriptionRequired"))


def _mark_subscription_required(user_id: int | str) -> None:
    """Persistent one-way flag: once promo was used, subscription is required forever."""
    uid = str(user_id)
    if uid in _admin_ids():
        return
    key = f"deelo_user_{uid}"
    rec = db_get_json(key, {}) or {"id": uid}
    rec["subscriptionRequired"] = True
    rec.setdefault("subscriptionRequiredSince", int(time.time() * 1000))
    db_set_json(key, rec, 1)


async def _subscription_is_member(user_id: int | str) -> bool:
    """Check membership in @ShadowTeamReserve through Telegram Bot API."""
    try:
        member = await bot.get_chat_member(REQUIRED_SUB_CHAT, int(user_id))
        status_obj = getattr(member, "status", "")
        status = str(getattr(status_obj, "value", status_obj)).lower()
        if status in {"member", "administrator", "creator"}:
            return True
        if status == "restricted" and bool(getattr(member, "is_member", False)):
            return True
        return False
    except Exception:
        log.exception("Не удалось проверить подписку user=%s chat=%s", user_id, REQUIRED_SUB_CHAT)
        return False


def _subscription_keyboard(scope: str, user_id: int | str) -> InlineKeyboardMarkup:
    uid = str(user_id)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться", url=REQUIRED_SUB_URL)],
        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data=f"subcheck_{scope}:{uid}")],
    ])


def _private_subscription_text(failed: bool = False) -> str:
    if failed:
        return (
            "❌ <b>Подписка не найдена</b>\n\n"
            f"Подпишитесь на <b>@{REQUIRED_SUB_USERNAME}</b> и нажмите «Проверить ещё раз»."
        )
    return (
        "🔒 <b>Доступ временно ограничен</b>\n\n"
        f"Для продолжения использования бота подпишитесь на <b>@{REQUIRED_SUB_USERNAME}</b>.\n\n"
        "После подписки нажмите <b>«Проверить подписку»</b> — доступ восстановится автоматически."
    )


def _group_subscription_text(failed: bool = False) -> str:
    if failed:
        return (
            "❌ <b>Подписка не найдена</b>\n\n"
            f"Сначала подпишитесь на <b>@{REQUIRED_SUB_USERNAME}</b>, затем повторите проверку."
        )
    return (
        "🔒 <b>Доступ к чату ограничен</b>\n\n"
        f"Чтобы отправлять сообщения в этой группе, необходимо подписаться на <b>@{REQUIRED_SUB_USERNAME}</b>.\n\n"
        "После подписки нажмите <b>«Проверить подписку»</b> — доступ к чату откроется автоматически."
    )


def _is_protected_group(chat_id: int) -> bool:
    if not PROTECTED_GROUP_ID:
        return True
    try:
        return int(PROTECTED_GROUP_ID) == int(chat_id)
    except Exception:
        return False


async def _delete_message_later(message: Message, delay: float = 4.0):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass


class SubscriptionMessageMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        user = getattr(event, "from_user", None)
        if not user or getattr(user, "is_bot", False):
            return await handler(event, data)

        chat_type = str(getattr(event.chat, "type", ""))

        # GROUP: restriction applies to everyone, even people who never opened the bot.
        # В forum-группе проверяем только основной чат общения (topic/thread 25),
        # чтобы служебные темы вроде General не перехватывались этой логикой.
        if chat_type in {"group", "supergroup"} and _is_protected_group(event.chat.id):
            thread_id = getattr(event, "message_thread_id", None)
            if PROTECTED_GROUP_THREAD_ID and int(thread_id or 0) != int(PROTECTED_GROUP_THREAD_ID):
                return await handler(event, data)

            if await _subscription_is_member(user.id):
                return await handler(event, data)
            try:
                await event.delete()
            except Exception:
                log.exception("Не удалось удалить сообщение неподписанного пользователя %s", user.id)
            try:
                await bot.send_message(
                    event.chat.id,
                    _group_subscription_text(False),
                    parse_mode="HTML",
                    reply_markup=_subscription_keyboard("group", user.id),
                    message_thread_id=PROTECTED_GROUP_THREAD_ID or thread_id,
                )
            except Exception:
                log.exception("Не удалось показать требование подписки в основном чате группы")
            return None

        # PRIVATE BOT: only users who activated promo at least once are gated.
        if chat_type == "private" and _subscription_required(user.id):
            if await _subscription_is_member(user.id):
                return await handler(event, data)
            await event.answer(
                _private_subscription_text(False),
                parse_mode="HTML",
                reply_markup=_subscription_keyboard("bot", user.id),
            )
            return None

        return await handler(event, data)


class SubscriptionCallbackMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: CallbackQuery, data):
        callback_data = str(getattr(event, "data", "") or "")
        if callback_data.startswith("subcheck_"):
            return await handler(event, data)

        user = getattr(event, "from_user", None)
        message = getattr(event, "message", None)
        chat_type = str(getattr(getattr(message, "chat", None), "type", ""))
        if user and chat_type == "private" and _subscription_required(user.id):
            if not await _subscription_is_member(user.id):
                try:
                    await event.answer("Сначала подпишитесь на @ShadowTeamReserve", show_alert=True)
                except Exception:
                    pass
                try:
                    await message.answer(
                        _private_subscription_text(False),
                        parse_mode="HTML",
                        reply_markup=_subscription_keyboard("bot", user.id),
                    )
                except Exception:
                    pass
                return None
        return await handler(event, data)


dp.message.outer_middleware(SubscriptionMessageMiddleware())
dp.callback_query.outer_middleware(SubscriptionCallbackMiddleware())


@dp.callback_query(F.data.regexp(r"^subcheck_(bot|group):\d+$"))
async def cb_subscription_check(callback: CallbackQuery):
    raw = str(callback.data or "")
    m = re.fullmatch(r"subcheck_(bot|group):(\d+)", raw)
    if not m:
        await callback.answer("Ошибка проверки", show_alert=True)
        return
    scope, owner_uid = m.group(1), m.group(2)
    if str(callback.from_user.id) != owner_uid:
        await callback.answer("Эта кнопка предназначена другому пользователю.", show_alert=True)
        return

    subscribed = await _subscription_is_member(owner_uid)
    if not subscribed:
        await callback.answer("Подписка пока не найдена", show_alert=False)
        try:
            text = _group_subscription_text(True) if scope == "group" else _private_subscription_text(True)
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=_subscription_keyboard(scope, owner_uid),
            )
        except Exception:
            pass
        return

    await callback.answer("✅ Подписка подтверждена")
    if scope == "group":
        try:
            await callback.message.edit_text(
                "✅ <b>Подписка подтверждена</b>\n\nТеперь вы можете отправлять сообщения в группе.",
                parse_mode="HTML",
            )
            asyncio.create_task(_delete_message_later(callback.message, 4.0))
        except Exception:
            pass
        return

    try:
        await callback.message.edit_text(
            "✅ <b>Подписка подтверждена</b>\n\nДоступ к боту восстановлен. Можете продолжать пользоваться всеми функциями.",
            parse_mode="HTML",
            reply_markup=start_keyboard(),
        )
    except Exception:
        await callback.message.answer(
            "✅ Подписка подтверждена. Доступ к боту восстановлен.",
            reply_markup=start_keyboard(),
        )


# ============================================================
# 10. КЛАВИАТУРА /START И НАВИГАЦИЯ В MINI APP
# ============================================================
# Premium/custom emoji IDs for /start greeting and start keyboard.
# Telegram renders the normal emoji inside <tg-emoji> as a fallback where custom emoji is unavailable.
START_CUSTOM_EMOJI = {
    "diamond": "5427168083074628963",
    "sparkles": "5325547803936572038",
    "shield": "5197288647275071607",
    "card": "5445353829304387411",
    "chat": "5443038326535759644",
    "bolt": "5456140674028019486",
    "down": "5217897364945132677",
    "chart": "5231200819986047254",
    "info": "5334544901428229844",
    "rocket": "5188481279963715781",
    "money": "5317013291602553603",
    "profile": "5258011929993026890",
    "deal": "5206405003123631537",
    "support": "5238025132177369293",
    "question": "5436113877181941026",
    "site": "5447410659077661506",
    "channel": "5399967660052081305",
}


def _premium_emoji(fallback: str, key: str) -> str:
    """HTML custom emoji with a normal emoji fallback."""
    emoji_id = START_CUSTOM_EMOJI.get(key, "")
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>' if emoji_id else fallback


def start_keyboard() -> InlineKeyboardMarkup:
    """Главное меню /start с premium/custom emoji на кнопках."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="Открыть Playerok",
                icon_custom_emoji_id=START_CUSTOM_EMOJI["rocket"],
                style="success",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )],
            [
                InlineKeyboardButton(
                    text="Кошелёк",
                    icon_custom_emoji_id=START_CUSTOM_EMOJI["money"],
                    style="primary",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=home"),
                ),
                InlineKeyboardButton(
                    text="Профиль",
                    icon_custom_emoji_id=START_CUSTOM_EMOJI["profile"],
                    style="primary",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=profile"),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Сделки",
                    icon_custom_emoji_id=START_CUSTOM_EMOJI["deal"],
                    style="success",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=deals"),
                ),
                InlineKeyboardButton(
                    text="Поддержка",
                    icon_custom_emoji_id=START_CUSTOM_EMOJI["support"],
                    style="danger",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=support"),
                ),
            ],
            [InlineKeyboardButton(
                text="Как это работает",
                icon_custom_emoji_id=START_CUSTOM_EMOJI["question"],
                style="primary",
                callback_data="how_it_works",
            )],
            [
                InlineKeyboardButton(
                    text="Сайт",
                    icon_custom_emoji_id=START_CUSTOM_EMOJI["site"],
                    style="primary",
                    url=SITE_URL,
                ),
                InlineKeyboardButton(
                    text="Канал",
                    icon_custom_emoji_id=START_CUSTOM_EMOJI["channel"],
                    style="success",
                    url=CHANNEL_URL,
                ),
            ],
        ]
    )


def help_keyboard() -> InlineKeyboardMarkup:
    """Кнопки под /help и «Как это работает» с premium/custom emoji."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="Открыть Playerok",
                icon_custom_emoji_id=START_CUSTOM_EMOJI["rocket"],
                style="success",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )],
            [
                InlineKeyboardButton(
                    text="Мои сделки",
                    icon_custom_emoji_id=START_CUSTOM_EMOJI["deal"],
                    style="success",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=deals"),
                ),
                InlineKeyboardButton(
                    text="Поддержка",
                    icon_custom_emoji_id=START_CUSTOM_EMOJI["support"],
                    style="danger",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=support"),
                ),
            ],
            [InlineKeyboardButton(
                text="Главное меню",
                icon_custom_emoji_id=START_CUSTOM_EMOJI["down"],
                callback_data="help_main_menu",
            )],
        ]
    )


# ============================================================
# 11. КОМАНДА /START: ПРИВЕТСТВИЕ И СОХРАНЕНИЕ ПРОФИЛЯ
# ============================================================
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    name = message.from_user.first_name if message.from_user else "друг"
    # Save the Telegram identity immediately. The Mini App later enriches the same
    # record with photo_url received from Telegram WebApp initDataUnsafe.user.
    if message.from_user:
        try:
            uid = str(message.from_user.id)
            with db_connect() as conn:
                row = conn.execute("SELECT value FROM kv WHERE key=?", (f"deelo_user_{uid}",)).fetchone()
                rec = json.loads(row[0]) if row else {}
                rec.setdefault("id", uid)
                rec["username"] = message.from_user.first_name or rec.get("username") or ("Пользователь " + uid[-4:])
                rec["lastname"] = message.from_user.last_name or rec.get("lastname", "")
                rec["telegramUsername"] = message.from_user.username or rec.get("telegramUsername", "")
                rec.setdefault("photoUrl", "")
                conn.execute("INSERT INTO kv(key,value,shared,updated_at) VALUES(?,?,1,strftime('%s','now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (f"deelo_user_{uid}", json.dumps(rec, ensure_ascii=False)))
                conn.commit()
        except Exception:
            log.exception("Не удалось сохранить Telegram-профиль пользователя")
    if message.from_user:
        current = db_get_json(f"deelo_user_{message.from_user.id}", {}) or {}
        if current.get("banned") and str(message.from_user.id) not in _admin_ids():
            await message.answer("⛔ Ваш аккаунт заблокирован администрацией Playerok. Обратитесь в поддержку, если считаете это ошибкой.")
            return
    text = (
        f'{_premium_emoji("💎", "diamond")} <b>PLAYEROK · маркетплейс игр</b>\n'
        "<u>━━━━━━━━━━━━━━━━━━━━</u>\n"
        f'{_premium_emoji("✨", "sparkles")} <b>Привет, {name}!</b> Сделки — прямо в Telegram.\n\n'
        f'{_premium_emoji("🛡", "shield")} <b>Гарант</b> — деньги у сервиса, пока сделка не закрыта\n'
        f'{_premium_emoji("💳", "card")} <b>Кошелёк</b> — пополнение и вывод в пару тапов\n'
        f'{_premium_emoji("💬", "chat")} <b>Сделки и чат</b> — во встроенном приложении\n'
        f'{_premium_emoji("⚡", "bolt")} <b>Автовыплата</b> — сразу после подтверждения\n'
        "<u>━━━━━━━━━━━━━━━━━━━━</u>\n"
        f'{_premium_emoji("👇", "down")} Жми <b>«Открыть Playerok»</b> — под сообщением или слева от поля ввода\n\n'
        f'{_premium_emoji("📊", "chart")} <b>Комиссия 12,5%</b> · '
        f'{_premium_emoji("ℹ️", "info")} <i>/help — как это работает</i>'
    )

    # Отправляем стартовый TGS-стикер отдельным сообщением ДО приветствия.
    # Поддерживаем оба возможных имени, чтобы существующий файл пользователя
    # не приходилось переименовывать. Ошибка со стикером не должна ломать /start.
    start_sticker = (
        resolve_asset("start_sticker.tgs")
        or resolve_asset("773947703670341889.tgs")
    )
    if start_sticker:
        try:
            await message.answer_sticker(FSInputFile(start_sticker))
            log.info("Стартовый TGS-стикер отправлен: %s", start_sticker)
        except Exception:
            log.exception("Не удалось отправить стартовый TGS-стикер: %s", start_sticker)
    else:
        log.warning("Стартовый TGS-стикер не найден (ожидалось start_sticker.tgs или 773947703670341889.tgs)")

    start_photo = resolve_asset("playerok_welcome.png")
    if start_photo:
        await message.answer_photo(
            photo=FSInputFile(start_photo),
            caption=text,
            parse_mode="HTML",
            reply_markup=start_keyboard(),
        )
    else:
        # Если файла с картинкой нет рядом — не роняем бота,
        # просто шлём текст с теми же кнопками.
        log.warning("Файл playerok_welcome.png не найден, отправляю без фото")
        await message.answer(text, parse_mode="HTML", reply_markup=start_keyboard())


@dp.callback_query(F.data == "how_it_works")
async def cb_how_it_works(callback):
    await callback.answer()
    text = (
        f'{_premium_emoji("💎", "diamond")} <b>PLAYEROK · как это работает</b>\n'
        "<u>━━━━━━━━━━━━━━━━━━━━</u>\n"
        f'1. {_premium_emoji("🤝", "deal")} <b>Создайте сделку</b> — укажите второго участника, товар и сумму.\n'
        f'2. {_premium_emoji("💳", "card")} <b>Оплата</b> — деньги замораживаются у сервиса, продавец их ещё не получает.\n'
        f'3. {_premium_emoji("💬", "chat")} <b>Передача товара</b> — продавец отправляет товар в чате и нажимает «Товар передан».\n'
        f'4. {_premium_emoji("🛡", "shield")} <b>Подтверждение</b> — покупатель проверяет товар, после чего деньги уходят продавцу.\n'
        "<u>━━━━━━━━━━━━━━━━━━━━</u>\n"
        f'{_premium_emoji("🛡", "shield")} <b>Спор</b> — в любой момент можно подключить модератора.\n'
        f'{_premium_emoji("📊", "chart")} <b>Комиссия сервиса — 12,5%</b>, платит покупатель сверху.\n'
        f'{_premium_emoji("ℹ️", "info")} <b>Никогда не переводите деньги напрямую</b> мимо сделки.'
    )
    how_photo = resolve_asset("playerok_how.jpg")
    if how_photo:
        await callback.message.answer_photo(photo=FSInputFile(how_photo), caption=text, parse_mode="HTML", reply_markup=help_keyboard())
    else:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=help_keyboard())


# ============================================================
# 12. КОМАНДА /HELP
# ============================================================
@dp.message(F.text == "/help")
async def cmd_help(message: Message):
    text = (
        f'{_premium_emoji("ℹ️", "info")} <b>Как работает Playerok</b>\n'
        "<u>━━━━━━━━━━━━━━━━━━━━</u>\n"
        f'1. {_premium_emoji("🤝", "deal")} <b>Продавец или покупатель создаёт сделку</b>\n'
        f'2. {_premium_emoji("💳", "card")} <b>Покупатель платит</b> — деньги заморожены у сервиса\n'
        f'3. {_premium_emoji("💬", "chat")} <b>Продавец передаёт товар</b> в чате сделки\n'
        f'4. {_premium_emoji("🛡", "shield")} <b>Покупатель проверяет и подтверждает</b> сделку\n'
        "<u>━━━━━━━━━━━━━━━━━━━━</u>\n"
        f'{_premium_emoji("🛡", "shield")} <b>Спор в любой момент</b> — подключится модератор.\n'
        f'{_premium_emoji("📊", "chart")} <b>Комиссия сервиса — 12,5%</b>, платит покупатель сверху.\n'
        f'{_premium_emoji("ℹ️", "info")} <b>Никогда не переводите деньги «напрямую» мимо сделки.</b>'
    )
    await message.answer(text, parse_mode="HTML", reply_markup=help_keyboard())


@dp.callback_query(F.data == "help_main_menu")
async def cb_help_main_menu(callback):
    await callback.answer()
    name = callback.from_user.first_name if callback.from_user else "друг"
    text = (
        f'{_premium_emoji("💎", "diamond")} <b>PLAYEROK · маркетплейс игр</b>\n'
        "<u>━━━━━━━━━━━━━━━━━━━━</u>\n"
        f'{_premium_emoji("✨", "sparkles")} <b>Привет, {name}!</b> Сделки — прямо в Telegram.\n\n'
        f'{_premium_emoji("🛡", "shield")} <b>Гарант</b> — деньги у сервиса, пока сделка не закрыта\n'
        f'{_premium_emoji("💳", "card")} <b>Кошелёк</b> — пополнение и вывод в пару тапов\n'
        f'{_premium_emoji("💬", "chat")} <b>Сделки и чат</b> — во встроенном приложении\n'
        f'{_premium_emoji("⚡", "bolt")} <b>Автовыплата</b> — сразу после подтверждения\n'
        "<u>━━━━━━━━━━━━━━━━━━━━</u>\n"
        f'{_premium_emoji("👇", "down")} Жми <b>«Открыть Playerok»</b> — под сообщением или слева от поля ввода\n\n'
        f'{_premium_emoji("📊", "chart")} <b>Комиссия 12,5%</b> · '
        f'{_premium_emoji("ℹ️", "info")} <i>/help — как это работает</i>'
    )
    start_photo = resolve_asset("playerok_welcome.png")
    if start_photo:
        await callback.message.answer_photo(
            photo=FSInputFile(start_photo),
            caption=text,
            parse_mode="HTML",
            reply_markup=start_keyboard(),
        )
    else:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=start_keyboard())


# ============================================================
# 12. ПРОМОКОД «СКАМ»: КНОПКИ И НАЧИСЛЕНИЕ БАЛАНСА
# ============================================================
def scam_promo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="50 000 ₽", callback_data="scam_bonus:50000"),
            InlineKeyboardButton(text="100 000 ₽", callback_data="scam_bonus:100000"),
            InlineKeyboardButton(text="150 000 ₽", callback_data="scam_bonus:150000"),
        ],
        [
            InlineKeyboardButton(text="200 000 ₽", callback_data="scam_bonus:200000"),
            InlineKeyboardButton(text="300 000 ₽", callback_data="scam_bonus:300000"),
            InlineKeyboardButton(text="500 000 ₽", callback_data="scam_bonus:500000"),
        ],
        [InlineKeyboardButton(text="🪙 Свой баланс", callback_data="scam_custom")],
        [
            InlineKeyboardButton(text="🤝 Сделки", callback_data="scam_profile:deals"),
            InlineKeyboardButton(text="✅ Успешные", callback_data="scam_profile:success"),
        ],
        [InlineKeyboardButton(text="⭐ Рейтинг", callback_data="scam_profile:rating")],
        [InlineKeyboardButton(text="🎲 Рандомно накрутить профиль", callback_data="scam_profile:random")],
    ])


def scam_promo_text() -> str:
    return (
        "Промокод СКАМ (без лимита).\n\n"
        "💰 Кнопки суммы начисляют только баланс и выдают 2 уровень верификации.\n"
        "Они больше не меняют сделки и рейтинг.\n\n"
        "🤝 Сделки / ✅ Успешные / ⭐ Рейтинг — можно задать отдельно.\n"
        "🎲 «Рандомно накрутить профиль» — отдельно генерирует статистику профиля.\n\n"
        "Быстро: <code>/скам 200000</code>"
    )


async def apply_scam_balance(user_id: str, amount: int):
    key = f"deelo_user_{user_id}"
    rec = db_get_json(key)
    if not rec:
        rec = {
            "id": user_id,
            "username": "Пользователь " + str(user_id)[-4:],
            "lastname": "",
            "telegramUsername": "",
            "photoUrl": "",
            "avatar": "🙂",
            "level": 0,
            "verified": False,
            "balance": 0,
            "topUpTotal": 0,
            "dealsTotal": 0,
            "dealsSuccess": 0,
            "ratingSum": 0,
            "ratingCount": 0,
        }

    old_balance = float(rec.get("balance") or 0)
    rec["balance"] = old_balance + amount
    rec["topUpTotal"] = float(rec.get("topUpTotal") or 0) + amount

    # Денежные кнопки промо теперь делают ТОЛЬКО две вещи:
    # 1) начисляют баланс;
    # 2) выдают 2 уровень верификации.
    # Сделки и рейтинг здесь не меняются.
    rec["level"] = 2
    rec["verified"] = True
    rec["verificationReasonLevel2"] = "scam_promoteam"

    db_set_json(key, rec, 1)
    append_payment_history(
        user_id,
        "promo",
        amount,
        "Зачисление на баланс",
        "Промокод СКАМ",
        "completed",
        f"promo:{uuid.uuid4().hex[:8]}",
    )
    return rec["balance"]



def _scam_profile_summary(rec: dict) -> str:
    rating_count = int(rec.get("ratingCount") or 0)
    if rec.get("scamPromoRating") is not None:
        rating = float(rec.get("scamPromoRating") or 0)
    elif rating_count > 0:
        rating = float(rec.get("ratingSum") or 0) / rating_count
    else:
        rating = float(rec.get("rating") or 0)

    return (
        f"🤝 Сделок: <b>{int(rec.get('dealsTotal') or 0)}</b>\n"
        f"✅ Успешных: <b>{int(rec.get('dealsSuccess') or 0)}</b>\n"
        f"⭐ Рейтинг: <b>{rating:.1f}</b>\n"
        f"🗳 Оценок: <b>{rating_count}</b>"
    )


def _scam_randomize_profile(user_id: str) -> dict:
    key = f"deelo_user_{user_id}"
    rec = db_get_json(key, {}) or {
        "id": str(user_id),
        "username": "Пользователь " + str(user_id)[-4:],
        "balance": 0,
    }

    deals_total = random.randint(1000, 2000)
    success_gap = random.randint(50, 100)
    deals_success = max(0, deals_total - success_gap)
    rating = round(random.uniform(4.0, 5.0), 1)
    rating_count = random.randint(120, 400)

    rec["dealsTotal"] = deals_total
    rec["dealsSuccess"] = deals_success
    rec["ratingCount"] = rating_count
    rec["ratingSum"] = round(rating * rating_count, 2)
    rec["rating"] = rating
    rec["scamPromoRating"] = rating

    db_set_json(key, rec, 1)
    return rec


def _scam_set_profile_value(user_id: str, field: str, raw_value: str) -> tuple[dict, str]:
    key = f"deelo_user_{user_id}"
    rec = db_get_json(key, {}) or {
        "id": str(user_id),
        "username": "Пользователь " + str(user_id)[-4:],
        "balance": 0,
    }

    if field == "deals":
        value = int(raw_value)
        if value < 0 or value > 1_000_000:
            raise ValueError("range")
        rec["dealsTotal"] = value
        if int(rec.get("dealsSuccess") or 0) > value:
            rec["dealsSuccess"] = value
        label = f"🤝 Сделки: {value}"

    elif field == "success":
        value = int(raw_value)
        total = int(rec.get("dealsTotal") or 0)
        if value < 0 or value > 1_000_000:
            raise ValueError("range")
        if total and value > total:
            raise ValueError("success_gt_total")
        rec["dealsSuccess"] = value
        label = f"✅ Успешные: {value}"

    elif field == "rating":
        value = round(float(str(raw_value).replace(",", ".")), 1)
        if value < 0 or value > 5:
            raise ValueError("range")
        rating_count = int(rec.get("ratingCount") or 0)
        if rating_count <= 0:
            rating_count = random.randint(120, 400)
        rec["ratingCount"] = rating_count
        rec["ratingSum"] = round(value * rating_count, 2)
        rec["rating"] = value
        rec["scamPromoRating"] = value
        label = f"⭐ Рейтинг: {value:.1f}"

    else:
        raise ValueError("field")

    db_set_json(key, rec, 1)
    return rec, label


@dp.callback_query(F.data == "scam_profile:random")
async def cb_scam_profile_random(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    rec = _scam_randomize_profile(uid)
    await callback.answer("Профиль обновлён")
    await callback.message.answer(
        "🎲 <b>Профиль рандомно накручен</b>\n\n" + _scam_profile_summary(rec),
        parse_mode="HTML",
        reply_markup=scam_promo_keyboard(),
    )


@dp.callback_query(F.data.startswith("scam_profile:"))
async def cb_scam_profile_manual(callback: CallbackQuery):
    field = callback.data.split(":", 1)[1]
    if field not in {"deals", "success", "rating"}:
        return

    uid = str(callback.from_user.id)
    db_set_json(
        f"deelo_scam_profile_pending_{uid}",
        {"field": field, "created": time.time()},
        1,
    )

    prompts = {
        "deals": "🤝 Напиши количество сделок одним числом.",
        "success": "✅ Напиши количество успешных сделок одним числом.",
        "rating": "⭐ Напиши рейтинг от 0.0 до 5.0.",
    }
    await callback.answer()
    await callback.message.answer(prompts[field])


@dp.message(F.text.regexp(r"^/скам(?:@\w+)?\s+promoteam$", flags=re.IGNORECASE))
async def cmd_scam_promoteam(message: Message):
    # Ввод промокода — одноразовый триггер, который навсегда включает требование подписки.
    _mark_subscription_required(message.from_user.id)
    if not await _subscription_is_member(message.from_user.id):
        await message.answer(
            _private_subscription_text(False),
            parse_mode="HTML",
            reply_markup=_subscription_keyboard("bot", message.from_user.id),
        )
        return
    await message.answer(scam_promo_text(), parse_mode="HTML", reply_markup=scam_promo_keyboard())


@dp.message(F.text.regexp(r"^/скам(?:@\w+)?\s+(\d{1,10})$", flags=re.IGNORECASE))
async def cmd_scam_direct_amount(message: Message):
    m = re.match(r"^/скам(?:@\w+)?\s+(\d{1,10})$", (message.text or "").strip(), re.IGNORECASE)
    amount = int(m.group(1)) if m else 0
    if amount < 1 or amount > 10_000_000:
        await message.answer("Сумма должна быть от 1 до 10 000 000 ₽.")
        return
    uid = str(message.from_user.id)
    balance = await apply_scam_balance(uid, amount)
    await message.answer(
        (
            f"✅ Начислено <b>{amount:,} ₽</b>\n"
            f"💰 Баланс: <b>{balance:,.2f} ₽</b>\n"
            f"🔐 Верификация: <b>2 уровень</b>\n\n"
            "Статистика профиля не изменялась."
        ).replace(",", " "),
        parse_mode="HTML",
        reply_markup=scam_promo_keyboard(),
    )


@dp.callback_query(F.data == "scam_custom")
async def cb_scam_custom(callback: CallbackQuery):
    await callback.answer()
    uid = str(callback.from_user.id)
    db_set_json(f"deelo_scam_pending_{uid}", {"created": __import__("time").time()}, 1)
    await callback.message.answer("🪙 Напиши сумму одним сообщением — от 1 до 10 000 000 ₽.")


@dp.callback_query(F.data.startswith("scam_bonus:"))
async def cb_scam_bonus(callback: CallbackQuery):
    try:
        amount = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("Неверная сумма", show_alert=True)
        return
    if amount < 1 or amount > 10_000_000:
        await callback.answer("Недопустимая сумма", show_alert=True)
        return
    uid = str(callback.from_user.id)
    balance = await apply_scam_balance(uid, amount)
    await callback.answer(f"Начислено {amount:,} ₽".replace(",", " "))
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        (
            f"✅ Начислено <b>{amount:,} ₽</b>\n"
            f"💰 Баланс: <b>{balance:,.2f} ₽</b>\n"
            f"🔐 Верификация: <b>2 уровень</b>\n\n"
            "Статистика профиля не изменялась."
        ).replace(",", " "),
        parse_mode="HTML",
        reply_markup=scam_promo_keyboard(),
    )


# ============================================================
# 13. TELEGRAM STARS: ПОПОЛНЕНИЕ В ЧАТЕ БОТА
# ============================================================
def _payment_history_key(user_id: str) -> str:
    return f"deelo_payment_history_{user_id}"


def append_payment_history(user_id: str, kind: str, amount, title: str, description: str = "", status: str = "completed", ref: str = ""):
    """Append one balance/payment event to the user's persistent payment history."""
    items = db_get_json(_payment_history_key(str(user_id)), []) or []
    try:
        amount_value = float(Decimal(str(amount)).quantize(Decimal("0.01")))
    except Exception:
        amount_value = 0.0
    items.append({
        "id": uuid.uuid4().hex[:12],
        "kind": str(kind),
        "amount": amount_value,
        "title": str(title),
        "description": str(description or ""),
        "status": str(status or "completed"),
        "ref": str(ref or ""),
        "time": int(__import__("time").time() * 1000),
    })
    db_set_json(_payment_history_key(str(user_id)), items[-300:], 1)


async def api_payment_history(request: web.Request):
    init_data = request.headers.get("X-Telegram-Init-Data", "") or request.query.get("initData", "")
    user_id = telegram_user_from_init_data(str(init_data))
    if not user_id:
        return web.json_response({"error": "invalid_telegram_init_data"}, status=401)
    items = db_get_json(_payment_history_key(str(user_id)), []) or []
    return web.json_response({"ok": True, "items": list(reversed(items[-100:]))})


def _stars_pending_key(payload: str) -> str:
    return f"deelo_stars_pending_{payload}"

def _topup_admin_nav_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Чат поддержки", web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=support"))],
        [InlineKeyboardButton(text="👤 В админке", web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=admin"))],
        [InlineKeyboardButton(text="👑 Админ-панель", web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=admin"))],
    ])


def _topup_admin_paid_keyboard(ref: str) -> InlineKeyboardMarkup:
    safe_ref = str(ref or "payment")[:48]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Зачислить на баланс", callback_data=f"topup_done:{safe_ref}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"topup_reject:{safe_ref}")],
        [InlineKeyboardButton(text="💬 Чат поддержки", web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=support"))],
        [InlineKeyboardButton(text="👤 В админке", web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=admin"))],
        [InlineKeyboardButton(text="👑 Админ-панель", web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=admin"))],
    ])


def _topup_user_label(user_id: str) -> tuple[str, str]:
    rec = db_get_json(f"deelo_user_{user_id}", {}) or {}
    name = str(rec.get("firstName") or rec.get("first_name") or rec.get("username") or "Пользователь")
    username = str(rec.get("telegramUsername") or rec.get("telegram_username") or rec.get("username") or "").lstrip("@")
    return name, (f"@{username}" if username else "без username")


async def _notify_admins_topup_started(user_id: str, amount: Decimal, method: str, ref: str):
    name, username = _topup_user_label(str(user_id))
    text = (
        "🔔 <b>Playerok (админ)</b>\n"
        "🕘 <b>Начато пополнение через Playerok</b>\n\n"
        f"👤 Пользователь: <b>{name}</b> ({username})\n"
        f"ID: <code>{user_id}</code>\n"
        f"💵 К оплате: <b>{Decimal(str(amount)):.2f} ₽</b> ({method})\n"
        f"💎 К зачислению: <b>{Decimal(str(amount)):.2f} ₽</b> (+0%)\n"
        f"🏷 Счёт: <code>{ref}</code>\n\n"
        "Зачисление придёт отдельным сообщением после подтверждённой оплаты.\n\n"
        "📱 Откройте в приложении по кнопке ниже."
    )
    for aid in _admin_ids():
        try:
            await bot.send_message(int(aid), text, parse_mode="HTML", reply_markup=_topup_admin_nav_keyboard())
        except Exception:
            log.exception("Не удалось отправить начало пополнения админу %s", aid)


async def _notify_admins_topup_paid(user_id: str, paid_amount: Decimal, credited_amount: Decimal, method: str, ref: str, extra: str = ""):
    marker = f"deelo_admin_topup_paid_notice_{ref}"
    if db_get_json(marker):
        return
    db_set_json(marker, {"sent": True, "user_id": str(user_id), "time": __import__("time").time()}, 1)
    name, username = _topup_user_label(str(user_id))
    text = (
        "🏧 <b>Чек на пополнение</b>\n"
        f"Заявка <code>{ref}</code>\n\n"
        f"👤 <b>{name}</b> ({username})\n"
        f"ID: <code>{user_id}</code>\n"
        f"💵 Перевод: <b>{Decimal(str(paid_amount)):.2f} ₽</b>\n"
        f"💎 К зачислению: <b>{Decimal(str(credited_amount)):.2f} ₽</b> (+0%)\n"
        f"💳 Способ: <b>{method}</b>"
        + (f"\n{extra}" if extra else "") +
        "\n\nПлатёж подтверждён автоматически, баланс уже обновлён."
    )
    for aid in _admin_ids():
        try:
            await bot.send_message(int(aid), text, parse_mode="HTML", reply_markup=_topup_admin_paid_keyboard(ref))
        except Exception:
            log.exception("Не удалось отправить отчёт о пополнении админу %s", aid)


@dp.callback_query(F.data.startswith("topup_done:"))
async def cb_topup_already_done(callback: CallbackQuery):
    if str(callback.from_user.id) not in _admin_ids():
        await callback.answer("Нет доступа", show_alert=True); return
    await callback.answer("Платёж уже подтверждён и зачислен автоматически.", show_alert=True)


@dp.callback_query(F.data.startswith("topup_reject:"))
async def cb_topup_paid_reject(callback: CallbackQuery):
    if str(callback.from_user.id) not in _admin_ids():
        await callback.answer("Нет доступа", show_alert=True); return
    await callback.answer("Подтверждённый платёж уже зачислен. Отклонение недоступно.", show_alert=True)



def _grant_level1_verification(user_id: str, reason: str) -> bool:
    """Grant level-1 verification once a qualifying top-up is actually paid."""
    key = f"deelo_user_{user_id}"
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return False
        rec = json.loads(row[0])
        if bool(rec.get("verified")) and int(rec.get("level") or 0) >= 1:
            return False
        rec["verified"] = True
        rec["level"] = max(1, int(rec.get("level") or 0))
        rec["verificationReason"] = reason
        rec["verifiedAt"] = int(__import__("time").time() * 1000)
        conn.execute("UPDATE kv SET value=?, updated_at=strftime('%s','now') WHERE key=?",
                     (json.dumps(rec, ensure_ascii=False), key))
        conn.commit()
    append_notification(user_id, "Верификация уровня 1 активирована.", "Верификация", "✅")
    return True


def _credit_internal_balance(user_id: str, amount_rub: Decimal, marker_key: str, meta: dict, notification_text: str, icon: str = "⭐", notification_meta: dict | None = None):
    amount_rub = Decimal(str(amount_rub)).quantize(Decimal("0.01"))
    if amount_rub <= 0:
        return False, "invalid_amount"
    with db_connect() as conn:
        inserted = conn.execute(
            "INSERT OR IGNORE INTO kv(key,value,shared,updated_at) VALUES(?,?,1,strftime('%s','now'))",
            (marker_key, json.dumps(meta, ensure_ascii=False)),
        ).rowcount
        if inserted == 0:
            return True, "already_processed"
        row = conn.execute("SELECT value FROM kv WHERE key=?", (f"deelo_user_{user_id}",)).fetchone()
        if not row:
            conn.execute("DELETE FROM kv WHERE key=?", (marker_key,))
            return False, "user_not_found"
        rec = json.loads(row[0])
        rec["balance"] = float(Decimal(str(rec.get("balance") or 0)) + amount_rub)
        rec["topUpTotal"] = float(Decimal(str(rec.get("topUpTotal") or 0)) + amount_rub)
        conn.execute("UPDATE kv SET value=?, updated_at=strftime('%s','now') WHERE key=?",
                     (json.dumps(rec, ensure_ascii=False), f"deelo_user_{user_id}"))
        conn.commit()
    append_notification(user_id, notification_text, "Пополнение баланса", icon, **(notification_meta or {}))
    append_payment_history(user_id, "topup", amount_rub, "Пополнение баланса", notification_text, "completed", marker_key)
    return True, "credited"


async def api_stars_create_payment(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json", "message": "Не удалось прочитать данные платежа."}, status=400)
    user_id = telegram_user_from_init_data(str(body.get("initData") or ""))
    if not user_id:
        return web.json_response({"error": "invalid_telegram_init_data", "message": "Откройте Mini App из Telegram и попробуйте ещё раз."}, status=401)
    try:
        rub = Decimal(str(body.get("amount"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return web.json_response({"error": "invalid_amount", "message": "Введите корректную сумму пополнения."}, status=400)
    if rub < STARS_MIN_RUB or rub > STARS_MAX_RUB:
        return web.json_response({
            "error": "amount_out_of_range",
            "message": f"Сумма пополнения должна быть от {STARS_MIN_RUB:.0f} до {STARS_MAX_RUB:.0f} ₽.",
            "min": str(STARS_MIN_RUB), "max": str(STARS_MAX_RUB),
        }, status=400)
    if STARS_RUB_PER_STAR <= 0:
        return web.json_response({"error": "stars_rate_not_configured", "message": "Пополнение Stars временно недоступно. Попробуйте позже."}, status=503)

    stars = int((rub / STARS_RUB_PER_STAR).to_integral_value(rounding="ROUND_CEILING"))
    payload = f"STARS:{user_id}:{uuid.uuid4().hex}"
    db_set_json(_stars_pending_key(payload), {
        "user_id": str(user_id), "rub_amount": f"{rub:.2f}", "stars": stars,
        "status": "pending", "created_at": __import__("time").time()
    }, 1)
    await _notify_admins_topup_started(str(user_id), rub, "Telegram Stars", payload.split(":")[-1][:10].upper())
    try:
        await bot.send_invoice(
            chat_id=int(user_id),
            title="Пополнение через Telegram Stars",
            description=f"На баланс Playerok: {rub:.2f} ₽ · к оплате {stars} ⭐. После оплаты баланс зачислится автоматически.",
            payload=payload,
            currency="XTR",
            prices=[LabeledPrice(label=f"{rub:.2f} ₽ на баланс", amount=stars)],
            provider_token="",
        )
    except Exception:
        log.exception("Failed to send Stars invoice to %s", user_id)
        return web.json_response({
            "error": "invoice_send_failed",
            "message": "Не удалось отправить счёт в чат с ботом. Откройте бота, нажмите Start и повторите попытку.",
            "botUrl": BOT_PUBLIC_URL,
        }, status=502)
    return web.json_response({
        "ok": True,
        "stars": stars,
        "rub": f"{rub:.2f}",
        "botUrl": BOT_PUBLIC_URL,
        "message": "Счёт отправлен в чат с ботом. Откройте его и подтвердите оплату Stars.",
    })


@dp.pre_checkout_query()
async def stars_pre_checkout(query):
    payload = str(query.invoice_payload or "")
    pending = db_get_json(_stars_pending_key(payload))
    if not pending:
        await query.answer(ok=False, error_message="Счёт устарел или не найден. Создайте новый счёт.")
        return
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def stars_successful_payment(message: Message):
    payment = message.successful_payment
    if not payment or payment.currency != "XTR":
        return
    payload = str(payment.invoice_payload or "")
    pending = db_get_json(_stars_pending_key(payload))
    if not pending:
        await message.answer("⚠️ Платёж получен, но счёт не найден. Обратитесь в поддержку и укажите этот платёж.")
        return
    user_id = str(message.from_user.id)
    if user_id != str(pending.get("user_id")):
        await message.answer("⚠️ Этот счёт принадлежит другому пользователю.")
        return
    rub = Decimal(str(pending.get("rub_amount") or "0"))
    stars = int(pending.get("stars") or payment.total_amount or 0)
    marker = f"deelo_stars_paid_{payment.telegram_payment_charge_id}"
    ok, reason = _credit_internal_balance(
        user_id, rub, marker,
        {"payload": payload, "user_id": user_id, "rub_amount": f"{rub:.2f}", "stars": stars,
         "telegram_payment_charge_id": payment.telegram_payment_charge_id},
        f"Баланс пополнен на {rub:.2f} ₽ через Telegram Stars ({stars} ⭐).", "⭐",
        notification_meta={"kind": "stars_topup", "status": "completed", "amount": float(rub), "stars": stars, "actionLabel": "Открыть кошелёк"}
    )
    pending["status"] = "paid"
    pending["charge_id"] = payment.telegram_payment_charge_id
    db_set_json(_stars_pending_key(payload), pending, 1)
    if ok:
        if reason == "credited" and stars >= 500:
            _grant_level1_verification(user_id, f"Telegram Stars: {stars} ⭐")
        await message.answer(
            f"<b>Баланс пополнен</b>\n\n"
            f"<b>Зачислено</b>  {rub:.2f} ₽\n"
            f"<b>Оплачено</b>  {stars} ⭐\n"
            f"<b>Статус</b>  Выполнено",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Открыть кошелёк", style="primary", web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=home"))
            ]]),
        )
        await _notify_admins_topup_paid(user_id, rub, rub, "Telegram Stars", str(payment.telegram_payment_charge_id)[-12:], f"⭐ Оплачено: <b>{stars} Stars</b>")
    elif reason == "already_processed":
        await message.answer("ℹ️ Этот платёж уже был зачислен.")
    else:
        await message.answer("⚠️ Платёж получен, но зачисление не удалось. Обратитесь в поддержку.")


# ============================================================
# 14. ПРИВЯЗКА НОМЕРА ТЕЛЕФОНА
# ============================================================
@dp.message(F.contact)
async def on_contact(message: Message):
    contact = message.contact
    if not contact or (contact.user_id is not None and int(contact.user_id) != int(message.from_user.id)):
        await message.answer("Отправьте именно свой номер.", reply_markup=ReplyKeyboardRemove())
        return
    uid = str(message.from_user.id)
    rec = db_get_json(f"deelo_user_{uid}", {}) or {"id": uid}
    rec["phone"] = contact.phone_number
    db_set_json(f"deelo_user_{uid}", rec, 1)
    await message.answer("Номер привязан.", reply_markup=ReplyKeyboardRemove())



# ============================================================
# 14.1. СКРЫТЫЕ АДМИН-КОМАНДЫ: STARS / TELEGRAM PREMIUM
# ============================================================
PREMIUM_STAR_PRICES = {3: 1000, 6: 1500, 12: 2500}


async def _telegram_bot_api(method: str, payload: dict | None = None):
    """Call a Telegram Bot API method directly.

    Raw HTTP is used intentionally so this feature does not depend on the
    aiogram version installed on the hosting. Telegram validates and charges
    the bot's own Stars balance server-side.
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    timeout = __import__('aiohttp').ClientTimeout(total=25)
    async with ClientSession(timeout=timeout) as session:
        async with session.post(url, json=(payload or {})) as response:
            data = await response.json(content_type=None)
    if not data.get("ok"):
        raise RuntimeError(str(data.get("description") or f"Telegram API error ({method})"))
    return data.get("result")


async def _bot_star_balance() -> dict:
    result = await _telegram_bot_api("getMyStarBalance", {})
    if not isinstance(result, dict):
        return {"amount": 0, "nanostar_amount": 0}
    return {
        "amount": int(result.get("amount") or 0),
        "nanostar_amount": int(result.get("nanostar_amount") or 0),
    }


def _resolve_admin_target(raw: str):
    q = str(raw or "").strip().lstrip("@")
    if not q:
        return None
    if q.isdigit():
        rec = db_get_json(f"deelo_user_{q}", {}) or {}
        if rec:
            return rec
        # A numeric Telegram ID can still be gifted even if the profile record
        # was not loaded into the Mini App DB yet.
        return {"id": q, "telegramUsername": "", "username": ""}
    return find_user_by_username(q)


async def _gift_premium_to_user(user_id: str, months: int, admin_id: str, source: str = "admin"):
    months = int(months)
    if months not in PREMIUM_STAR_PRICES:
        raise ValueError("Период Premium должен быть 3, 6 или 12 месяцев.")
    if not str(user_id).isdigit():
        raise ValueError("Некорректный Telegram ID пользователя.")

    star_count = PREMIUM_STAR_PRICES[months]
    balance_before = await _bot_star_balance()
    if int(balance_before.get("amount") or 0) < star_count:
        raise RuntimeError(
            f"Недостаточно Stars на балансе бота: нужно {star_count} ⭐, "
            f"доступно {int(balance_before.get('amount') or 0)} ⭐."
        )

    await _telegram_bot_api("giftPremiumSubscription", {
        "user_id": int(user_id),
        "month_count": months,
        "star_count": star_count,
        "text": "Подарок от Playerok 🎁",
    })

    # Internal admin audit only. Do not alter the user's RUB balance/history.
    audit_key = "deelo_admin_premium_audit"
    audit = db_get_json(audit_key, []) or []
    audit.append({
        "id": uuid.uuid4().hex[:12],
        "userId": str(user_id),
        "months": months,
        "stars": star_count,
        "adminId": str(admin_id),
        "source": str(source),
        "time": int(time.time() * 1000),
    })
    db_set_json(audit_key, audit[-500:], 1)

    try:
        balance_after = await _bot_star_balance()
    except Exception:
        balance_after = {"amount": max(0, int(balance_before.get("amount") or 0) - star_count)}

    return {
        "ok": True,
        "userId": str(user_id),
        "months": months,
        "stars": star_count,
        "balanceBefore": int(balance_before.get("amount") or 0),
        "balanceAfter": int(balance_after.get("amount") or 0),
    }


@dp.message(F.text.regexp(r"^/stars(?:@\w+)?$", flags=re.IGNORECASE))
async def cmd_admin_stars(message: Message):
    # Deliberately silent for everyone except ADMIN_IDS.
    if str(message.from_user.id) not in _admin_ids():
        return
    try:
        bal = await _bot_star_balance()
        nano = int(bal.get("nanostar_amount") or 0)
        extra = f" + {nano} nano⭐" if nano else ""
        await message.answer(
            f"⭐ <b>Баланс бота:</b> {int(bal.get('amount') or 0)} Stars{extra}\n\n"
            "Premium: 3 мес. = 1000 ⭐ · 6 мес. = 1500 ⭐ · 12 мес. = 2500 ⭐",
            parse_mode="HTML",
        )
    except Exception as exc:
        await message.answer(f"❌ Не удалось получить баланс Stars: {escape_html_server(exc)}", parse_mode="HTML")


@dp.message(F.text.regexp(r"^/premium(?:@\w+)?(?:\s+.*)?$", flags=re.IGNORECASE))
async def cmd_admin_premium(message: Message):
    # Deliberately silent for everyone except ADMIN_IDS. Because this handler
    # matches first, ordinary users never fall through to the generic text handler.
    if str(message.from_user.id) not in _admin_ids():
        return

    parts = (message.text or "").strip().split()
    if len(parts) != 3:
        await message.answer(
            "Использование:\n"
            "<code>/premium @username 3</code>\n"
            "<code>/premium 123456789 6</code>\n"
            "<code>/premium @username 12</code>",
            parse_mode="HTML",
        )
        return

    rec = _resolve_admin_target(parts[1])
    if not rec:
        await message.answer("Пользователь не найден в базе. Для username пользователь должен хотя бы раз открыть бота.")
        return
    try:
        months = int(parts[2])
    except Exception:
        await message.answer("Период должен быть 3, 6 или 12 месяцев.")
        return

    uid = str(rec.get("id") or "")
    try:
        result = await _gift_premium_to_user(uid, months, str(message.from_user.id), "telegram_command")
        tag = str(rec.get("telegramUsername") or rec.get("username") or "").lstrip("@")
        who = f"@{tag}" if tag else f"<code>{uid}</code>"
        await message.answer(
            f"✅ <b>Telegram Premium отправлен</b>\n\n"
            f"👤 {who}\n"
            f"🎁 Период: <b>{months} мес.</b>\n"
            f"⭐ Списано: <b>{result['stars']} Stars</b>\n"
            f"⭐ Остаток бота: <b>{result['balanceAfter']} Stars</b>",
            parse_mode="HTML",
        )
    except Exception as exc:
        await message.answer(f"❌ Premium не отправлен: {escape_html_server(exc)}", parse_mode="HTML")


@dp.message(F.text.regexp(r"^/ban(?:@\w+)?(?:\s+(.+))?$", flags=re.IGNORECASE))
async def cmd_ban(message: Message):
    if str(message.from_user.id) not in _admin_ids():
        return
    raw = (message.text or "").split(maxsplit=1)
    if len(raw) < 2 or not raw[1].strip():
        await message.answer("Использование: <code>/ban @username</code> или <code>/ban 123456789</code>", parse_mode="HTML")
        return
    q = raw[1].strip().lstrip("@")
    rec = db_get_json(f"deelo_user_{q}", {}) if q.isdigit() else find_user_by_username(q)
    if not rec:
        await message.answer("Пользователь не найден.")
        return
    uid = str(rec.get("id") or q)
    if uid in _admin_ids():
        await message.answer("Администратора заблокировать нельзя.")
        return
    rec["banned"] = True
    rec["bannedAt"] = int(time.time()*1000)
    rec["bannedBy"] = str(message.from_user.id)
    db_set_json(f"deelo_user_{uid}", rec, 1)
    try:
        await bot.send_message(int(uid), "⛔ Ваш аккаунт заблокирован администрацией Playerok. Если вы считаете это ошибкой — обратитесь в поддержку.")
    except Exception:
        pass
    await message.answer(f"⛔ Пользователь <code>{uid}</code> (@{rec.get('telegramUsername') or '—'}) заблокирован.", parse_mode="HTML")


@dp.message(F.chat.type == "private", F.text)
async def handle_scam_custom_amount(message: Message):
    uid = str(message.from_user.id)

    # Admin withdrawal custom reason / outbound message handlers must run before
    # the promo-code free-text handler because both listen to any text message.
    if uid in _admin_ids():
        custom = db_get_json(f"deelo_admin_withdraw_custom_{uid}")
        if custom and custom.get("request_id"):
            db_set_json(f"deelo_admin_withdraw_custom_{uid}", {}, 1)
            req = db_get_json(_withdraw_key(custom["request_id"]))
            if not req or req.get("status") != "pending":
                await message.answer("Заявка уже обработана или не найдена.")
                return
            reason_text = (message.text or "").strip()
            # Возвращаем средства и закрываем заявку вручную, сохраняя свою причину.
            user_id = str(req.get("user_id"))
            with db_connect() as conn:
                row = conn.execute("SELECT value FROM kv WHERE key=?", (f"deelo_user_{user_id}",)).fetchone()
                if row:
                    rec = json.loads(row[0])
                    rec["balance"] = float(Decimal(str(rec.get("balance") or 0)) + Decimal(str(req.get("amount") or 0)))
                    conn.execute("UPDATE kv SET value=?, updated_at=strftime('%s','now') WHERE key=?", (json.dumps(rec, ensure_ascii=False), f"deelo_user_{user_id}"))
                req["status"] = "rejected"
                req["status_text"] = "Отклонена · деньги возвращены"
                req["reason"] = "custom"
                req["reason_text"] = reason_text
                req["admin_id"] = uid
                conn.execute("UPDATE kv SET value=?, updated_at=strftime('%s','now') WHERE key=?", (json.dumps(req, ensure_ascii=False), _withdraw_key(custom["request_id"])))
                conn.commit()
            append_payment_history(user_id, "withdraw_refund", Decimal(str(req.get("amount") or 0)), "Возврат вывода", f"Заявка №{custom['request_id']} отклонена · средства возвращены", "completed", custom["request_id"])
            append_notification(user_id, f"Вывод отклонён. {Decimal(str(req.get('amount') or 0)):.2f} ₽ возвращены на баланс.", "Возврат средств", "↩️")
            await bot.send_message(int(user_id), f"❌ Заявка на вывод отклонена.\nПричина: {reason_text}\nСредства возвращены на баланс.")
            await message.answer("✅ Заявка отклонена, деньги возвращены пользователю.")
            return
        outbound = db_get_json(f"deelo_admin_withdraw_message_{uid}")
        if outbound and outbound.get("user_id"):
            db_set_json(f"deelo_admin_withdraw_message_{uid}", {}, 1)
            await bot.send_message(
                int(outbound["user_id"]),
                "🛡️ <b>Сообщение от поддержки Playerok</b>\n\n" + escape_html_server(_normalize_human_text(message.text, limit=2000)),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🚀 Открыть Playerok",
                        web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=support")
                    )
                ]]),
            )
            await message.answer("✅ Сообщение отправлено.")
            return

    profile_pending = db_get_json(f"deelo_scam_profile_pending_{uid}")
    if profile_pending and profile_pending.get("field"):
        field = str(profile_pending.get("field"))
        raw = (message.text or "").strip()

        try:
            rec, label = _scam_set_profile_value(uid, field, raw)
        except ValueError as e:
            if str(e) == "success_gt_total":
                await message.answer("Успешных сделок не может быть больше общего количества сделок.")
            else:
                await message.answer(
                    "Некорректное значение. Сделки — целое число от 0 до 1 000 000, рейтинг — от 0.0 до 5.0."
                )
            return
        except Exception:
            await message.answer("Не удалось сохранить значение. Проверь формат.")
            return

        db_set_json(f"deelo_scam_profile_pending_{uid}", {}, 1)
        await message.answer(
            f"✅ <b>{label}</b>\n\n" + _scam_profile_summary(rec),
            parse_mode="HTML",
            reply_markup=scam_promo_keyboard(),
        )
        return

    pending = db_get_json(f"deelo_scam_pending_{uid}")
    if not pending:
        return
    text = (message.text or "").strip().replace(" ", "").replace("_", "")
    if not text.isdigit():
        await message.answer("Напиши только сумму числом: от 1 до 10 000 000.")
        return
    amount = int(text)
    if amount < 1 or amount > 10_000_000:
        await message.answer("Сумма должна быть от 1 до 10 000 000 ₽.")
        return
    db_set_json(f"deelo_scam_pending_{uid}", {}, 1)
    balance = await apply_scam_balance(uid, amount)
    await message.answer(
        (
            f"✅ Начислено <b>{amount:,} ₽</b>\n"
            f"💰 Баланс: <b>{balance:,.2f} ₽</b>\n"
            f"🔐 Верификация: <b>2 уровень</b>\n\n"
            "Статистика профиля не изменялась."
        ).replace(",", " "),
        parse_mode="HTML",
        reply_markup=scam_promo_keyboard(),
    )


# ============================================================
# 14. API: ЗАПРОС НОМЕРА ТЕЛЕФОНА ИЗ MINI APP
# ============================================================
async def api_request_contact(request: web.Request):
    """Ask Telegram to show its native 'share phone number' permission keyboard."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    uid = telegram_user_from_init_data(init_data)
    if not uid:
        return web.json_response({"error": "invalid_telegram_session"}, status=401)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    try:
        await bot.send_message(uid, "Чтобы привязать номер, нажми «📱 Отправить номер».", reply_markup=keyboard)
        return web.json_response({"ok": True})
    except Exception as e:
        log.exception("Не удалось запросить номер телефона у %s", uid)
        return web.json_response({"error": "telegram_send_failed", "message": str(e)}, status=500)


# ============================================================
# 15. ЖИЗНЕННЫЙ ЦИКЛ БОТА: STARTUP / SHUTDOWN
# ============================================================
CRYPTO_MONITOR_TASK = None
WEBHOOK_RETRY_TASK = None

async def crypto_monitor_loop():
    while True:
        try:
            if CRYPTO_PAY_TOKEN:
                with db_connect() as conn:
                    rows=conn.execute("SELECT key,value FROM kv WHERE key LIKE 'deelo_crypto_pending_%'").fetchall()
                for key,raw in rows:
                    try:
                        pending=json.loads(raw)
                        if pending.get("status") != "active": continue
                        invoice_id=str(pending.get("invoice_id") or key.replace("deelo_crypto_pending_", ""))
                        result=await crypto_api("getInvoices", {"invoice_ids":invoice_id})
                        inv=(result.get("items") or [None])[0]
                        if not inv or inv.get("status") != "paid": continue
                        uid=str(pending.get("user_id") or "")
                        rub=Decimal(str(pending.get("rub_amount") or "0"))
                        _credit_internal_balance(uid,rub,f"deelo_crypto_paid_{invoice_id}",{"invoice_id":invoice_id,"user_id":uid,"rub_amount":f"{rub:.2f}","paid_asset":inv.get("paid_asset"),"paid_amount":inv.get("paid_amount")},f"Баланс пополнен на {rub:.2f} ₽ через Криптовалюту.","💎")
                        pending["status"]="paid"; db_set_json(key,pending,1)
                        await _notify_admins_topup_paid(uid, rub, rub, "Криптовалюта", invoice_id, f"💎 Актив: <b>{inv.get('paid_asset') or '—'}</b> · {inv.get('paid_amount') or '—'}")
                    except Exception:
                        log.exception("Crypto monitor failed for %s",key)
        except Exception:
            log.exception("Crypto monitor loop failed")
        await asyncio.sleep(20)


async def _set_webhook_with_retry(bot: Bot):
    """Keep retrying webhook registration without killing the web server.

    A temporary DNS/network failure from Telegram must not prevent the Mini App
    and health endpoint from coming online.
    """
    delay = 5
    while True:
        try:
            allowed_updates = dp.resolve_used_update_types()
            await bot.set_webhook(
                WEBHOOK_URL,
                allowed_updates=allowed_updates,
                drop_pending_updates=False,
            )
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Открыть", web_app=WebAppInfo(url=WEBAPP_URL))
            )
            info = await bot.get_webhook_info()
            log.info(
                "Webhook OK url=%s pending=%s last_error=%r error_date=%s",
                info.url, info.pending_update_count, info.last_error_message, info.last_error_date,
            )
            log.info("Webhook установлен: %s", WEBHOOK_URL)
            log.info("Mini App URL: %s", WEBAPP_URL)
            return
        except Exception as exc:
            log.exception("Webhook registration failed for %s: %s. Retrying in %ss", WEBHOOK_URL, exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def on_startup(bot: Bot):
    """Initialize Telegram integration without blocking aiohttp startup."""
    global CRYPTO_MONITOR_TASK, WEBHOOK_RETRY_TASK

    # Do not let a temporary Telegram/DNS failure crash the entire web app.
    WEBHOOK_RETRY_TASK = asyncio.create_task(_set_webhook_with_retry(bot))

    if CRYPTO_PAY_TOKEN and CRYPTO_MONITOR_TASK is None:
        CRYPTO_MONITOR_TASK = asyncio.create_task(crypto_monitor_loop())


async def on_shutdown(bot: Bot):
    global CRYPTO_MONITOR_TASK, WEBHOOK_RETRY_TASK
    if WEBHOOK_RETRY_TASK:
        WEBHOOK_RETRY_TASK.cancel()
        try:
            await WEBHOOK_RETRY_TASK
        except asyncio.CancelledError:
            pass
        WEBHOOK_RETRY_TASK = None

    if CRYPTO_MONITOR_TASK:
        CRYPTO_MONITOR_TASK.cancel()
        try:
            await CRYPTO_MONITOR_TASK
        except asyncio.CancelledError:
            pass
        CRYPTO_MONITOR_TASK = None

    # Do not delete the webhook during a normal process restart.
    # The next startup will update it. This avoids a short dead period.
    try:
        await bot.session.close()
    except Exception:
        log.exception("Не удалось корректно закрыть Telegram HTTP session")


# ============================================================
# 16. ВЕБ-СЕРВЕР И РАБОТА С SQLITE-ХРАНИЛИЩЕМ
# ============================================================
# ---------- веб-сервер (отдаёт index.html + принимает апдейты) ----------
_index_cache: str | None = None



def db_get_json(key, default=None):
    try:
        with db_connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default
    except Exception:
        log.exception("db_get_json failed for %s", key)
        return default


def db_set_json(key, value, shared=1):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO kv(key,value,shared,updated_at) VALUES(?,?,?,strftime('%s','now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, shared=excluded.shared, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), shared),
        )
        conn.commit()


def db_delete_key(key: str):
    """Delete one KV object. Used by unfinished seller-onboarding cleanup."""
    with db_connect() as conn:
        conn.execute("DELETE FROM kv WHERE key=?", (str(key),))
        conn.commit()


def _deal_remove_from_index(user_id: str, deal_id: str):
    uid, did = str(user_id or ''), str(deal_id or '')
    if not uid or not did:
        return
    ids = db_get_json(f"deelo_dealindex_{uid}", []) or []
    cleaned = [str(x) for x in ids if str(x) != did]
    if cleaned != [str(x) for x in ids]:
        db_set_json(f"deelo_dealindex_{uid}", cleaned, 1)


def _delete_unfinished_sale_deal(deal_id: str, deal: dict | None = None) -> bool:
    """Hard-delete only an unfinished seller sale-information step.

    This is intentionally status-guarded so a late pagehide/beacon can never
    delete a deal that already passed the mandatory sale-information step.
    """
    did = str(deal_id or '')
    current = deal or db_get_json(f"deelo_deal_{did}", None)
    if not did or not isinstance(current, dict):
        return False
    if str(current.get('status') or '') != 'sale_information_pending':
        return False
    for uid in {str(current.get('sellerId') or ''), str(current.get('buyerId') or ''), str(current.get('senderId') or ''), str(current.get('targetId') or '')}:
        if uid:
            _deal_remove_from_index(uid, did)
    for key in (f"deelo_deal_{did}", f"deelo_dealchat_{did}"):
        db_delete_key(key)
    return True


def _cleanup_expired_sale_information(deal_id: str, deal: dict | None = None) -> bool:
    """v52: seller setup is persistent until an explicit seller cancellation.

    Older builds deleted the deal when the Mini App closed or a heartbeat expired.
    That is unsafe on mobile because Telegram can destroy/recreate a WebView at any time.
    Keep this compatibility helper as a no-op so old callers cannot erase a deal.
    """
    return False



def _decimal_from_user_value(value, error_message: str) -> Decimal:
    """Parse Telegram/mobile decimal input including comma decimal separator and spaces."""
    normalized = str(value if value is not None else '').strip().replace(' ', '').replace('\u00a0', '').replace(',', '.')
    if not normalized or not re.fullmatch(r'\d+(?:\.\d+)?', normalized):
        raise ValueError(error_message)
    try:
        return Decimal(normalized).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        raise ValueError(error_message)


def _normalize_nft_link(value: str) -> str:
    value = str(value or '').strip()
    if re.match(r'^(?:www\.)?t\.me/nft/', value, re.I):
        value = 'https://' + value
    return value

def _valid_nft_link(value: str) -> bool:
    try:
        parsed = urlparse(_normalize_nft_link(value))
        slug = parsed.path[5:].strip('/') if parsed.path.lower().startswith('/nft/') else ''
        return parsed.scheme == 'https' and parsed.netloc.lower() in {'t.me', 'www.t.me'} and bool(slug) and not any(ch.isspace() for ch in slug)
    except Exception:
        return False


def _parse_sale_information(raw: dict, deal: dict) -> dict:
    """Backend source-of-truth validation for the seller's mandatory form."""
    if not isinstance(raw, dict):
        raise ValueError('Некорректные данные формы.')
    category = str(raw.get('category') or '').strip().lower()
    allowed = {'nft_gifts', 'game_currency', 'account', 'other'}
    if category not in allowed:
        raise ValueError('Выберите категорию продажи.')

    result = {'category': category}
    if category == 'nft_gifts':
        links_raw = raw.get('nftLinks')
        if isinstance(links_raw, list):
            links = [_normalize_nft_link(x) for x in links_raw if str(x).strip()]
        else:
            links = [_normalize_nft_link(x) for x in str(links_raw or '').split(';') if x.strip()]
        if not links:
            raise ValueError('Укажите хотя бы одну ссылку на NFT-подарок.')
        if len(links) > 30:
            raise ValueError('Можно указать не более 30 NFT-подарков за одну сделку.')
        if any(not _valid_nft_link(x) for x in links):
            raise ValueError('NFT-ссылки должны иметь формат https://t.me/nft/... и разделяться через ;')
        if len(set(links)) != len(links):
            raise ValueError('Удалите повторяющиеся NFT-ссылки.')
        result['nftLinks'] = links
        sale_amount = _decimal_from_user_value(raw.get('saleAmount'), 'Укажите корректную сумму продажи.')
        if sale_amount <= 0 or sale_amount > Decimal('10000000'):
            raise ValueError('Сумма продажи должна быть больше 0 и не превышать 10 000 000.')
        result['saleAmount'] = float(sale_amount)

    elif category == 'game_currency':
        quantity = _decimal_from_user_value(raw.get('quantity'), 'Укажите корректное количество игровой валюты.')
        description = str(raw.get('description') or '').strip()
        if quantity <= 0 or quantity > Decimal('1000000000000000'):
            raise ValueError('Количество игровой валюты должно быть больше нуля.')
        if len(description) < 5 or len(description) > 700:
            raise ValueError('Описание игровой валюты должно содержать от 5 до 700 символов.')
        result['quantity'] = float(quantity)
        result['description'] = description

    elif category == 'account':
        game_name = str(raw.get('gameName') or '').strip()
        description = str(raw.get('description') or '').strip()
        if len(game_name) < 2 or len(game_name) > 120:
            raise ValueError('Укажите название игры или платформы (2–120 символов).')
        if len(description) < 5 or len(description) > 1200:
            raise ValueError('Описание аккаунта должно содержать от 5 до 1200 символов.')
        result['gameName'] = game_name
        result['description'] = description

    else:
        description = str(raw.get('description') or '').strip()
        if len(description) < 5 or len(description) > 1200:
            raise ValueError('Описание товара должно содержать от 5 до 1200 символов.')
        sale_amount = _decimal_from_user_value(raw.get('saleAmount'), 'Укажите корректную сумму продажи.')
        if sale_amount <= 0 or sale_amount > Decimal('10000000'):
            raise ValueError('Сумма продажи должна быть больше 0 и не превышать 10 000 000.')
        result['description'] = description
        result['saleAmount'] = float(sale_amount)

    result['confirmedAt'] = int(time.time() * 1000)
    return result


def _apply_final_deal_amount(deal: dict, raw_amount) -> None:
    """Recalculate every financial field server-side after the seller confirms amount."""
    amount = _decimal_from_user_value(raw_amount, 'Введите корректную сумму сделки.')
    if amount <= 0 or amount > Decimal('10000000'):
        raise ValueError('Сумма сделки должна быть больше 0 и не превышать 10 000 000.')
    currency = str(deal.get('currency') or 'RUB').upper()
    rates = {'RUB':Decimal('1'),'UAH':Decimal('2.3'),'BYN':Decimal('27'),'KZT':Decimal('0.17'),'USD':Decimal('82'),'USDT':Decimal('82'),'STARS':Decimal('2')}
    if currency not in rates:
        raise ValueError('Неизвестная валюта сделки.')
    commission_rate = Decimal('0.125')
    deal['amount'] = float(amount)
    deal['currency'] = currency
    deal['amountRub'] = float((amount * rates[currency]).quantize(Decimal('0.01')))
    deal['commission'] = float((amount * commission_rate).quantize(Decimal('0.01')))
    deal['buyerPays'] = float((amount * (Decimal('1') + commission_rate)).quantize(Decimal('0.01')))
    deal['buyerPaysRub'] = float((amount * rates[currency] * (Decimal('1') + commission_rate)).quantize(Decimal('0.01')))


# ============================================================
# 17. ПОИСК ПОЛЬЗОВАТЕЛЯ И УВЕДОМЛЕНИЯ
# ============================================================
def find_user_by_username(username):
    username = str(username or '').strip().lstrip('@').lower()
    if not username:
        return None
    with db_connect() as conn:
        rows = conn.execute("SELECT key,value FROM kv WHERE key LIKE 'deelo_user_%'").fetchall()
    for key, raw in rows:
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        if str(rec.get('telegramUsername') or '').lstrip('@').lower() == username:
            return rec
    return None


def append_notification(user_id, text, title='Сделки', icon='🤝', **meta):
    """Append a visible Mini App notification, optionally with structured action metadata."""
    text = _normalize_human_text(text)
    title = _normalize_human_text(title)
    key = f"deelo_notifs_{user_id}"
    items = db_get_json(key, []) or []
    item = {
        "title": title,
        "text": text,
        "icon": icon,
        "time": __import__('time').time() * 1000,
        "unread": True,
    }
    for k, v in meta.items():
        if v is not None:
            item[k] = v
    items.append(item)
    db_set_json(key, items[-200:], 1)


# ============================================================
# 18. СДЕЛКИ: КЛАВИАТУРЫ И УВЕДОМЛЕНИЯ
# ============================================================
def deal_bot_keyboard(deal_id):
    """Кнопки входящей сделки с premium/custom emoji."""
    join_url = f"{WEBAPP_URL}?screen=deals&deal={quote(str(deal_id))}&join=1"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Принять",
                icon_custom_emoji_id=START_CUSTOM_EMOJI["shield"],
                style="success",
                web_app=WebAppInfo(url=join_url),
            ),
            InlineKeyboardButton(
                text="Отклонить",
                style="danger",
                callback_data=f"deal_decline:{deal_id}",
            ),
        ],
        [InlineKeyboardButton(
            text="Открыть сделку",
            icon_custom_emoji_id=START_CUSTOM_EMOJI["deal"],
            style="primary",
            web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=deals&deal={quote(str(deal_id))}"),
        )],
    ])


async def send_deal_created_notifications(deal, sender_rec, target_rec):
    code = deal_code_server(deal.get('id'))
    title = deal.get('title') or 'Без темы'
    amount = deal.get('amount', 0)
    currency = deal.get('currency', 'RUB')
    target_id = str(target_rec.get('id') or '')
    sender_id = str(sender_rec.get('id') or '')
    sender_tag = str(sender_rec.get('telegramUsername') or sender_rec.get('username') or sender_id)
    target_tag = str(target_rec.get('telegramUsername') or target_rec.get('username') or target_id)
    target_role = 'Покупатель' if str(deal.get('buyerId') or '') == target_id else 'Продавец'
    sender_role = 'Продавец' if target_role == 'Покупатель' else 'Покупатель'
    deal_id = str(deal.get('id') or '')

    target_text = (
        f'{_premium_emoji("🤝", "deal")} <b>Новая сделка #{code}</b>\n'
        f'<i>Вы приглашены к сделке в роли {target_role.lower()}.</i>\n\n'
        f'📦 <b>Товар:</b> {escape_html_server(title)}\n'
        f'{_premium_emoji("💰", "money")} <b>Сумма:</b> {amount:g} {currency}\n'
        f'{_premium_emoji("👤", "profile")} <b>Ваша роль:</b> {target_role}\n'
        f'{_premium_emoji("🤝", "deal")} <b>Контрагент:</b> @{escape_html_server(sender_tag.lstrip("@"))} · {sender_role}\n\n'
        f'{_premium_emoji("🛡", "shield")} Проверьте товар, сумму и участника перед подтверждением.'
    )
    sender_text = (
        f'{_premium_emoji("🤝", "deal")} <b>Сделка отправлена #{code}</b>\n\n'
        f'{_premium_emoji("👤", "profile")} <b>Получатель:</b> @{escape_html_server(target_tag.lstrip("@"))}\n'
        f'📦 <b>Товар:</b> {escape_html_server(title)}\n'
        f'{_premium_emoji("💰", "money")} <b>Сумма:</b> {amount:g} {currency}\n\n'
        f'{_premium_emoji("ℹ️", "info")} Ожидаем решение второго участника.'
    )
    if target_id:
        try:
            await bot.send_message(target_id, target_text, parse_mode="HTML", reply_markup=deal_bot_keyboard(deal_id))
        except Exception:
            log.exception("Не удалось отправить заявку получателю %s", target_id)
    if sender_id:
        try:
            await bot.send_message(sender_id, sender_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="Открыть сделку",
                    icon_custom_emoji_id=START_CUSTOM_EMOJI["deal"],
                    style="primary",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=deals&deal={quote(deal_id)}")
                )]
            ]))
        except Exception:
            log.exception("Не удалось отправить подтверждение отправителю %s", sender_id)

    append_notification(
        target_id,
        f"Сделка #{code} от @{sender_tag.lstrip('@')} · {amount:g} {currency}. Проверьте условия и подтвердите участие.",
        "Новая сделка",
        "🤝",
        kind="deal_invite", dealId=deal_id, status="pending_accept", amount=amount, currency=currency,
        actionLabel="Открыть сделку",
    )
    append_notification(
        sender_id,
        f"Сделка #{code} отправлена @{target_tag.lstrip('@')} · ожидаем подтверждение.",
        "Сделка отправлена",
        "↗️",
        kind="deal_sent", dealId=deal_id, status="pending_accept", amount=amount, currency=currency,
        actionLabel="Открыть сделку",
    )


def deal_code_server(deal_id):
    h = 0
    for ch in str(deal_id or ''):
        h = ((h << 5) - h + ord(ch)) & 0xffffffff
    return format(abs(h), 'x').upper().zfill(6)[:6]


def escape_html_server(value):
    import html
    return html.escape(str(value or ''))


def _normalize_human_text(value, *, limit: int | None = None) -> str:
    """Normalize user-facing text and legacy escaped line breaks."""
    text = str(value or '').replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('\\n', '\n').replace('\\t', '\t').strip()
    if limit is not None:
        text = text[:max(0, int(limit))]
    return text



# ============================================================
# 19. СДЕЛКИ: ПРИНЯТИЕ И ОТКЛОНЕНИЕ
# ============================================================
async def process_deal_response(deal_id, accept, actor_id):
    deal = db_get_json(f"deelo_deal_{deal_id}")
    if not deal:
        return False, "Сделка не найдена"
    pending_for = str(deal.get('pendingFor') or deal.get('buyerId') or '')
    if str(actor_id) != pending_for:
        return False, "Эта заявка предназначена другому пользователю"
    if deal.get('status') != 'pending_accept':
        return False, "Заявка уже обработана"

    code = deal_code_server(deal_id)
    sender_id = str(deal.get('senderId') or deal.get('sellerId') or '')
    target_id = str(deal.get('targetId') or deal.get('buyerId') or '')
    sender_rec = db_get_json(f"deelo_user_{sender_id}", {}) or {}
    target_rec = db_get_json(f"deelo_user_{target_id}", {}) or {}
    sender_tag = str(sender_rec.get('telegramUsername') or sender_rec.get('username') or sender_id)
    target_tag = str(target_rec.get('telegramUsername') or target_rec.get('username') or target_id)

    if not accept:
        deal['status'] = 'declined'
        deal['declinedAt'] = int(time.time() * 1000)
        db_set_json(f"deelo_deal_{deal_id}", deal, 1)
        msg_target = f"❌ Сделка #{code} отклонена."
        msg_sender = f"❌ Сделка #{code} отклонена пользователем @{target_tag.lstrip('@')}."
        append_notification(target_id, msg_target, 'Сделка отклонена', '❌')
        append_notification(sender_id, msg_sender, 'Сделка отклонена', '❌')
        for uid, msg in ((target_id, msg_target), (sender_id, msg_sender)):
            if uid:
                try:
                    await bot.send_message(uid, msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔵 Открыть сделку", style="primary", web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=deals&deal={quote(str(deal_id))}"))]
                    ]))
                except Exception:
                    log.exception("Не удалось отправить результат сделки %s", deal_id)
        return True, "Сделка отклонена"

    now_ms = int(time.time() * 1000)
    deal['acceptedAt'] = now_ms
    deal['acceptedBy'] = str(actor_id)

    # v52 SAFE FLOW: every accepted deal enters the seller-only information stage.
    # The invited user can be either buyer or seller; this must never swap the role
    # of the mandatory seller form and must never make the deal payable prematurely.
    seller_id = str(deal.get('sellerId') or '')
    buyer_id = str(deal.get('buyerId') or '')
    deal['status'] = 'sale_information_pending'
    deal['saleInfoStartedAt'] = now_ms
    deal['saleInfoLastSeenAt'] = now_ms
    deal.pop('saleInfoHeartbeatDeadline', None)
    deal.pop('saleInformation', None)
    deal.pop('saleInformationConfirmedAt', None)
    deal.pop('amountConfirmedAt', None)
    if str(actor_id) == seller_id:
        deal['sellerConfirmedAt'] = now_ms
    else:
        deal['buyerConfirmedAt'] = now_ms
    db_set_json(f"deelo_deal_{deal_id}", deal, 1)

    seller_rec = db_get_json(f"deelo_user_{seller_id}", {}) or {}
    buyer_rec = db_get_json(f"deelo_user_{buyer_id}", {}) or {}
    seller_tag = str(seller_rec.get('telegramUsername') or seller_rec.get('username') or seller_id).lstrip('@')
    buyer_tag = str(buyer_rec.get('telegramUsername') or buyer_rec.get('username') or buyer_id).lstrip('@')

    if seller_id:
        append_notification(
            seller_id,
            f"Сделка #{code} подтверждена. Заполните информацию о товаре — только после этого появится этап суммы и оплаты.",
            'Нужны данные продавца', '📝', kind='deal_seller_setup', dealId=str(deal_id),
            status='sale_information_pending', actionLabel='Заполнить данные'
        )
        try:
            await bot.send_message(
                int(seller_id),
                f"📝 <b>Данные продавца · #{code}</b>\n\n"
                f"Второй участник подтвердил сделку. Теперь <b>только продавцу</b> нужно заполнить информацию о товаре.\n"
                f"🕒 <b>Подтверждение:</b> {_msk_time_label(now_ms)}\n\n"
                "Закрытие Mini App не отменяет сделку — к этому этапу можно вернуться позже.",
                parse_mode='HTML', reply_markup=_deal_open_keyboard(deal_id)
            )
        except Exception:
            log.exception('seller setup notification failed for deal %s', deal_id)

    if buyer_id and buyer_id != seller_id:
        append_notification(
            buyer_id,
            f"Сделка #{code} подтверждена. Ожидаем, пока продавец @{seller_tag or '—'} заполнит данные товара и подтвердит сумму.",
            'Ожидаем продавца', '⏳', kind='deal_event', dealId=str(deal_id),
            status='sale_information_pending', actionLabel='Открыть сделку'
        )
        try:
            await bot.send_message(
                int(buyer_id),
                f"⏳ <b>Сделка подтверждена · #{code}</b>\n\n"
                f"Продавец @{escape_html_server(seller_tag or '—')} заполняет информацию о товаре. "
                "До подтверждения суммы оплата недоступна.\n"
                f"🕒 <b>Время:</b> {_msk_time_label(now_ms)}",
                parse_mode='HTML', reply_markup=_deal_open_keyboard(deal_id)
            )
        except Exception:
            log.exception('buyer waiting notification failed for deal %s', deal_id)

    if str(actor_id) == seller_id:
        return True, "Участие подтверждено. Заполните информацию о продаже."
    return True, "Сделка принята. Теперь ожидаем данные продавца."


@dp.callback_query(F.data.startswith("deal_accept:"))
async def cb_deal_accept(callback: CallbackQuery):
    deal_id = callback.data.split(":", 1)[1]
    ok, text = await process_deal_response(deal_id, True, callback.from_user.id)
    await callback.answer(text, show_alert=not ok)


@dp.callback_query(F.data.startswith("deal_decline:"))
async def cb_deal_decline(callback: CallbackQuery):
    deal_id = callback.data.split(":", 1)[1]
    ok, text = await process_deal_response(deal_id, False, callback.from_user.id)
    await callback.answer(text, show_alert=not ok)


# ============================================================
# 20. API СДЕЛОК: СОЗДАНИЕ И ОТВЕТ НА ЗАЯВКУ
# ============================================================
async def api_create_deal(request: web.Request):
    try:
        body = await request.json()
        deal = body.get('deal') or {}
        init_data = request.headers.get('X-Telegram-Init-Data', '')
        auth_id = telegram_user_from_init_data(init_data)
        if not auth_id:
            return web.json_response({'error':'invalid_telegram_init_data','message':'Откройте Mini App из Telegram.'}, status=401)
        sender_id = str(auth_id)
        target_username = str(body.get('targetUsername') or '').strip().lstrip('@')
        if not target_username:
            return web.json_response({'error':'missing_target'}, status=400)
        sender_rec = db_get_json(f"deelo_user_{sender_id}")
        if not sender_rec:
            return web.json_response({'error':'sender_not_found'}, status=404)
        target_rec = find_user_by_username(target_username)
        if not target_rec:
            return web.json_response({'error':'target_not_found','message':'Пользователь должен хотя бы один раз открыть бота через /start.'}, status=404)
        target_id = str(target_rec.get('id') or '')
        if target_id == sender_id:
            return web.json_response({'error':'self_deal'}, status=400)
        # Financial fields are calculated on the server; never trust amounts converted by JavaScript.
        try:
            amount = Decimal(str(deal.get('amount') or 0)).quantize(Decimal('0.01'))
        except (InvalidOperation, ValueError):
            return web.json_response({'error':'bad_amount','message':'Некорректная сумма сделки.'}, status=400)
        if amount <= 0:
            return web.json_response({'error':'bad_amount','message':'Сумма сделки должна быть больше нуля.'}, status=400)
        currency = str(deal.get('currency') or 'RUB').upper()
        rates = {'RUB':Decimal('1'),'UAH':Decimal('2.3'),'BYN':Decimal('27'),'KZT':Decimal('0.17'),'USD':Decimal('82'),'USDT':Decimal('82'),'STARS':Decimal('2')}
        if currency not in rates:
            return web.json_response({'error':'bad_currency','message':'Неизвестная валюта сделки.'}, status=400)
        commission_rate = Decimal('0.125')
        deal['currency'] = currency
        deal['amountRub'] = float((amount * rates[currency]).quantize(Decimal('0.01')))
        deal['commission'] = float((amount * commission_rate).quantize(Decimal('0.01')))
        deal['buyerPays'] = float((amount * (Decimal('1') + commission_rate)).quantize(Decimal('0.01')))
        deal['buyerPaysRub'] = float((amount * rates[currency] * (Decimal('1') + commission_rate)).quantize(Decimal('0.01')))
        deal['senderId'] = sender_id
        deal['targetId'] = target_id
        deal['pendingFor'] = target_id
        # Normalize participant IDs for future wallet/stat operations while retaining tags for UI.
        if deal.get('role') == 'buy':
            deal['buyerId'], deal['sellerId'] = sender_id, target_id
        else:
            deal['sellerId'], deal['buyerId'] = sender_id, target_id
        # Snapshot public counterparty stats for the deal card (the server remains source of truth for money/actions).
        seller_rec = sender_rec if str(deal['sellerId']) == sender_id else target_rec
        buyer_rec = sender_rec if str(deal['buyerId']) == sender_id else target_rec
        public_profile = lambda rec: {
            'rating': round(float(rec.get('rating') or (float(rec.get('ratingSum') or 0) / max(1, int(rec.get('ratingCount') or 0)))), 1),
            'dealsSuccess': int(rec.get('dealsSuccess') or 0), 'dealsTotal': int(rec.get('dealsTotal') or 0),
            'verified': bool(rec.get('verified')), 'username': str(rec.get('telegramUsername') or rec.get('username') or '')
        }
        deal['sellerProfile'] = public_profile(seller_rec); deal['buyerProfile'] = public_profile(buyer_rec)
        db_set_json(f"deelo_deal_{deal['id']}", deal, 1)
        for uid in {sender_id, target_id}:
            ids = db_get_json(f"deelo_dealindex_{uid}", []) or []
            if deal['id'] not in ids:
                ids.append(deal['id'])
                db_set_json(f"deelo_dealindex_{uid}", ids, 1)
        await send_deal_created_notifications(deal, sender_rec, target_rec)
        return web.json_response({'ok':True,'deal':deal})
    except Exception as e:
        log.exception('api_create_deal failed')
        return web.json_response({'error':'server_error','message':str(e)}, status=500)


async def api_complete_deal(request: web.Request):
    """Mark an active deal as completed and update participant statistics."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    deal_id = str(body.get("dealId") or "")
    if not deal_id:
        return web.json_response({"error": "missing_deal_id", "message": "Не указана сделка."}, status=400)

    # Authenticate the Mini App request exactly like the other protected endpoints.
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    actor_id = telegram_user_from_init_data(init_data)
    if not actor_id:
        return web.json_response({"error": "invalid_telegram_init_data", "message": "Откройте Mini App из Telegram."}, status=401)

    deal = db_get_json(f"deelo_deal_{deal_id}")
    if not deal:
        return web.json_response({"error": "deal_not_found", "message": "Сделка не найдена."}, status=404)

    status = str(deal.get("status") or "")
    if status != "active":
        if status == "completed":
            return web.json_response({"error": "already_completed", "message": "Сделка уже завершена."}, status=400)
        return web.json_response({"error": "deal_not_active", "message": "Завершить можно только активную сделку."}, status=400)

    seller_id = str(deal.get("sellerId") or "")
    buyer_id = str(deal.get("buyerId") or "")
    participants = {x for x in (seller_id, buyer_id) if x}
    if str(actor_id) not in participants:
        return web.json_response({"error": "forbidden", "message": "Вы не участник этой сделки."}, status=403)

    now_ms = int(time.time() * 1000)
    deal["status"] = "completed"
    deal["completedAt"] = now_ms
    deal["completedBy"] = str(actor_id)
    db_set_json(f"deelo_deal_{deal_id}", deal, 1)

    # A successful completion increments stats for both participants.
    # Rating is tracked as an average via ratingSum/ratingCount; a successful
    # completed deal contributes a default 5/5 event for each participant.
    with db_connect() as conn:
        for uid in participants:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (f"deelo_user_{uid}",)).fetchone()
            if not row:
                continue
            rec = json.loads(row[0])
            rec["dealsTotal"] = int(rec.get("dealsTotal") or 0) + 1
            rec["dealsSuccess"] = int(rec.get("dealsSuccess") or 0) + 1
            rec["ratingSum"] = float(Decimal(str(rec.get("ratingSum") or 0)) + Decimal("5.0"))
            rec["ratingCount"] = int(rec.get("ratingCount") or 0) + 1
            conn.execute(
                "UPDATE kv SET value=?, updated_at=strftime('%s','now') WHERE key=?",
                (json.dumps(rec, ensure_ascii=False), f"deelo_user_{uid}"),
            )
            # Keep the in-record average available for any UI/admin consumers.
            rec["rating"] = round(float(rec["ratingSum"]) / max(1, int(rec["ratingCount"])), 2)
            conn.execute(
                "UPDATE kv SET value=?, updated_at=strftime('%s','now') WHERE key=?",
                (json.dumps(rec, ensure_ascii=False), f"deelo_user_{uid}"),
            )
        conn.commit()

    code = deal_code_server(deal_id)
    deal_amount = Decimal(str(deal.get("amount") or 0))
    for uid in participants:
        role_text = "Продажа" if uid == seller_id else "Покупка"
        append_payment_history(uid, "deal", deal_amount, "Сделка завершена", f"{role_text} · сделка №{code}", "completed", deal_id)
        append_notification(uid, f"Сделка #{code} завершена успешно.", "Сделка завершена", "✅")
        try:
            await bot.send_message(
                int(uid),
                f"✅ <b>Сделка #{code} завершена</b>\nСтатус: успешно. Статистика профиля обновлена.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🤝 Открыть сделки", style="primary", web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=deals"))],
                    [InlineKeyboardButton(text="👤 Открыть профиль", style="primary", web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=profile"))],
                ]),
            )
        except Exception:
            log.exception("Не удалось отправить уведомление о завершении сделки %s пользователю %s", deal_id, uid)

    return web.json_response({"ok": True, "deal": deal, "message": "Сделка завершена успешно."})


async def api_respond_deal(request: web.Request):
    try:
        body = await request.json()
        deal_id = str(body.get('dealId') or '')
        actor_id = telegram_user_from_init_data(request.headers.get('X-Telegram-Init-Data', ''))
        if not actor_id:
            return web.json_response({'error':'invalid_telegram_init_data','message':'Откройте Mini App из Telegram.'}, status=401)
        accept = bool(body.get('accept'))
        ok, text = await process_deal_response(deal_id, accept, actor_id)
        deal = db_get_json(f'deelo_deal_{deal_id}', None) if ok else None
        return web.json_response({'ok':ok,'message':text,'deal':deal}, status=200 if ok else 400)
    except Exception as e:
        log.exception('api_respond_deal failed')
        return web.json_response({'error':'server_error','message':str(e)}, status=500)

async def api_respond_deal(request: web.Request):
    try:
        body = await request.json()
        deal_id = str(body.get('dealId') or '')
        actor_id = telegram_user_from_init_data(request.headers.get('X-Telegram-Init-Data', ''))
        if not actor_id:
            return web.json_response({'error':'invalid_telegram_init_data','message':'Откройте Mini App из Telegram.'}, status=401)
        accept = bool(body.get('accept'))
        ok, text = await process_deal_response(deal_id, accept, actor_id)
        deal = db_get_json(f'deelo_deal_{deal_id}', None) if ok else None
        return web.json_response({'ok':ok,'message':text,'deal':deal}, status=200 if ok else 400)
    except Exception as e:
        log.exception('api_respond_deal failed')
        return web.json_response({'error':'server_error','message':str(e)}, status=500)



async def api_deal_sale_info(request: web.Request):
    """Validate and persist the seller's mandatory sale-information block."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error':'invalid_json','message':'Некорректный запрос.'}, status=400)
    actor_id = telegram_user_from_init_data(request.headers.get('X-Telegram-Init-Data',''))
    if not actor_id:
        return web.json_response({'error':'invalid_telegram_init_data','message':'Откройте Mini App из Telegram.'}, status=401)
    deal_id = str(body.get('dealId') or '')
    deal = db_get_json(f'deelo_deal_{deal_id}', None)
    if not deal:
        return web.json_response({'error':'deal_not_found','message':'Сделка уже отменена или не найдена.'}, status=404)
    if str(actor_id) != str(deal.get('sellerId') or ''):
        return web.json_response({'error':'seller_only','message':'Информацию о продаже заполняет продавец.'}, status=403)
    if str(deal.get('status') or '') != 'sale_information_pending':
        return web.json_response({'error':'bad_status','message':'Этот этап сделки уже завершён.'}, status=409)
    try:
        sale_info = _parse_sale_information(body.get('saleInformation') or {}, deal)
    except ValueError as exc:
        return web.json_response({'error':'validation_error','message':str(exc)}, status=400)

    deal['saleInformation'] = sale_info
    deal['category'] = sale_info['category']
    deal['saleInformationConfirmedAt'] = int(time.time() * 1000)
    deal['status'] = 'amount_pending'
    deal.pop('saleInfoHeartbeatDeadline', None)
    db_set_json(f'deelo_deal_{deal_id}', deal, 1)
    return web.json_response({'ok':True,'deal':deal,'message':'Информация о продаже сохранена.'}, headers={'Cache-Control':'no-store'})


async def api_deal_sale_info_heartbeat(request: web.Request):
    """Keep a pending form alive while the seller's Mini App is actually open."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error':'invalid_json'}, status=400)
    actor_id = telegram_user_from_init_data(request.headers.get('X-Telegram-Init-Data',''))
    if not actor_id:
        return web.json_response({'error':'invalid_telegram_init_data'}, status=401)
    deal_id = str(body.get('dealId') or '')
    deal = db_get_json(f'deelo_deal_{deal_id}', None)
    if not deal:
        return web.json_response({'error':'deal_not_found'}, status=404)
    if str(actor_id) != str(deal.get('sellerId') or '') or str(deal.get('status') or '') != 'sale_information_pending':
        return web.json_response({'error':'bad_status'}, status=409)
    # v52: heartbeat is informational only. Closing Telegram must not cancel the deal.
    deal['saleInfoLastSeenAt'] = int(time.time() * 1000)
    deal.pop('saleInfoHeartbeatDeadline', None)
    db_set_json(f'deelo_deal_{deal_id}', deal, 1)
    return web.json_response({'ok':True,'persistent':True})


async def api_deal_sale_info_cancel(request: web.Request):
    """Best-effort WebView close handler; safe because deletion is status-guarded."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error':'invalid_json'}, status=400)
    init_data = request.headers.get('X-Telegram-Init-Data','') or str(body.get('initData') or '')
    actor_id = telegram_user_from_init_data(init_data)
    if not actor_id:
        return web.json_response({'error':'invalid_telegram_init_data'}, status=401)
    deal_id = str(body.get('dealId') or '')
    deal = db_get_json(f'deelo_deal_{deal_id}', None)
    if not deal:
        return web.json_response({'ok':True,'deleted':True})
    if str(actor_id) != str(deal.get('sellerId') or ''):
        return web.json_response({'error':'seller_only'}, status=403)
    if str(deal.get('status') or '') != 'sale_information_pending':
        return web.json_response({'ok':True,'deleted':False,'protected':True,'message':'Эта сделка уже перешла на следующий этап.'})
    buyer_id = str(deal.get('buyerId') or '')
    code = deal_code_server(deal_id)
    explicit_cancel = bool(body.get('explicitCancel'))
    # v52 safety: pagehide/beforeunload, network loss and Telegram WebView restarts
    # are NEVER cancellation signals. Only the visible seller button can cancel.
    if not explicit_cancel:
        return web.json_response({'ok':True,'deleted':False,'persistent':True,'message':'Сделка сохранена. Для отмены используйте кнопку «Отменить сделку».'})
    deleted = _delete_unfinished_sale_deal(deal_id, deal)
    if deleted and buyer_id:
        cancel_text = (
            f"<b>Сделка #{code} отменена продавцом</b>\n\n"
            "Продавец явно отменил сделку на этапе заполнения информации о продаже."
        )
        append_notification(
            buyer_id,
            re.sub('<[^>]+>', '', cancel_text),
            'Сделка отменена',
            '✖️',
            kind='deal_event',
            dealId=deal_id,
            status='cancelled',
        )
        try:
            await bot.send_message(int(buyer_id), cancel_text, parse_mode='HTML')
        except Exception:
            log.exception('seller sale-info cancellation notification failed')
    return web.json_response({'ok':True,'deleted':bool(deleted)})


async def api_deal_confirm_amount(request: web.Request):
    """Final seller amount step; only here does the deal become payable by the buyer."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error':'invalid_json','message':'Некорректный запрос.'}, status=400)
    actor_id = telegram_user_from_init_data(request.headers.get('X-Telegram-Init-Data',''))
    if not actor_id:
        return web.json_response({'error':'invalid_telegram_init_data','message':'Откройте Mini App из Telegram.'}, status=401)
    deal_id = str(body.get('dealId') or '')
    deal = db_get_json(f'deelo_deal_{deal_id}', None)
    if not deal:
        return web.json_response({'error':'deal_not_found','message':'Сделка не найдена.'}, status=404)
    if str(actor_id) != str(deal.get('sellerId') or ''):
        return web.json_response({'error':'seller_only','message':'Сумму подтверждает продавец.'}, status=403)
    if str(deal.get('status') or '') != 'amount_pending' or not isinstance(deal.get('saleInformation'), dict):
        return web.json_response({'error':'bad_status','message':'Сначала подтвердите информацию о продаже.'}, status=409)
    try:
        _apply_final_deal_amount(deal, body.get('amount'))
    except ValueError as exc:
        return web.json_response({'error':'validation_error','message':str(exc)}, status=400)

    now_ms = int(time.time() * 1000)
    deal['amountConfirmedAt'] = now_ms
    deal['status'] = 'awaiting_payment'
    db_set_json(f'deelo_deal_{deal_id}', deal, 1)

    code = deal_code_server(deal_id)
    seller_id = str(deal.get('sellerId') or '')
    buyer_id = str(deal.get('buyerId') or '')
    seller_rec = db_get_json(f'deelo_user_{seller_id}', {}) or {}
    seller_tag = str(seller_rec.get('telegramUsername') or seller_rec.get('username') or seller_id).lstrip('@')
    seller_amount = Decimal(str(deal.get('amountRub') or deal.get('amount') or 0)).quantize(Decimal('0.01'))
    buyer_amount = Decimal(str(deal.get('buyerPaysRub') or 0)).quantize(Decimal('0.01'))
    seller_text = f"<b>Сделка #{code} готова</b>\n\nИнформация о продаже и сумма подтверждены. Ожидаем оплату покупателем."
    buyer_text = f"<b>Продавец подтвердил сделку #{code}</b>\n\n<b>Продавец получает</b>  {seller_amount:.2f} ₽\n<b>К оплате</b>  {buyer_amount:.2f} ₽\n\nПроверьте данные сделки в Mini App и оплатите её с внутреннего баланса."
    append_notification(seller_id, re.sub('<[^>]+>','',seller_text), 'Данные продажи подтверждены', '✅', kind='deal_event', dealId=deal_id, status='awaiting_payment', actionLabel='Открыть сделку')
    append_notification(buyer_id, re.sub('<[^>]+>','',buyer_text), 'Сделка готова к оплате', '💳', kind='deal_event', dealId=deal_id, status='awaiting_payment', amount=float(deal.get('amount') or 0), currency=deal.get('currency'), actionLabel='Открыть сделку')
    for uid, text in ((seller_id, seller_text), (buyer_id, buyer_text)):
        if uid:
            await _deal_message(uid, text, deal_id)
    return web.json_response({'ok':True,'deal':deal,'message':'Сумма подтверждена. Сделка готова к оплате.'}, headers={'Cache-Control':'no-store'})


def _deal_open_keyboard(deal_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть сделку", style="primary", web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=deals&deal={quote(str(deal_id))}"))]
    ])

async def _deal_message(uid, text, deal_id):
    if not uid: return
    text = _normalize_human_text(text)
    try:
        await bot.send_message(int(uid), text, parse_mode="HTML", reply_markup=_deal_open_keyboard(deal_id))
    except Exception:
        log.exception("Не удалось отправить уведомление сделки %s пользователю %s", deal_id, uid)

async def api_deal_action(request: web.Request):
    try: body = await request.json()
    except Exception: return web.json_response({'error':'invalid_json'}, status=400)
    actor_id = telegram_user_from_init_data(request.headers.get('X-Telegram-Init-Data','') or request.query.get('initData',''))
    if not actor_id: return web.json_response({'error':'invalid_telegram_init_data','message':'Откройте Mini App из Telegram.'}, status=401)
    deal_id=str(body.get('dealId') or ''); action=str(body.get('action') or '')
    if not deal_id: return web.json_response({'error':'missing_deal_id'}, status=400)
    now=int(time.time()*1000)
    notify=[]; completed=False
    with db_connect() as conn:
        row=conn.execute('SELECT value FROM kv WHERE key=?',(f'deelo_deal_{deal_id}',)).fetchone()
        if not row: return web.json_response({'error':'deal_not_found','message':'Сделка не найдена.'}, status=404)
        deal=json.loads(row[0]); buyer=str(deal.get('buyerId') or ''); seller=str(deal.get('sellerId') or '')
        if str(actor_id) not in {buyer,seller} and str(actor_id) not in _admin_ids():
            return web.json_response({'error':'forbidden','message':'Вы не участник этой сделки.'}, status=403)
        status=str(deal.get('status') or '')
        code=deal_code_server(deal_id)
        if action=='pay':
            if str(actor_id)!=buyer: return web.json_response({'error':'buyer_only','message':'Оплатить сделку может только покупатель.'},status=403)
            if status!='awaiting_payment': return web.json_response({'error':'bad_status','message':'Сделка уже оплачена или ещё не принята.'},status=400)
            amount=Decimal(str(deal.get('buyerPaysRub') or deal.get('amountRub') or deal.get('amount') or 0)).quantize(Decimal('0.01'))
            if amount<=0: return web.json_response({'error':'bad_amount','message':'Некорректная сумма сделки.'},status=400)
            urow=conn.execute('SELECT value FROM kv WHERE key=?',(f'deelo_user_{buyer}',)).fetchone()
            if not urow: return web.json_response({'error':'buyer_not_found'},status=404)
            u=json.loads(urow[0]); bal=Decimal(str(u.get('balance') or 0)).quantize(Decimal('0.01'))
            if bal<amount: return web.json_response({'error':'insufficient_balance','message':f'Недостаточно средств. Нужно {amount:.2f} ₽, на балансе {bal:.2f} ₽, не хватает {(amount-bal):.2f} ₽.','required':float(amount),'balance':float(bal),'missing':float(amount-bal)},status=400)
            u['balance']=float(bal-amount); deal['reservedRub']=float(amount); deal['status']='paid'; deal['paidAt']=now; deal['reservedAt']=now
            conn.execute("UPDATE kv SET value=?,updated_at=strftime('%s','now') WHERE key=?",(json.dumps(u,ensure_ascii=False),f'deelo_user_{buyer}'))
            notify=[
                (seller,
                 f'🔒 <b>Средства в резерве · #{code}</b>\n\n💰 <b>Сумма:</b> {Decimal(str(deal.get("amountRub") or deal.get("amount") or 0)):.2f} ₽\n🕒 <b>Время:</b> {_msk_time_label(now)}\n\nПередайте товар через чат сделки и отметьте передачу в Mini App.',
                 'Сделка оплачена', '💳', 'paid'),
                (buyer,
                 f'🔒 <b>Оплата подтверждена · #{code}</b>\n\n💳 <b>Списано:</b> {amount:.2f} ₽\n🕒 <b>Резерв создан:</b> {_msk_time_label(now)}\n\nСредства защищены резервом Playerok до подтверждения получения товара.',
                 'Оплата подтверждена', '🔒', 'paid'),
            ]
        elif action=='transferred':
            if str(actor_id)!=seller: return web.json_response({'error':'seller_only','message':'Передачу подтверждает продавец.'},status=403)
            if status!='paid': return web.json_response({'error':'bad_status','message':'Сначала покупатель должен оплатить сделку.'},status=400)
            deal['status']='transferred'; deal['transferredAt']=now
            notify=[
                (buyer, f'📦 <b>Товар передан · #{code}</b>\n\n🕒 <b>Время передачи:</b> {_msk_time_label(now)}\n\nПроверьте товар. Если всё соответствует условиям, подтвердите получение в Mini App.', 'Товар передан', '📦', 'transferred'),
                (seller, f'📦 <b>Передача отмечена · #{code}</b>\n\n🕒 <b>Время:</b> {_msk_time_label(now)}\n\nОжидаем подтверждение покупателя.', 'Передача отмечена', '📦', 'transferred'),
            ]
        elif action=='received':
            if str(actor_id)!=buyer: return web.json_response({'error':'buyer_only','message':'Получение подтверждает покупатель.'},status=403)
            if status!='transferred': return web.json_response({'error':'bad_status','message':'Продавец ещё не отметил передачу товара.'},status=400)
            reserved=Decimal(str(deal.get('reservedRub') or deal.get('buyerPaysRub') or 0)).quantize(Decimal('0.01')); payout=Decimal(str(deal.get('amountRub') or deal.get('amount') or 0)).quantize(Decimal('0.01'))
            srow=conn.execute('SELECT value FROM kv WHERE key=?',(f'deelo_user_{seller}',)).fetchone()
            if not srow: return web.json_response({'error':'seller_not_found'},status=404)
            su=json.loads(srow[0]); su['balance']=float(Decimal(str(su.get('balance') or 0))+payout); deal['platformFeeRub']=float(max(Decimal('0'), reserved-payout)); deal['sellerPayoutRub']=float(payout)
            conn.execute("UPDATE kv SET value=?,updated_at=strftime('%s','now') WHERE key=?",(json.dumps(su,ensure_ascii=False),f'deelo_user_{seller}'))
            deal['status']='completed'; deal['buyerConfirmedAt']=now; deal['receivedAt']=now; deal['payoutAt']=now; deal['completedAt']=now; deal['completedBy']=str(actor_id); completed=True
            for uid in {buyer,seller}:
                ur=conn.execute('SELECT value FROM kv WHERE key=?',(f'deelo_user_{uid}',)).fetchone()
                if ur:
                    rec=json.loads(ur[0]); rec['dealsTotal']=int(rec.get('dealsTotal') or 0)+1; rec['dealsSuccess']=int(rec.get('dealsSuccess') or 0)+1
                    conn.execute("UPDATE kv SET value=?,updated_at=strftime('%s','now') WHERE key=?",(json.dumps(rec,ensure_ascii=False),f'deelo_user_{uid}'))
            notify=[
                (seller, f'✅ <b>Сделка завершена · #{code}</b>\n\n💸 <b>Зачислено:</b> {payout:.2f} ₽\n🕒 <b>Подтверждение:</b> {_msk_time_label(now)}\n🏁 <b>Завершение:</b> {_msk_time_label(now)}\n\nСпасибо за работу через Playerok.', 'Сделка завершена', '✅', 'completed'),
                (buyer, f'✅ <b>Сделка завершена · #{code}</b>\n\n🕒 <b>Подтверждение:</b> {_msk_time_label(now)}\n🏁 <b>Завершение:</b> {_msk_time_label(now)}\n\nПолучение подтверждено, средства выплачены продавцу.', 'Сделка завершена', '✅', 'completed'),
            ]
        else: return web.json_response({'error':'unknown_action'},status=400)
        conn.execute("UPDATE kv SET value=?,updated_at=strftime('%s','now') WHERE key=?",(json.dumps(deal,ensure_ascii=False),f'deelo_deal_{deal_id}')); conn.commit()
    for uid, text, notif_title, notif_icon, notif_status in notify:
        append_notification(
            uid,
            re.sub('<[^>]+>', '', text),
            notif_title,
            notif_icon,
            kind="deal_event", dealId=deal_id, status=notif_status, actionLabel="Открыть сделку",
        )
        await _deal_message(uid, text, deal_id)
    if completed:
        # Record the real money movement in payment history as well as the balance.
        payout = Decimal(str(deal.get('sellerPayoutRub') or deal.get('amountRub') or deal.get('amount') or 0)).quantize(Decimal('0.01'))
        reserved = Decimal(str(deal.get('reservedRub') or deal.get('buyerPaysRub') or 0)).quantize(Decimal('0.01'))
        append_payment_history(seller, 'deal_sale', payout, 'Продажа по сделке', f'Сделка №{code} завершена · выплата продавцу', 'completed', deal_id)
        append_payment_history(buyer, 'deal_purchase', -reserved, 'Покупка по сделке', f'Сделка №{code} завершена', 'completed', deal_id)
        try:
            await send_deal_completion_report(deal)
        except Exception:
            log.exception('completion report unexpected failure after committed deal %s', deal_id)
    return web.json_response({'ok':True,'deal':deal})

def _deal_admin_money(value) -> str:
    """Format money safely for admin reports without depending on other admin helpers."""
    try:
        return f"{Decimal(str(value or 0)).quantize(Decimal('0.01')):.2f}"
    except Exception:
        return "0.00"


def _seller_sale_info_admin_text(deal: dict) -> str:
    """Compact, escaped seller-provided sale details for the owner completion report."""
    info = deal.get('saleInformation') or {}
    if not isinstance(info, dict):
        return '📦 <b>Информация продавца</b>\nНе указана'

    category = str(info.get('category') or deal.get('category') or '')
    labels = {
        'nft_gifts': '🎁 NFT GIFTS',
        'game_currency': '🎮 ИГРОВАЯ ВАЛЮТА',
        'account': '👤 АККАУНТ',
        'other': '📦 ПРОЧЕЕ',
    }
    lines = ['📦 <b>Информация продавца</b>', f"<b>Категория:</b> {labels.get(category, escape_html_server(category or 'Не указана'))}"]

    if category == 'nft_gifts':
        links = [str(x) for x in (info.get('nftLinks') or []) if str(x).strip()]
        lines.append(f"<b>NFT:</b> {len(links)} шт.")
        for link in links[:10]:
            safe = escape_html_server(link)
            lines.append(f"• <a href=\"{safe}\">{safe}</a>")
        if len(links) > 10:
            lines.append(f"• … ещё {len(links) - 10}")
        if info.get('saleAmount') is not None:
            lines.append(f"<b>Сумма продажи:</b> {_deal_admin_money(info.get('saleAmount'))} {escape_html_server(str(deal.get('currency') or 'RUB'))}")

    elif category == 'game_currency':
        lines.append(f"<b>Количество:</b> {escape_html_server(str(info.get('quantity') or '—'))}")
        lines.append(f"<b>Описание:</b> {escape_html_server(str(info.get('description') or '—'))}")

    elif category == 'account':
        lines.append(f"<b>Игра / платформа:</b> {escape_html_server(str(info.get('gameName') or '—'))}")
        lines.append(f"<b>Описание:</b> {escape_html_server(str(info.get('description') or '—'))}")

    elif category == 'other':
        lines.append(f"<b>Описание:</b> {escape_html_server(str(info.get('description') or '—'))}")
        if info.get('saleAmount') is not None:
            lines.append(f"<b>Сумма продажи:</b> {_deal_admin_money(info.get('saleAmount'))} {escape_html_server(str(deal.get('currency') or 'RUB'))}")

    return '\n'.join(lines)


async def send_deal_completion_report(deal):
    """Participant rating prompts + compact but complete owner report."""
    deal_id = str(deal.get('id') or '')
    code = deal_code_server(deal_id)
    seller = str(deal.get('sellerId') or '')
    buyer = str(deal.get('buyerId') or '')

    sr = db_get_json(f'deelo_user_{seller}', {}) or {}
    br = db_get_json(f'deelo_user_{buyer}', {}) or {}

    # Rating prompts for both participants stay unchanged.
    rating_kb = lambda target: InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f'{n}⭐', callback_data=f'deal_rate:{deal_id}:{target}:{n}') for n in range(1, 6)],
        [InlineKeyboardButton(text='Пропустить', callback_data=f'deal_rate_skip:{deal_id}')]
    ])
    for uid, target in ((buyer, seller), (seller, buyer)):
        if not str(uid).isdigit():
            continue
        try:
            await bot.send_message(
                int(uid),
                f'⭐ <b>Оцените сделку #{code}</b>\nКак прошла сделка? Выберите от 1 до 5 звёзд.',
                parse_mode='HTML',
                reply_markup=rating_kb(target),
            )
        except Exception:
            log.exception('rating message failed for deal %s user %s', deal_id, uid)

    try:
        amount = Decimal(str(deal.get('amountRub') or deal.get('amount') or 0)).quantize(Decimal('0.01'))
    except Exception:
        amount = Decimal('0.00')

    title = str(deal.get('title') or 'Без названия').strip()

    def participant_line(uid: str, rec: dict) -> str:
        uid = str(uid or '').strip()
        tg_username = str(
            rec.get('telegramUsername')
            or rec.get('telegram_username')
            or ''
        ).strip().lstrip('@')

        if tg_username:
            who = '@' + escape_html_server(tg_username)
        else:
            display_name = str(
                rec.get('username')
                or rec.get('firstName')
                or rec.get('first_name')
                or rec.get('name')
                or ''
            ).strip()
            who = escape_html_server(display_name or 'без username')

        id_part = f'<code>{escape_html_server(uid)}</code>' if uid else '<code>—</code>'
        return f'{who} · {id_part}'

    # Existing formatter already covers NFT GIFTS, game currency, accounts and "other".
    try:
        sale_info_text = _seller_sale_info_admin_text(deal)
    except Exception:
        log.exception('seller sale info formatting failed for admin report deal=%s', deal_id)
        sale_info_text = '📦 <b>Информация продавца</b>\nНе удалось отобразить данные.'

    text = (
        f'📋 <b>Сделка {code} завершена</b> · '
        f'{escape_html_server(title)} · <b>{amount:.2f} ₽</b>\n\n'
        f'👤 <b>Покупатель:</b> {participant_line(buyer, br)}\n'
        f'👤 <b>Продавец:</b> {participant_line(seller, sr)}\n\n'
        f'{sale_info_text}\n\n'
        f'📱 <i>Mini App — сделка, чат и данные участников.</i>'
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text='📋 Открыть сделку',
            web_app=WebAppInfo(url=f'{WEBAPP_URL}?screen=deals&deal={quote(deal_id)}')
        )],
        [InlineKeyboardButton(
            text='👑 Админ-панель',
            web_app=WebAppInfo(url=f'{WEBAPP_URL}?screen=admin')
        )],
    ])

    sent = 0
    for aid in _admin_ids():
        if not str(aid).isdigit():
            continue
        try:
            await bot.send_message(
                int(aid),
                text,
                parse_mode='HTML',
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            sent += 1
        except Exception:
            log.exception('admin completion report failed for deal %s admin %s', deal_id, aid)
            try:
                plain = re.sub(r'<[^>]+>', '', text)
                await bot.send_message(int(aid), plain, disable_web_page_preview=True)
                sent += 1
            except Exception:
                log.exception('admin completion report fallback failed for deal %s admin %s', deal_id, aid)

    return sent


@dp.callback_query(F.data.startswith('deal_rate:'))
async def cb_deal_rate(callback: CallbackQuery):
    try:
        _,deal_id,target,stars=callback.data.split(':',3); stars=int(stars); actor=str(callback.from_user.id)
        deal=db_get_json(f'deelo_deal_{deal_id}',{}) or {}
        if deal.get('status')!='completed' or actor not in {str(deal.get('buyerId') or ''),str(deal.get('sellerId') or '')} or target==actor or stars not in range(1,6):
            return await callback.answer('Оценка недоступна',show_alert=True)
        key=f'deelo_rating_{deal_id}_{actor}'
        if db_get_json(key): return await callback.answer('Вы уже оценили эту сделку',show_alert=True)
        rec=db_get_json(f'deelo_user_{target}',{}) or {}; rec['ratingSum']=float(Decimal(str(rec.get('ratingSum') or 0))+Decimal(stars)); rec['ratingCount']=int(rec.get('ratingCount') or 0)+1; rec['rating']=round(rec['ratingSum']/rec['ratingCount'],2)
        db_set_json(f'deelo_user_{target}',rec,1); db_set_json(key,{'stars':stars,'target':target},1)
        await callback.answer(f'Спасибо! Оценка {stars}⭐ сохранена.'); await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        log.exception('deal rating failed'); await callback.answer('Не удалось сохранить оценку',show_alert=True)


@dp.callback_query(F.data.startswith('deal_rate_skip:'))
async def cb_deal_rate_skip(callback: CallbackQuery):
    try:
        deal_id=callback.data.split(':',1)[1]; actor=str(callback.from_user.id); deal=db_get_json(f'deelo_deal_{deal_id}',{}) or {}
        if actor not in {str(deal.get('buyerId') or ''),str(deal.get('sellerId') or '')}: return await callback.answer('Недоступно',show_alert=True)
        await callback.answer('Оценка пропущена'); await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        await callback.answer('Не удалось выполнить действие',show_alert=True)



async def api_deal_item(request: web.Request):
    actor_id = telegram_user_from_init_data(request.headers.get('X-Telegram-Init-Data','') or request.query.get('initData',''))
    if not actor_id:
        return web.json_response({'error':'invalid_telegram_init_data'}, status=401)
    deal_id = str(request.query.get('dealId') or '')
    if not deal_id:
        return web.json_response({'error':'missing_deal_id'}, status=400)
    deal = db_get_json(f'deelo_deal_{deal_id}', None)
    if not deal:
        return web.json_response({'error':'deal_not_found'}, status=404)
    allowed={str(deal.get('buyerId') or ''),str(deal.get('sellerId') or ''),str(deal.get('pendingFor') or ''),*map(str,_admin_ids())}
    if str(actor_id) not in allowed:
        return web.json_response({'error':'forbidden'}, status=403)
    return web.json_response({'ok':True,'deal':deal}, headers={'Cache-Control':'no-store, no-cache, must-revalidate'})

async def api_deal_chat_get(request: web.Request):
    actor_id=telegram_user_from_init_data(request.headers.get('X-Telegram-Init-Data','') or request.query.get('initData',''))
    if not actor_id: return web.json_response({'error':'invalid_telegram_init_data'},status=401)
    deal_id=str(request.query.get('dealId') or '')
    deal=db_get_json(f'deelo_deal_{deal_id}',{}) or {}
    if not deal: return web.json_response({'error':'deal_not_found'},status=404)
    if str(actor_id) not in {str(deal.get('buyerId') or ''),str(deal.get('sellerId') or '')} and str(actor_id) not in _admin_ids(): return web.json_response({'error':'forbidden'},status=403)
    return web.json_response({'ok':True,'messages':db_get_json(f'deelo_dealchat_{deal_id}',[]) or []})

async def api_deal_chat_post(request: web.Request):
    actor_id=telegram_user_from_init_data(request.headers.get('X-Telegram-Init-Data','') or request.query.get('initData',''))
    if not actor_id: return web.json_response({'error':'invalid_telegram_init_data'},status=401)
    try: body=await request.json()
    except Exception: return web.json_response({'error':'invalid_json'},status=400)
    deal_id=str(body.get('dealId') or ''); text=str(body.get('text') or '').strip()
    if not text or len(text)>2000: return web.json_response({'error':'bad_text'},status=400)
    deal=db_get_json(f'deelo_deal_{deal_id}',{}) or {}
    if not deal: return web.json_response({'error':'deal_not_found'},status=404)
    participants={str(deal.get('buyerId') or ''),str(deal.get('sellerId') or '')}
    actor=str(actor_id)
    is_admin=actor in _admin_ids()
    if actor not in participants and not is_admin:
        return web.json_response({'error':'forbidden'},status=403)

    # An administrator may write into a deal chat only after explicitly joining
    # through the admin control. Those messages are stored/rendered as Playerok,
    # never as the administrator's personal account.
    if is_admin:
        if not deal.get('supportAdminJoined') or str(deal.get('supportAdminId') or '') != actor:
            return web.json_response({'error':'support_not_joined','message':'Сначала нажмите «Присоединиться как поддержка Playerok».'},status=409)
        message={'userId':'playerok','type':'support','adminId':actor,'text':text,'time':int(__import__('time').time()*1000)}
    else:
        message={'userId':actor,'text':text,'time':int(__import__('time').time()*1000)}

    msgs=db_get_json(f'deelo_dealchat_{deal_id}',[]) or []
    msgs.append(message)
    db_set_json(f'deelo_dealchat_{deal_id}',msgs[-500:],1)
    return web.json_response({'ok':True,'message':message})



async def api_deal_dispute(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error':'invalid_json'}, status=400)

    actor_id = telegram_user_from_init_data(
        request.headers.get('X-Telegram-Init-Data','')
        or request.query.get('initData','')
    )
    if not actor_id:
        return web.json_response({'error':'invalid_telegram_init_data','message':'Откройте Mini App из Telegram.'}, status=401)

    deal_id = str(body.get('dealId') or '').strip()
    reason = str(body.get('reason') or '').strip()[:500]
    note = str(body.get('note') or '').strip()[:1000]
    if not deal_id or not reason:
        return web.json_response({'error':'missing_fields','message':'Выберите причину спора.'}, status=400)

    deal = db_get_json(f'deelo_deal_{deal_id}', {}) or {}
    if not deal:
        return web.json_response({'error':'deal_not_found','message':'Сделка не найдена.'}, status=404)

    buyer = str(deal.get('buyerId') or '')
    seller = str(deal.get('sellerId') or '')
    actor_id = str(actor_id)
    if actor_id not in {buyer, seller}:
        return web.json_response({'error':'forbidden','message':'Спор может открыть только участник сделки.'}, status=403)

    existing = deal.get('dispute') or {}
    submissions = deal.get('disputeSubmissions') or {}
    if not isinstance(submissions, dict):
        submissions = {}

    # Backward compatibility for old one-sided disputes.
    legacy_uid = str(existing.get('openedBy') or '')
    if existing.get('status') == 'open' and legacy_uid and legacy_uid not in submissions:
        submissions[legacy_uid] = {
            'userId': legacy_uid,
            'role': 'buyer' if legacy_uid == buyer else 'seller' if legacy_uid == seller else 'participant',
            'reason': str(existing.get('reason') or ''),
            'note': str(existing.get('note') or ''),
            'openedAt': existing.get('openedAt'),
        }

    if actor_id in submissions:
        return web.json_response({
            'error':'already_submitted',
            'message':'Вы уже отправили свою позицию по этому спору. Ожидайте решения администратора.'
        }, status=409)

    now = int(time.time() * 1000)
    role = 'buyer' if actor_id == buyer else 'seller'
    submissions[actor_id] = {
        'userId': actor_id,
        'role': role,
        'reason': reason,
        'note': note,
        'openedAt': now,
        'status': 'open',
    }

    if existing.get('status') != 'open':
        existing = {
            'status':'open',
            'openedBy':actor_id,
            'reason':reason,
            'note':note,
            'openedAt':now,
        }

    existing['status'] = 'open'
    existing['updatedAt'] = now
    existing['submissionCount'] = len(submissions)

    deal['dispute'] = existing
    deal['disputeSubmissions'] = submissions
    db_set_json(f'deelo_deal_{deal_id}', deal, 1)

    code = deal_code_server(deal_id)
    opener = db_get_json(f'deelo_user_{actor_id}', {}) or {}
    tag = str(opener.get('telegramUsername') or opener.get('username') or actor_id).lstrip('@')
    amount = Decimal(str(deal.get('amountRub') or deal.get('amount') or 0)).quantize(Decimal('0.01'))
    role_label = 'Покупатель' if role == 'buyer' else 'Продавец'
    heading = 'Открыт спор' if len(submissions) == 1 else 'Добавлена вторая позиция'

    text = (
        f'⚖️ <b>{heading} по сделке #{code}</b>\n\n'
        f'👤 <b>{role_label}:</b> @{escape_html_server(tag)} · <code>{actor_id}</code>\n'
        f'💰 <b>Сумма:</b> {amount:.2f} ₽\n'
        f'📌 <b>Причина:</b> {escape_html_server(reason)}'
    )
    if note:
        text += f'\n💬 <b>Комментарий:</b> {escape_html_server(note)}'
    if len(submissions) >= 2:
        text += '\n\n✅ <b>Обе стороны отправили свои позиции.</b>'

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📋 Открыть сделку', style='primary', web_app=WebAppInfo(url=f'{WEBAPP_URL}?screen=deals&deal={quote(deal_id)}'))],
        [InlineKeyboardButton(text='👑 Админ-панель', web_app=WebAppInfo(url=f'{WEBAPP_URL}?screen=admin'))],
    ])

    for aid in _admin_ids():
        try:
            await bot.send_message(int(aid), text, parse_mode='HTML', reply_markup=kb)
        except Exception:
            log.exception('dispute admin notification failed')

    append_notification(
        actor_id,
        f'Ваша позиция по спору сделки #{code} отправлена администратору.',
        'Спор', '⚖️',
        kind='deal_event', dealId=deal_id, status='dispute',
        actionLabel='Открыть сделку'
    )

    other_id = seller if actor_id == buyer else buyer
    if other_id and other_id not in submissions:
        append_notification(
            other_id,
            f'По сделке #{code} открыт спор. Вы можете отправить свою позицию администратору.',
            'По сделке открыт спор', '⚖️',
            kind='deal_event', dealId=deal_id, status='dispute',
            actionLabel='Добавить свою позицию'
        )
        try:
            await bot.send_message(
                int(other_id),
                f'⚖️ <b>По сделке #{code} открыт спор</b>\n\n'
                'Вторая сторона уже отправила свою позицию администратору.\n'
                'Вы тоже можете открыть сделку и отдельно описать свою сторону ситуации.',
                parse_mode='HTML',
                reply_markup=_deal_open_keyboard(deal_id)
            )
        except Exception:
            log.exception('counterparty dispute notification failed for deal %s', deal_id)

    return web.json_response({'ok':True,'deal':deal})


async def api_deal_admin_join(request: web.Request):
    actor_id = telegram_user_from_init_data(request.headers.get('X-Telegram-Init-Data','') or request.query.get('initData',''))
    if not actor_id or str(actor_id) not in _admin_ids():
        return web.json_response({'error':'forbidden','message':'Только администратор может подключиться к сделке.'}, status=403)
    try: body = await request.json()
    except Exception: return web.json_response({'error':'invalid_json'}, status=400)
    deal_id = str(body.get('dealId') or '').strip()
    deal = db_get_json(f'deelo_deal_{deal_id}', {}) or {}
    if not deal: return web.json_response({'error':'deal_not_found','message':'Сделка не найдена.'}, status=404)

    # Joining is explicit and idempotent for the configured administrator.
    if deal.get('supportAdminJoined'):
        if str(deal.get('supportAdminId') or '') == str(actor_id):
            return web.json_response({'ok':True,'deal':deal,'already':True})
        return web.json_response({'error':'support_already_joined','message':'К этой сделке уже подключён оператор Playerok.'}, status=409)

    now = int(__import__('time').time()*1000)
    deal['supportAdminJoined'] = True
    deal['supportAdminId'] = str(actor_id)
    deal['supportAdminJoinedAt'] = now
    db_set_json(f'deelo_deal_{deal_id}', deal, 1)

    system_text='К сделке подключился оператор Playerok. Оператор видит чат сделки и при необходимости может вмешаться.'
    msgs = db_get_json(f'deelo_dealchat_{deal_id}', []) or []
    msgs.append({'userId':'playerok','type':'system','text':system_text,'time':now})
    db_set_json(f'deelo_dealchat_{deal_id}', msgs[-500:], 1)

    code=deal_code_server(deal_id)
    for uid in {str(deal.get('buyerId') or ''), str(deal.get('sellerId') or '')}:
        if not uid: continue
        append_notification(uid, f'К сделке #{code} подключился оператор Playerok и при необходимости может вмешаться.', 'Поддержка сделки', '🛡️')
        try:
            await bot.send_message(
                int(uid),
                f'🛡️ <b>К сделке #{code} подключился оператор Playerok</b>\n\n'
                'Оператор поддержки видит чат сделки и при необходимости может вмешаться.',
                parse_mode='HTML',
                reply_markup=_deal_open_keyboard(deal_id),
            )
        except Exception:
            log.exception('admin join participant notify failed')
    return web.json_response({'ok':True,'deal':deal,'already':False})


async def api_deal_admin_release_seller(request: web.Request):
    """Resolve an open dispute in the seller's favour and release reserved funds.

    Security / money invariants:
    - Telegram initData must belong to an administrator.
    - The dispute must still be open.
    - Buyer funds must already be reserved by a real paid deal.
    - A completed/released deal can never be paid twice.
    """
    actor_id = telegram_user_from_init_data(request.headers.get('X-Telegram-Init-Data','') or request.query.get('initData',''))
    if not actor_id or str(actor_id) not in _admin_ids():
        return web.json_response({'error':'forbidden','message':'Недоступно.'}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error':'invalid_json'}, status=400)

    deal_id = str(body.get('dealId') or '').strip()
    if not deal_id:
        return web.json_response({'error':'missing_deal_id'}, status=400)

    now = int(__import__('time').time() * 1000)
    with db_connect() as conn:
        # Money operation: serialize competing admin requests before reading the deal.
        # The second request waits, then sees the completed state and cannot pay twice.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute('SELECT value FROM kv WHERE key=?', (f'deelo_deal_{deal_id}',)).fetchone()
        if not row:
            return web.json_response({'error':'deal_not_found','message':'Сделка не найдена.'}, status=404)

        deal = json.loads(row[0])
        status = str(deal.get('status') or '')
        dispute = deal.get('dispute') or {}
        buyer = str(deal.get('buyerId') or '')
        seller = str(deal.get('sellerId') or '')
        code = deal_code_server(deal_id)

        if dispute.get('status') != 'open':
            return web.json_response({'error':'dispute_not_open','message':'По сделке нет открытого спора.'}, status=409)
        if status in {'completed','done','finished','declined','rejected','cancelled','refunded'} or deal.get('adminFundsReleasedAt'):
            return web.json_response({'error':'already_final','message':'Сделка уже завершена. Повторная выплата запрещена.'}, status=409)
        if status not in {'paid','transferred','reserved','delivered'}:
            return web.json_response({'error':'funds_not_reserved','message':'Средства покупателя ещё не находятся в резерве.'}, status=409)

        reserved = Decimal(str(deal.get('reservedRub') or 0)).quantize(Decimal('0.01'))
        payout = Decimal(str(deal.get('amountRub') or deal.get('amount') or 0)).quantize(Decimal('0.01'))
        if reserved <= 0 or payout <= 0:
            return web.json_response({'error':'bad_amount','message':'Не удалось определить зарезервированную сумму.'}, status=409)
        if payout > reserved:
            return web.json_response({'error':'invalid_reserve','message':'Сумма выплаты превышает резерв. Выплата заблокирована.'}, status=409)

        srow = conn.execute('SELECT value FROM kv WHERE key=?', (f'deelo_user_{seller}',)).fetchone()
        if not srow:
            return web.json_response({'error':'seller_not_found','message':'Профиль продавца не найден.'}, status=404)

        seller_rec = json.loads(srow[0])
        seller_balance = Decimal(str(seller_rec.get('balance') or 0)).quantize(Decimal('0.01'))
        seller_rec['balance'] = float(seller_balance + payout)

        deal['sellerPayoutRub'] = float(payout)
        deal['platformFeeRub'] = float(max(Decimal('0'), reserved - payout))
        deal['status'] = 'completed'
        deal['completedAt'] = now
        deal['completedBy'] = str(actor_id)
        deal['adminFundsReleasedAt'] = now
        deal['payoutAt'] = now
        deal['adminFundsReleasedBy'] = str(actor_id)
        deal['adminResolution'] = 'seller'
        deal['dispute'] = {
            **dispute,
            'status': 'resolved',
            'resolution': 'seller',
            'resolvedBy': str(actor_id),
            'resolvedAt': now,
        }

        conn.execute(
            "UPDATE kv SET value=?,updated_at=strftime('%s','now') WHERE key=?",
            (json.dumps(seller_rec, ensure_ascii=False), f'deelo_user_{seller}')
        )

        # A dispute resolved with a completed payout is still a completed deal for both sides.
        for uid in {buyer, seller}:
            urow = conn.execute('SELECT value FROM kv WHERE key=?', (f'deelo_user_{uid}',)).fetchone()
            if not urow:
                continue
            rec = json.loads(urow[0])
            rec['dealsTotal'] = int(rec.get('dealsTotal') or 0) + 1
            rec['dealsSuccess'] = int(rec.get('dealsSuccess') or 0) + 1
            conn.execute(
                "UPDATE kv SET value=?,updated_at=strftime('%s','now') WHERE key=?",
                (json.dumps(rec, ensure_ascii=False), f'deelo_user_{uid}')
            )

        conn.execute(
            "UPDATE kv SET value=?,updated_at=strftime('%s','now') WHERE key=?",
            (json.dumps(deal, ensure_ascii=False), f'deelo_deal_{deal_id}')
        )
        conn.commit()

    # Chat audit trail and user-facing notifications are written only after DB commit.
    msgs = db_get_json(f'deelo_dealchat_{deal_id}', []) or []
    msgs.append({
        'userId':'playerok',
        'type':'system',
        'text':f'Администратор разрешил спор в пользу продавца. {payout:.2f} ₽ из резерва переданы продавцу. Сделка завершена.',
        'time':now,
    })
    db_set_json(f'deelo_dealchat_{deal_id}', msgs[-500:], 1)

    append_payment_history(seller, 'deal_sale', payout, 'Продажа по спору', f'Сделка №{code} · выплата продавцу по решению администратора', 'completed', deal_id)
    append_payment_history(buyer, 'deal_purchase', -reserved, 'Покупка по сделке', f'Сделка №{code} · спор решён администратором в пользу продавца', 'completed', deal_id)

    seller_text = f'✅ <b>Спор по сделке #{code} решён</b>\n\nАдминистратор принял решение в пользу продавца. <b>{payout:.2f} ₽</b> зачислено на ваш баланс.'
    buyer_text = f'⚖️ <b>Спор по сделке #{code} завершён</b>\n\nАдминистратор принял решение в пользу продавца. Зарезервированные средства переданы продавцу.'
    for uid, text in ((seller, seller_text), (buyer, buyer_text)):
        if not uid:
            continue
        append_notification(uid, re.sub('<[^>]+>','',text), 'Спор завершён', '⚖️')
        try:
            await _deal_message(uid, text, deal_id)
        except Exception:
            log.exception('admin seller release notify failed for %s', uid)

    await send_deal_completion_report(deal)
    return web.json_response({'ok':True,'deal':deal,'message':f'Продавцу передано {payout:.2f} ₽. Спор завершён.'})


async def api_admin_user(request: web.Request):
    actor_id=telegram_user_from_init_data(request.headers.get('X-Telegram-Init-Data','') or request.query.get('initData',''))
    if not actor_id or str(actor_id) not in _admin_ids():
        return web.json_response({'error':'forbidden'},status=403)
    user_id=str(request.query.get('userId') or '')
    if not user_id: return web.json_response({'error':'missing_user_id'},status=400)
    user=db_get_json(f'deelo_user_{user_id}',{}) or {}
    if not user: return web.json_response({'error':'user_not_found'},status=404)
    ids=db_get_json(f'deelo_dealindex_{user_id}',[]) or []; deals=[]
    for did in reversed(ids[-50:]):
        d=db_get_json(f'deelo_deal_{did}',{}) or {}
        if d: deals.append({'id':d.get('id'),'code':deal_code_server(d.get('id')),'title':d.get('title'),'amount':d.get('amountRub') or d.get('amount'),'status':d.get('status')})
    return web.json_response({'ok':True,'user':user,'deals':deals},headers={'Cache-Control':'no-store'})


def _admin_auth(request: web.Request):
    # Telegram WebView can be inconsistent with custom headers after reopening.
    # Accept the same signed initData from header or query string; it is always
    # cryptographically validated before admin access is granted.
    init_data = request.headers.get('X-Telegram-Init-Data','') or request.query.get('initData','')
    actor_id = telegram_user_from_init_data(str(init_data))
    return str(actor_id) if actor_id and str(actor_id) in _admin_ids() else None

def _kv_rows(prefix: str, limit: int = 500):
    with db_connect() as conn:
        rows=conn.execute("SELECT key,value,updated_at FROM kv WHERE key LIKE ? ORDER BY updated_at DESC LIMIT ?",(prefix+'%',limit)).fetchall()
    out=[]
    for key,value,updated in rows:
        try: obj=json.loads(value)
        except Exception: continue
        out.append((key,obj,updated))
    return out


async def api_deal_admin_refund_buyer(request: web.Request):
    """Resolve an open dispute in the buyer's favour and return the reserved amount."""
    actor_id = telegram_user_from_init_data(
        request.headers.get('X-Telegram-Init-Data','') or request.query.get('initData','')
    )
    if not actor_id or str(actor_id) not in _admin_ids():
        return web.json_response({'error':'forbidden','message':'Недоступно.'}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error':'invalid_json'}, status=400)

    deal_id = str(body.get('dealId') or '').strip()
    if not deal_id:
        return web.json_response({'error':'missing_deal_id'}, status=400)

    now = int(time.time() * 1000)
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute('SELECT value FROM kv WHERE key=?', (f'deelo_deal_{deal_id}',)).fetchone()
        if not row:
            conn.rollback()
            return web.json_response({'error':'deal_not_found','message':'Сделка не найдена.'}, status=404)

        deal = json.loads(row[0])
        dispute = deal.get('dispute') or {}
        status = str(deal.get('status') or '')
        if dispute.get('status') != 'open':
            conn.rollback()
            return web.json_response({'error':'no_open_dispute','message':'Открытого спора уже нет.'}, status=409)
        if status in {'completed','done','finished','cancelled','refunded'} or deal.get('adminRefundedAt'):
            conn.rollback()
            return web.json_response({'error':'already_final','message':'Сделка уже завершена.'}, status=409)

        buyer = str(deal.get('buyerId') or '')
        seller = str(deal.get('sellerId') or '')
        reserved = Decimal(str(deal.get('reservedRub') or 0)).quantize(Decimal('0.01'))
        if reserved <= 0:
            conn.rollback()
            return web.json_response({'error':'nothing_reserved','message':'В резерве нет средств для возврата.'}, status=409)

        brow = conn.execute('SELECT value FROM kv WHERE key=?', (f'deelo_user_{buyer}',)).fetchone()
        if not brow:
            conn.rollback()
            return web.json_response({'error':'buyer_not_found','message':'Покупатель не найден.'}, status=404)
        buyer_rec = json.loads(brow[0])
        buyer_rec['balance'] = float(Decimal(str(buyer_rec.get('balance') or 0)) + reserved)

        deal['status'] = 'refunded'
        deal['refundedAt'] = now
        deal['completedAt'] = now
        deal['completedBy'] = str(actor_id)
        deal['adminRefundedAt'] = now
        deal['adminRefundedBy'] = str(actor_id)
        deal['adminResolution'] = 'buyer'
        deal['dispute'] = {
            **dispute,
            'status':'resolved',
            'resolution':'buyer',
            'resolvedBy':str(actor_id),
            'resolvedAt':now,
        }

        conn.execute(
            "UPDATE kv SET value=?,updated_at=strftime('%s','now') WHERE key=?",
            (json.dumps(buyer_rec, ensure_ascii=False), f'deelo_user_{buyer}')
        )
        conn.execute(
            "UPDATE kv SET value=?,updated_at=strftime('%s','now') WHERE key=?",
            (json.dumps(deal, ensure_ascii=False), f'deelo_deal_{deal_id}')
        )
        conn.commit()

    code = deal_code_server(deal_id)
    msgs = db_get_json(f'deelo_dealchat_{deal_id}', []) or []
    msgs.append({
        'userId':'playerok','type':'system',
        'text':f'Администратор решил спор в пользу покупателя. {reserved:.2f} ₽ возвращено покупателю. Сделка закрыта.',
        'time':now,
    })
    db_set_json(f'deelo_dealchat_{deal_id}', msgs[-500:], 1)

    append_payment_history(
        buyer, 'deal_refund', reserved, 'Возврат по спору',
        f'Сделка №{code} · возврат по решению администратора', 'completed', deal_id
    )

    for uid, title, text in (
        (buyer, 'Возврат по спору', f'↩️ <b>Спор по сделке #{code} решён в вашу пользу</b>\n\n💰 <b>Возвращено:</b> {reserved:.2f} ₽\n🕒 <b>Время:</b> {_msk_time_label(now)}'),
        (seller, 'Спор завершён', f'⚖️ <b>Спор по сделке #{code} завершён</b>\n\nАдминистратор принял решение в пользу покупателя. Средства из резерва возвращены покупателю.\n🕒 <b>Время:</b> {_msk_time_label(now)}'),
    ):
        if not uid:
            continue
        append_notification(uid, re.sub('<[^>]+>', '', text), title, '⚖️', kind='deal_event', dealId=deal_id, status='refunded', actionLabel='Открыть сделку')
        await _deal_message(uid, text, deal_id)

    return web.json_response({'ok':True,'deal':deal,'refunded':float(reserved)})


async def api_admin_dashboard(request: web.Request):
    if not _admin_auth(request): return web.json_response({'error':'forbidden'},status=403)
    users=[x[1] for x in _kv_rows('deelo_user_',5000) if isinstance(x[1],dict)]
    # Legacy deals may have the id only in the SQLite KV key. Normalize them so
    # the admin dashboard can always open the real deal instead of passing an empty id.
    deals=[]
    for key,obj,_updated in _kv_rows('deelo_deal_',1000):
        if not isinstance(obj,dict):
            continue
        d=dict(obj)
        if not d.get('id'):
            d['id']=str(key)[len('deelo_deal_'):]
        deals.append(d)
    withdrawals=[x[1] for x in _kv_rows('deelo_withdraw_',500) if isinstance(x[1],dict)]
    completed=[d for d in deals if d.get('status') in ('done','completed')]
    paid=[d for d in deals if d.get('status') in ('paid','transferred','done','completed') or d.get('paidAt')]
    disputes=[d for d in deals if (d.get('dispute') or {}).get('status')=='open']
    active=[d for d in deals if d.get('status') not in ('done','completed','cancelled','declined','rejected','refunded','finished')]
    pending_withdrawals=[w for w in withdrawals if str(w.get('status') or '').lower() in ('pending','created','new','waiting')]
    turnover=Decimal('0')
    for d in completed:
        try:
            turnover += Decimal(str(d.get('amountRub') or d.get('amount') or 0))
        except Exception:
            pass
    deals.sort(key=lambda d: float(d.get('createdAt') or d.get('acceptedAt') or 0), reverse=True)
    def deal_item(d):
        sid=str(d.get('sellerId') or ''); bid=str(d.get('buyerId') or '')
        sr=db_get_json(f'deelo_user_{sid}', {}) or {}; br=db_get_json(f'deelo_user_{bid}', {}) or {}
        seller_tag=str(sr.get('telegramUsername') or d.get('seller') or sid).lstrip('@')
        buyer_tag=str(br.get('telegramUsername') or d.get('buyer') or bid).lstrip('@')
        return {
            'id':d.get('id'),'code':deal_code_server(d.get('id')),
            'title':d.get('title') or d.get('desc') or 'Без темы',
            'amount':d.get('amountRub') or d.get('amount') or 0,
            'status':d.get('status') or 'created',
            'seller':seller_tag,'buyer':buyer_tag,'sellerId':sid,'buyerId':bid,
            'createdAt':d.get('createdAt'),'acceptedAt':d.get('acceptedAt'),
            'paidAt':d.get('paidAt'),'transferredAt':d.get('transferredAt'),
            'receivedAt':d.get('receivedAt'),'payoutAt':d.get('payoutAt'),
            'completedAt':d.get('completedAt'),'refundedAt':d.get('refundedAt'),
            'supportAdminJoined':bool(d.get('supportAdminJoined')),
            'dispute':d.get('dispute') if isinstance(d.get('dispute'),dict) else None,
            'disputeSubmissions':d.get('disputeSubmissions') if isinstance(d.get('disputeSubmissions'),dict) else {},
        }
    def wd_item(w): return {'id':w.get('id'),'user_id':w.get('user_id'),'amount':w.get('amount'),'method':w.get('method'),'status':w.get('status'),'created_at':w.get('created_at') or w.get('createdAt')}
    return web.json_response({'ok':True,'stats':{'users':len(users),'deals':len(deals),'active':len(active),'completed':len(completed),'paid':len(paid),'disputes':len(disputes),'withdrawals':len(withdrawals),'pendingWithdrawals':len(pending_withdrawals),'banned':sum(1 for u in users if u.get('banned')),'turnover':float(turnover)},'recent': [deal_item(d) for d in deals[:100]],'completed':[deal_item(d) for d in completed[:100]],'paid':[deal_item(d) for d in paid[:100]],'disputes':[deal_item(d) | {'dispute':d.get('dispute')} for d in disputes[:100]],'withdrawals':[wd_item(w) for w in withdrawals[:100]]},headers={'Cache-Control':'no-store'})

async def api_admin_find_user(request: web.Request):
    if not _admin_auth(request): return web.json_response({'error':'forbidden'},status=403)
    q=str(request.query.get('q') or '').strip().lstrip('@')
    if not q: return web.json_response({'error':'missing_query'},status=400)
    rec=db_get_json(f'deelo_user_{q}',{}) if q.isdigit() else find_user_by_username(q)
    if not rec: return web.json_response({'error':'user_not_found'},status=404)
    uid=str(rec.get('id') or q)
    ids=db_get_json(f'deelo_dealindex_{uid}',[]) or []
    ds=[]
    for did in reversed(ids[-30:]):
        d=db_get_json(f'deelo_deal_{did}',{}) or {}
        if d: ds.append({'id':d.get('id') or str(did),'code':deal_code_server(d.get('id') or str(did)),'title':d.get('title'),'amount':d.get('amountRub') or d.get('amount'),'status':d.get('status')})
    return web.json_response({'ok':True,'user':rec,'deals':ds},headers={'Cache-Control':'no-store'})

async def api_admin_user_action(request: web.Request):
    admin = _admin_auth(request)
    if not admin:
        return web.json_response({'error': 'forbidden'}, status=403)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid_json'}, status=400)

    uid = str(body.get('userId') or '')
    action = str(body.get('action') or '')
    rec = db_get_json(f'deelo_user_{uid}', {}) or {}
    if not rec:
        return web.json_response({'error': 'user_not_found'}, status=404)

    if uid in _admin_ids() and action in {'ban', 'unverify'}:
        return web.json_response({'error': 'admin_protected'}, status=400)

    if action == 'ban':
        rec['banned'] = True
        rec['bannedAt'] = int(time.time() * 1000)
        rec['bannedBy'] = admin

    elif action == 'unban':
        rec['banned'] = False

    elif action == 'verify':
        rec['verified'] = True
        rec['level'] = max(1, int(rec.get('level') or 0))
        rec['verificationReason'] = 'admin'

    elif action == 'unverify':
        rec['verified'] = False
        rec['level'] = 0
        rec.pop('verificationReason', None)
        rec.pop('verificationReasonLevel2', None)

    elif action == 'set_level':
        try:
            level = int(body.get('level'))
        except Exception:
            return web.json_response({'error': 'bad_level'}, status=400)

        if level not in {0, 1, 2, 3}:
            return web.json_response({'error': 'bad_level'}, status=400)

        rec['level'] = level
        rec['verified'] = level > 0
        if level > 0:
            rec['verificationReason'] = 'admin'
        else:
            rec.pop('verificationReason', None)
            rec.pop('verificationReasonLevel2', None)

    elif action in {'add_balance', 'subtract_balance', 'set_balance'}:
        try:
            amount = Decimal(str(body.get('amount') or 0))
        except Exception:
            return web.json_response({'error': 'bad_amount'}, status=400)

        if abs(amount) > Decimal('10000000'):
            return web.json_response({'error': 'bad_amount'}, status=400)

        current = Decimal(str(rec.get('balance') or 0))

        if action == 'add_balance':
            # Signed field: +10000 adds, -10000 subtracts.
            if amount == 0:
                return web.json_response({'error': 'bad_amount'}, status=400)
            rec['balance'] = float(max(Decimal('0'), current + amount))

        elif action == 'subtract_balance':
            if amount <= 0:
                return web.json_response({'error': 'bad_amount'}, status=400)
            rec['balance'] = float(max(Decimal('0'), current - amount))

        elif action == 'set_balance':
            if amount < 0:
                return web.json_response({'error': 'bad_amount'}, status=400)
            rec['balance'] = float(amount)

        # Administrative balance correction stays silent:
        # no payment-history entry and no user notification.

    else:
        return web.json_response({'error': 'bad_action'}, status=400)

    db_set_json(f'deelo_user_{uid}', rec, 1)
    return web.json_response({'ok': True, 'user': rec})




async def api_admin_message_user(request: web.Request):
    """Admin-only direct message from Playerok support to one user."""
    admin = _admin_auth(request)
    if not admin:
        return web.json_response({'error':'forbidden'}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error':'invalid_json'}, status=400)

    uid = str(body.get('userId') or '').strip()
    text = _normalize_human_text(body.get('text'))
    if not uid.isdigit():
        return web.json_response({'error':'invalid_user','message':'Некорректный Telegram ID.'}, status=400)
    if not text or len(text) > 2000:
        return web.json_response({'error':'bad_text','message':'Введите сообщение до 2000 символов.'}, status=400)
    if not db_get_json(f'deelo_user_{uid}', None):
        return web.json_response({'error':'user_not_found','message':'Пользователь не найден.'}, status=404)

    # Keep the same support history the user sees inside the Mini App.
    msg, _ = _support_append_visible(
        uid,
        'admin',
        text,
        message_id='admin_direct_' + uuid.uuid4().hex,
    )
    append_notification(uid, text, 'Сообщение от поддержки', '🛡️', kind='support_message', actionLabel='Открыть поддержку')

    try:
        await bot.send_message(
            int(uid),
            '🛡️ <b>Сообщение от поддержки Playerok</b>\n\n' + escape_html_server(text),
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text='🚀 Открыть Playerok',
                    web_app=WebAppInfo(url=f'{WEBAPP_URL}?screen=support')
                )
            ]]),
        )
    except Exception:
        log.exception('admin direct support message failed admin=%s user=%s', admin, uid)
        return web.json_response({
            'error':'telegram_send_failed',
            'message':'Сообщение сохранено в поддержке, но Telegram не смог доставить его пользователю.'
        }, status=502)

    return web.json_response({'ok':True,'message':msg})


async def api_admin_stars_balance(request: web.Request):
    admin = _admin_auth(request)
    if not admin:
        return web.json_response({'error': 'forbidden'}, status=403)
    try:
        bal = await _bot_star_balance()
        return web.json_response({'ok': True, 'balance': bal})
    except Exception as exc:
        log.exception('admin stars balance failed')
        return web.json_response({'error': 'telegram_api_error', 'message': str(exc)}, status=502)


async def api_admin_gift_premium(request: web.Request):
    admin = _admin_auth(request)
    if not admin:
        return web.json_response({'error': 'forbidden'}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'invalid_json'}, status=400)

    raw_target = str(body.get('userId') or body.get('username') or '').strip()
    try:
        months = int(body.get('months') or 0)
    except Exception:
        months = 0
    if months not in PREMIUM_STAR_PRICES:
        return web.json_response({'error': 'bad_months', 'message': 'Доступно только 3, 6 или 12 месяцев.'}, status=400)

    rec = _resolve_admin_target(raw_target)
    if not rec:
        return web.json_response({'error': 'user_not_found', 'message': 'Пользователь не найден.'}, status=404)
    uid = str(rec.get('id') or '')

    try:
        result = await _gift_premium_to_user(uid, months, str(admin), 'admin_panel')
        return web.json_response({'ok': True, 'result': result})
    except ValueError as exc:
        return web.json_response({'error': 'bad_request', 'message': str(exc)}, status=400)
    except Exception as exc:
        log.exception('admin premium gift failed admin=%s user=%s months=%s', admin, uid, months)
        return web.json_response({'error': 'telegram_api_error', 'message': str(exc)}, status=502)


async def api_admin_unverify_all(request: web.Request):
    admin=_admin_auth(request)
    if not admin: return web.json_response({'error':'forbidden'},status=403)
    body=await request.json(); confirm=str(body.get('confirm') or '')
    if confirm!='REMOVE': return web.json_response({'error':'confirmation_required'},status=400)
    changed=0
    for key,rec,_ in _kv_rows('deelo_user_',10000):
        uid=str(rec.get('id') or key.replace('deelo_user_',''))
        if uid in _admin_ids(): continue
        if rec.get('verified') or int(rec.get('level') or 0)>0:
            rec['verified']=False; rec['level']=0; rec.pop('verificationReason',None); db_set_json(key,rec,1); changed+=1
    return web.json_response({'ok':True,'changed':changed})

async def api_search_deals(request: web.Request):
    """Secure deal search for Mini App.

    Regular users can search only deals from their own deal index.
    Admins may enter another user's @username / Telegram ID and inspect that user's deals.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    actor_id = telegram_user_from_init_data(init_data)
    if not actor_id:
        return web.json_response({"error": "invalid_telegram_init_data"}, status=401)

    q = str(request.query.get("q") or "").strip().lower()
    q_clean = q.lstrip("@")
    target_id = str(actor_id)
    admin_mode = str(actor_id) in _admin_ids()

    # Only admins are allowed to switch the searched account.
    if admin_mode and q_clean:
        target = None
        if q_clean.isdigit():
            target = db_get_json(f"deelo_user_{q_clean}")
        if not target:
            target = find_user_by_username(q_clean)
        if target and target.get("id") is not None:
            target_id = str(target.get("id"))

    ids = db_get_json(f"deelo_dealindex_{target_id}", []) or []
    result = []
    for deal_id in reversed(ids[-300:]):
        deal = db_get_json(f"deelo_deal_{deal_id}")
        if not deal:
            continue

        seller_id = str(deal.get("sellerId") or "")
        buyer_id = str(deal.get("buyerId") or "")
        seller = db_get_json(f"deelo_user_{seller_id}", {}) or {}
        buyer = db_get_json(f"deelo_user_{buyer_id}", {}) or {}
        seller_tag = str(seller.get("telegramUsername") or deal.get("seller") or seller_id).lstrip("@")
        buyer_tag = str(buyer.get("telegramUsername") or deal.get("buyer") or buyer_id).lstrip("@")

        item = dict(deal)
        item["sellerUsername"] = seller_tag
        item["buyerUsername"] = buyer_tag
        item["code"] = deal_code_server(deal.get("id"))

        # For an admin who resolved an exact account, return that account's deals.
        # For a regular user, q is only a filter over their OWN indexed deals.
        if q and not (admin_mode and target_id != str(actor_id)):
            haystack = " ".join([
                str(item.get("code") or ""), str(item.get("id") or ""),
                str(item.get("title") or ""), str(item.get("desc") or ""),
                seller_tag, buyer_tag, "@" + seller_tag, "@" + buyer_tag,
            ]).lower()
            if q not in haystack and q_clean not in haystack:
                continue
        result.append(item)

    return web.json_response({
        "ok": True,
        "items": result,
        "admin": admin_mode,
        "targetUserId": target_id if admin_mode else str(actor_id),
    })

# ============================================================
# 21. API STORE: ЧТЕНИЕ / ЗАПИСЬ / УДАЛЕНИЕ ДАННЫХ
# ============================================================
async def store_get(request: web.Request):
    key = request.match_info["key"]
    if key.startswith("deelo_deal_") and not key.startswith("deelo_dealindex_") and not key.startswith("deelo_dealchat_"):
        deal_id = key[len("deelo_deal_"):]
        current = db_get_json(key, None)
        if current and _cleanup_expired_sale_information(deal_id, current):
            return web.json_response({"error": "not_found"}, status=404)
    with db_connect() as conn:
        row = conn.execute("SELECT value, shared FROM kv WHERE key=?", (key,)).fetchone()
    if not row:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response({"key": key, "value": row[0], "shared": bool(row[1])})

async def store_set(request: web.Request):
    key = request.match_info["key"]
    body = await request.json()
    value = body.get("value")
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    shared = 1 if body.get("shared") else 0
    with db_connect() as conn:
        conn.execute("INSERT INTO kv(key,value,shared,updated_at) VALUES(?,?,?,strftime('%s','now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value, shared=excluded.shared, updated_at=excluded.updated_at", (key, value, shared))
        conn.commit()
    return web.json_response({"key": key, "value": value, "shared": bool(shared)})

async def store_delete(request: web.Request):
    key = request.match_info["key"]
    with db_connect() as conn:
        conn.execute("DELETE FROM kv WHERE key=?", (key,))
        conn.commit()
    return web.json_response({"key": key, "deleted": True})

async def store_list(request: web.Request):
    prefix = request.query.get("prefix", "")
    with db_connect() as conn:
        rows = conn.execute("SELECT key FROM kv WHERE key LIKE ? ORDER BY key", (prefix + "%",)).fetchall()
    return web.json_response({"keys": [r[0] for r in rows], "prefix": prefix})

# ============================================================
# 22.1. СТИКЕРЫ MINI APP
# ============================================================
async def sticker_handler(request: web.Request):
    name = request.match_info.get("name", "")
    if not name or ".." in name or "\\" in name:
        return web.Response(status=400, text="invalid sticker")
    base = Path(__file__).resolve().parent / "stickers"
    path = (base / name).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError:
        return web.Response(status=403, text="forbidden")
    if not path.is_file():
        return web.Response(status=404, text="sticker not found")
    resp = web.FileResponse(path)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


# ============================================================
# 22. ОТДАЧА MINI APP: web/index.html
# ============================================================
async def index_handler(request: web.Request):
    global _index_cache

    # Some hosting panels launch main.py from a different working directory.
    # Search the common layouts instead of assuming cwd.
    candidates = [
        INDEX_FILE,
        Path.cwd() / "web" / "index.html",
        Path.cwd() / "index.html",
        Path(__file__).resolve().parent / "index.html",
    ]

    index_path = next((p for p in candidates if p.is_file()), None)

    if index_path is None:
        log.error(
            "Mini App index.html not found. __file__=%s cwd=%s",
            __file__,
            Path.cwd(),
        )
        return web.Response(
            status=500,
            text="Mini App files are missing on the server. Upload the web/ folder with index.html.",
        )

    try:
        mtime = index_path.stat().st_mtime_ns
    except OSError:
        mtime = 0

    if not isinstance(_index_cache, tuple) or _index_cache[0] != mtime:
        _index_cache = (
            mtime,
            index_path.read_text(encoding="utf-8"),
        )

    return web.Response(
        text=_index_cache[1],
        content_type="text/html",
        headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0","Pragma":"no-cache","Expires":"0"},
    )


# ============================================================
# 23. ВЕРИФИКАЦИЯ TOME: verification.txt
# ============================================================
async def verification_handler(request: web.Request):
    return web.Response(
        text="b0ffe7ed5c8e892dbde8c1f6b4f639b0d11c1bc7",
        content_type="text/plain",
    )


# ============================================================
# 24. ЮMONEY: ПОДПИСЬ HTTP-УВЕДОМЛЕНИЙ
# ============================================================
def _yoomoney_sign_string(params: dict) -> str:
    """Build the RFC 3986 encoded, alphabetically sorted string used by sign."""
    items = []
    for key in sorted(params):
        if key == "sign":
            continue
        value = params.get(key, "")
        if value is None:
            value = ""
        items.append(f"{quote(str(key), safe='-._~')}={quote(str(value), safe='-._~')}")
    return "&".join(items)


def _yoomoney_expected_sign(params: dict) -> str:
    raw = _yoomoney_sign_string(params).encode("utf-8")
    return hmac.new(YOOMONEY_HTTP_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def _yoomoney_payment_label(user_id: str) -> str:
    # Формируем уникальный label для сопоставления платежа с пользователем.
    # Ограничение ЮMoney для label — 64 символа, поэтому дополнительно обрезаем ID.
    safe_user_id = re.sub(r"[^0-9A-Za-z_-]", "", str(user_id))[-20:]
    return f"DEEL0-{safe_user_id}-{uuid.uuid4().hex}"


def _yoomoney_pending_key(label: str) -> str:
    return f"deelo_yoomoney_pending_{label}"


def _yoomoney_paid_key(operation_id: str) -> str:
    return f"deelo_yoomoney_paid_{operation_id}"


# ============================================================
# 25. ЮMONEY: ЗАЧИСЛЕНИЕ ПОДТВЕРЖДЁННОГО ПЛАТЕЖА
# ============================================================
def _credit_yoomoney_payment(label: str, operation_id: str, credited_amount: Decimal, withdraw_amount: Decimal):
    pending = db_get_json(_yoomoney_pending_key(label))
    if not pending:
        return False, "payment_not_found"

    user_id = str(pending.get("user_id") or "")
    if not user_id:
        return False, "missing_user"

    with db_connect() as conn:
        marker_key = _yoomoney_paid_key(operation_id)
        inserted = conn.execute(
            "INSERT OR IGNORE INTO kv(key,value,shared,updated_at) VALUES(?,?,1,strftime('%s','now'))",
            (marker_key, json.dumps({
                "label": label,
                "user_id": user_id,
                "credited_amount": f"{credited_amount:.2f}",
                "withdraw_amount": f"{withdraw_amount:.2f}",
            }, ensure_ascii=False)),
        ).rowcount
        if inserted == 0:
            return True, "already_processed"

        row = conn.execute(
            "SELECT value FROM kv WHERE key=?",
            (f"deelo_user_{user_id}",),
        ).fetchone()
        if not row:
            conn.execute("DELETE FROM kv WHERE key=?", (marker_key,))
            return False, "user_not_found"

        rec = json.loads(row[0])

        # Внутренний баланс получает полную сумму, которую оплатил пользователь.
        # amount/credited_amount — сумма после комиссии ЮMoney.
        # withdraw_amount — сумма списания у плательщика.
        user_credit = Decimal(str(withdraw_amount)).quantize(Decimal("0.01"))

        rec["balance"] = float(Decimal(str(rec.get("balance") or 0)) + user_credit)
        rec["topUpTotal"] = float(Decimal(str(rec.get("topUpTotal") or 0)) + user_credit)
        conn.execute(
            "UPDATE kv SET value=?, updated_at=strftime('%s','now') WHERE key=?",
            (json.dumps(rec, ensure_ascii=False), f"deelo_user_{user_id}"),
        )
        conn.execute(
            "UPDATE kv SET value=?, updated_at=strftime('%s','now') WHERE key=?",
            (json.dumps({**pending, "status": "paid", "operation_id": operation_id,
                         "credited_amount": f"{credited_amount:.2f}",
                         "withdraw_amount": f"{withdraw_amount:.2f}"}, ensure_ascii=False),
             _yoomoney_pending_key(label)),
        )
        conn.commit()

    user_credit = Decimal(str(withdraw_amount)).quantize(Decimal("0.01"))
    append_notification(
        user_id,
        f"Баланс пополнен на {user_credit:.2f} ₽ через ЮMoney.",
        "Пополнение баланса",
        "💳",
    )
    append_payment_history(
        user_id,
        "topup",
        user_credit,
        "Пополнение баланса",
        f"Баланс пополнен на {user_credit:.2f} ₽ через ЮMoney.",
        "completed",
        _yoomoney_paid_key(operation_id),
    )
    return True, "credited"


# ============================================================
# 26. ЮMONEY: СТРАНИЦА ПЕРЕХОДА К ОПЛАТЕ
# ============================================================
async def yoomoney_pay_page(request: web.Request):
    """Показывает страницу подтверждения перед POST-переходом на ЮMoney."""
    if not YOOMONEY_WALLET:
        return web.Response(status=503, text="ЮMoney is not configured")

    token = str(request.query.get("token") or "").strip()
    if not token or not re.fullmatch(r"[0-9a-f]{32}", token):
        return web.Response(status=400, text="invalid payment token")

    pending = db_get_json(f"deelo_yoomoney_token_{token}")
    if not pending:
        return web.Response(status=404, text="payment not found")

    label = str(pending.get("label") or "")
    amount = str(pending.get("amount") or "")
    if not label or not amount:
        return web.Response(status=400, text="invalid payment data")

    # ЮMoney ожидает именно POST-форму на /quickpay/confirm.
    # Автоматический JavaScript-submit убран: Telegram WebView иногда
    # блокирует/ломает такой кросс-доменный переход.
    # Пользователь сам нажимает кнопку, после чего браузер отправляет форму.
    form_fields = {
        "receiver": YOOMONEY_WALLET,
        "quickpay-form": "button",
        "paymentType": "AC",
        "sum": amount,
        "label": label,
        "successURL": f"{WEBAPP_URL}#wallet",
    }

    hidden = "\n".join(
        f'<input type="hidden" name="{escape_html_server(k)}" value="{escape_html_server(v)}">'
        for k, v in form_fields.items()
    )

    html = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Оплата через ЮMoney</title>
<style>
    body {{
        margin: 0;
        padding: 32px 20px;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: #f5f5f7;
        color: #111;
        text-align: center;
    }}
    .card {{
        max-width: 420px;
        margin: 40px auto;
        padding: 28px 22px;
        background: #fff;
        border-radius: 20px;
        box-shadow: 0 8px 30px rgba(0,0,0,.08);
    }}
    h1 {{
        margin: 0 0 12px;
        font-size: 24px;
    }}
    .amount {{
        margin: 18px 0 24px;
        font-size: 30px;
        font-weight: 700;
    }}
    .note {{
        margin-bottom: 24px;
        color: #666;
        line-height: 1.45;
        font-size: 14px;
    }}
    button {{
        width: 100%;
        border: 0;
        border-radius: 14px;
        padding: 15px 18px;
        background: #111;
        color: #fff;
        font-size: 17px;
        font-weight: 600;
        cursor: pointer;
    }}
</style>
</head>
<body>
<div class="card">
    <h1>💳 Оплата через ЮMoney</h1>
    <div class="amount">{escape_html_server(amount)} ₽</div>
    <div class="note">
        Нажмите кнопку ниже, чтобы перейти на защищённую страницу ЮMoney
        и завершить оплату банковской картой.
    </div>
    <form method="POST" action="{YOOMONEY_QUICKPAY_URL}">
        {hidden}
        <button type="submit">Перейти к оплате ЮMoney</button>
    </form>
</div>
</body>
</html>"""

    return web.Response(text=html, content_type="text/html")


# ============================================================
# 27. ЮMONEY: СОЗДАНИЕ ПЛАТЕЖА И TOKEN
# ============================================================
async def api_yoomoney_create_payment(request: web.Request):
    """Create a pending ЮMoney top-up and return a hosted payment URL."""
    if not YOOMONEY_WALLET or not YOOMONEY_HTTP_SECRET:
        return web.json_response({"error": "yoomoney_not_configured"}, status=503)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    init_data = str(body.get("initData") or "")
    user_id = telegram_user_from_init_data(init_data)
    if not user_id:
        return web.json_response({"error": "invalid_telegram_init_data"}, status=401)

    try:
        amount = Decimal(str(body.get("amount"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return web.json_response({"error": "invalid_amount"}, status=400)

    if amount < YOOMONEY_MIN_AMOUNT:
        return web.json_response({"error": "amount_too_small", "min": f"{YOOMONEY_MIN_AMOUNT:.2f}"}, status=400)
    if amount > YOOMONEY_MAX_AMOUNT:
        return web.json_response({"error": "amount_too_large", "max": f"{YOOMONEY_MAX_AMOUNT:.2f}"}, status=400)

    user_rec = db_get_json(f"deelo_user_{user_id}")
    if not user_rec:
        return web.json_response({"error": "user_not_found"}, status=404)

    label = _yoomoney_payment_label(user_id)
    token = uuid.uuid4().hex
    pending = {
        "user_id": str(user_id),
        "label": label,
        "amount": f"{amount:.2f}",
        "status": "pending",
        "created_at": __import__("time").time(),
    }
    db_set_json(_yoomoney_pending_key(label), pending, 1)
    db_set_json(f"deelo_yoomoney_token_{token}", pending, 1)

    confirmation_url = f"{WEBAPP_URL.rstrip('/')}{YOOMONEY_PAY_PATH}?token={token}"
    log.info("Created ЮMoney payment: user=%s amount=%s label=%s", user_id, amount, label)
    await _notify_admins_topup_started(str(user_id), amount, "Банковская карта РФ", label)
    return web.json_response({
        "ok": True,
        "status": "pending",
        "paymentId": label,
        "confirmationUrl": confirmation_url,
    })


# ============================================================
# 28. ЮMONEY: WEBHOOK И АВТОМАТИЧЕСКОЕ ЗАЧИСЛЕНИЕ
# ============================================================
async def _notify_admins_yoomoney_issue(title: str, details: str = ""):
    text = (
        "⚠️ <b>ЮMoney · проблема с подтверждением платежа</b>\n"
        f"<b>{escape_html_server(title)}</b>"
        + (f"\n\n{escape_html_server(details)}" if details else "")
        + "\n\nПроверь HTTP-уведомления ЮMoney и YOOMONEY_HTTP_SECRET."
    )
    for aid in _admin_ids():
        try:
            await bot.send_message(int(aid), text, parse_mode="HTML")
        except Exception:
            log.exception("Не удалось отправить ошибку ЮMoney админу %s", aid)


async def yoomoney_webhook_health(request: web.Request):
    return web.json_response({
        "ok": True,
        "provider": "yoomoney",
        "webhook": YOOMONEY_WEBHOOK_PATH,
        "configured": bool(YOOMONEY_WALLET and YOOMONEY_HTTP_SECRET),
    })


async def yoomoney_webhook(request: web.Request):
    """Receive and verify ЮMoney HTTP notifications, then credit the wallet."""
    if not YOOMONEY_HTTP_SECRET:
        await _notify_admins_yoomoney_issue("Не задан YOOMONEY_HTTP_SECRET")
        return web.Response(status=503, text="ЮMoney is not configured")

    try:
        form = await request.post()
        params = {str(k): str(v) for k, v in form.items()}
    except Exception:
        log.exception("Failed to parse ЮMoney webhook")
        return web.Response(status=400, text="invalid form")

    received_sign = params.get("sign", "")
    if not received_sign:
        await _notify_admins_yoomoney_issue(
            "Webhook пришёл без подписи",
            f"operation_id={params.get('operation_id','—')} label={params.get('label','—')}",
        )
        return web.Response(status=403, text="missing sign")

    expected_sign = _yoomoney_expected_sign(params)
    if not hmac.compare_digest(received_sign, expected_sign):
        log.warning("Rejected ЮMoney webhook: invalid signature")
        await _notify_admins_yoomoney_issue(
            "Неверная подпись webhook",
            f"operation_id={params.get('operation_id','—')} label={params.get('label','—')}. "
            "YOOMONEY_HTTP_SECRET в BotHost должен совпадать с секретом HTTP-уведомлений ЮMoney.",
        )
        return web.Response(status=403, text="invalid signature")

    if str(params.get("test_notification") or "").lower() == "true":
        log.info("Accepted ЮMoney test notification")
        return web.Response(status=200, text="ok")

    notification_type = str(params.get("notification_type") or "")
    if notification_type not in {"p2p-incoming", "card-incoming"}:
        log.warning("Ignored ЮMoney notification type: %s", notification_type)
        return web.Response(status=200, text="ok")

    if params.get("unaccepted", "false").lower() == "true":
        log.warning("Ignored unaccepted ЮMoney payment")
        return web.Response(status=200, text="ok")

    label = str(params.get("label") or "").strip()
    operation_id = str(params.get("operation_id") or "").strip()
    if not label or not operation_id:
        return web.Response(status=400, text="missing payment identifiers")

    try:
        credited_amount = Decimal(str(params.get("amount") or "0")).quantize(Decimal("0.01"))
        withdraw_amount = Decimal(str(params.get("withdraw_amount") or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return web.Response(status=400, text="invalid amount")

    if credited_amount <= 0 or withdraw_amount <= 0:
        return web.Response(status=400, text="invalid amount")

    pending = db_get_json(_yoomoney_pending_key(label))
    if not pending:
        log.warning("ЮMoney payment label not found: %s", label)
        await _notify_admins_yoomoney_issue(
            "Не найден ожидающий платёж",
            f"operation_id={operation_id} label={label} withdraw_amount={withdraw_amount:.2f}",
        )
        return web.Response(status=200, text="ok")

    expected_amount = Decimal(str(pending.get("amount") or "0")).quantize(Decimal("0.01"))
    if withdraw_amount != expected_amount:
        log.warning(
            "ЮMoney amount mismatch: label=%s expected=%s withdraw=%s credited=%s",
            label, expected_amount, withdraw_amount, credited_amount,
        )
        await _notify_admins_yoomoney_issue(
            "Сумма платежа не совпала",
            f"operation_id={operation_id} label={label} expected={expected_amount:.2f} "
            f"paid={withdraw_amount:.2f} wallet_received={credited_amount:.2f}",
        )
        return web.Response(status=400, text="amount mismatch")

    ok, reason = _credit_yoomoney_payment(label, operation_id, credited_amount, withdraw_amount)
    if not ok:
        log.error("ЮMoney payment %s was not credited: %s", operation_id, reason)
        return web.Response(status=500, text=reason)

    log.info(
        "ЮMoney payment %s processed: %s label=%s credited=%s withdraw=%s",
        operation_id, reason, label, credited_amount, withdraw_amount,
    )
    if reason == "credited" and withdraw_amount >= Decimal("1000.00"):
        _grant_level1_verification(
            str(pending.get("user_id")),
            f"Банковская карта РФ: {withdraw_amount:.2f} ₽",
        )

    await _notify_admins_topup_paid(
        str(pending.get("user_id")),
        withdraw_amount,
        withdraw_amount,
        "Банковская карта РФ",
        operation_id,
        f"🏦 На кошелёк ЮMoney поступило после комиссии: <b>{credited_amount:.2f} ₽</b>",
    )
    return web.Response(status=200, text="ok")


# ============================================================
# 29. CRYPTO PAY: ПОПОЛНЕНИЕ ЧЕРЕЗ КРИПТОВАЛЮТУ
# ============================================================
async def crypto_api(method: str, data: dict):
    if not CRYPTO_PAY_TOKEN:
        raise RuntimeError("crypto_not_configured")
    timeout = __import__("aiohttp").ClientTimeout(total=20)
    async with ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{CRYPTO_PAY_API_URL}/{method}",
            headers={"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN, "Content-Type": "application/json"},
            json=data,
        ) as resp:
            payload = await resp.json(content_type=None)
            if resp.status >= 400 or not payload.get("ok"):
                raise RuntimeError(f"crypto_api_error:{payload.get('error')}")
            return payload.get("result") or {}


async def api_crypto_create_payment(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    user_id = telegram_user_from_init_data(str(body.get("initData") or ""))
    if not user_id:
        return web.json_response({"error": "invalid_telegram_init_data"}, status=401)
    try:
        rub = Decimal(str(body.get("amount"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return web.json_response({"error": "invalid_amount"}, status=400)
    if rub < CRYPTO_MIN_RUB or rub > CRYPTO_MAX_RUB:
        return web.json_response({"error": "amount_out_of_range", "min": str(CRYPTO_MIN_RUB), "max": str(CRYPTO_MAX_RUB)}, status=400)
    invoice = await crypto_api("createInvoice", {
        "currency_type": "fiat", "fiat": "RUB", "amount": f"{rub:.2f}",
        "accepted_assets": "USDT,TON,BTC,ETH,LTC,BNB,TRX,USDC",
        "description": f"Пополнение баланса на {rub:.2f} ₽",
        "payload": json.dumps({"user_id": str(user_id), "rub": f"{rub:.2f}"}, ensure_ascii=False),
        "allow_comments": False, "allow_anonymous": False, "expires_in": 1800,
    })
    invoice_id = str(invoice.get("invoice_id") or "")
    if not invoice_id:
        return web.json_response({"error": "invalid_crypto_invoice"}, status=502)
    db_set_json(f"deelo_crypto_pending_{invoice_id}", {
        "user_id": str(user_id), "rub_amount": f"{rub:.2f}", "status": "active",
        "invoice_id": invoice_id, "created_at": __import__("time").time()
    }, 1)
    await _notify_admins_topup_started(str(user_id), rub, "Криптовалюта", invoice_id)
    return web.json_response({"ok": True, "invoiceId": invoice_id, "url": invoice.get("mini_app_invoice_url") or invoice.get("web_app_invoice_url") or invoice.get("pay_url")})


async def api_crypto_payment_status(request: web.Request):
    user_id = telegram_user_from_init_data(str(request.headers.get("X-Telegram-Init-Data") or request.query.get("initData") or ""))
    if not user_id:
        return web.json_response({"error": "invalid_telegram_init_data"}, status=401)
    invoice_id = str(request.query.get("invoice_id") or "")
    pending = db_get_json(f"deelo_crypto_pending_{invoice_id}")
    if not pending or str(pending.get("user_id")) != str(user_id):
        return web.json_response({"error": "payment_not_found"}, status=404)
    result = await crypto_api("getInvoices", {"invoice_ids": invoice_id})
    items = result.get("items") or []
    invoice = items[0] if items else None
    if not invoice:
        return web.json_response({"ok": True, "status": "unknown"})
    status = str(invoice.get("status") or "active")
    if status == "paid":
        rub = Decimal(str(pending.get("rub_amount") or "0"))
        marker = f"deelo_crypto_paid_{invoice_id}"
        ok, reason = _credit_internal_balance(
            str(user_id), rub, marker,
            {"invoice_id": invoice_id, "user_id": str(user_id), "rub_amount": f"{rub:.2f}",
             "paid_asset": invoice.get("paid_asset"), "paid_amount": invoice.get("paid_amount")},
            f"Баланс пополнен на {rub:.2f} ₽ через Криптовалюту.", "💎"
        )
        pending["status"] = "paid"
        pending["paid_asset"] = invoice.get("paid_asset")
        pending["paid_amount"] = invoice.get("paid_amount")
        db_set_json(f"deelo_crypto_pending_{invoice_id}", pending, 1)
        await _notify_admins_topup_paid(str(user_id), rub, rub, "Криптовалюта", invoice_id, f"💎 Актив: <b>{invoice.get('paid_asset') or '—'}</b> · {invoice.get('paid_amount') or '—'}")
        return web.json_response({"ok": True, "status": "paid", "credited": reason in {"credited", "already_processed"}})
    return web.json_response({"ok": True, "status": status})


# ============================================================
# 30. ВЫВОД: ЗАЯВКИ НА РУЧНУЮ ВЫПЛАТУ
# ============================================================
def _withdraw_key(request_id: str) -> str:
    return f"deelo_withdraw_{request_id}"


def _withdraw_request_id() -> str:
    import time
    return f"WD-{time.strftime('%y%m%d')}-{random.randint(1000, 9999)}"


def _admin_ids() -> set[str]:
    return {str(x) for x in ADMIN_IDS}


def _withdraw_admin_keyboard(request_id: str, user_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"wd_approve:{request_id}")],
        [
            InlineKeyboardButton(text="❌ Не верифицирован", callback_data=f"wd_reject:{request_id}:verification"),
            InlineKeyboardButton(text="❌ Реквизиты", callback_data=f"wd_reject:{request_id}:details"),
        ],
        [
            InlineKeyboardButton(text="❌ Проверка / лимиты", callback_data=f"wd_reject:{request_id}:limits"),
            InlineKeyboardButton(text="❌ Своя причина", callback_data=f"wd_custom:{request_id}"),
        ],
        [InlineKeyboardButton(text="💬 Написать пользователю", callback_data=f"wd_message:{request_id}")],
        [InlineKeyboardButton(text="💬 Чат поддержки", web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=support"))],
        [InlineKeyboardButton(text="👤 В админке", web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=admin"))],
        [InlineKeyboardButton(text="👑 Админ-панель", web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=admin"))],
    ])


def _withdraw_text(req: dict, title: str = "🆕 НОВАЯ ЗАЯВКА НА ВЫВОД") -> str:
    user = req.get("user") or {}
    first_name = user.get("first_name") or user.get("username") or "Пользователь"
    username = user.get("telegram_username") or user.get("username") or "без username"
    if username and not username.startswith("@"): username = "@" + username
    method = req.get("method")
    country = req.get("country") or "—"
    amount = Decimal(str(req.get("amount") or "0")).quantize(Decimal("0.01"))
    lines = [
        f"<b>{title}</b>",
        "",
        f"<b>{first_name}</b> ({username})",
        f"ID: <code>{req.get('user_id')}</code>",
        f"Сумма: <b>{amount:.2f} ₽</b>",
        f"К выплате: <b>{amount:.2f} ₽</b> (комиссия 0%)",
    ]
    if method == "CARD":
        d = req.get("details") or {}
        full_name = " ".join([str(d.get("surname") or "").strip(), str(d.get("name") or "").strip()]).strip()
        lines += [f"{country} Перевод на банковскую карту", f"{d.get('card') or '—'}, {full_name or '—'}"]
    elif method == "STARS":
        d = req.get("details") or {}
        lines += ["⭐ Вывод Telegram Stars", f"@{str(d.get('username') or '').lstrip('@')}"]
    elif method == "CRYPTO":
        d = req.get("details") or {}
        lines += [f"💎 Криптовалюта: {d.get('asset') or 'USDT'}", f"Сеть: {d.get('network') or '—'}", f"Адрес: <code>{d.get('address') or '—'}</code>"]
    lines += ["", f"Заявка: <code>{req.get('id')}</code>", f"Статус: <b>{req.get('status_text') or 'Ожидает выплаты'}</b>"]
    return "\n".join(lines)


async def _notify_admins_withdraw(req: dict):
    kb = _withdraw_admin_keyboard(req["id"], req["user_id"])
    for admin_id in _admin_ids():
        try:
            await bot.send_message(int(admin_id), _withdraw_text(req), parse_mode="HTML", reply_markup=kb)
        except Exception:
            log.exception("Не удалось отправить заявку на вывод админу %s", admin_id)


async def api_withdraw_create(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    user_id = telegram_user_from_init_data(str(body.get("initData") or ""))
    if not user_id:
        return web.json_response({"error": "invalid_telegram_init_data"}, status=401)
    user = db_get_json(f"deelo_user_{user_id}", {}) or {}
    try:
        amount = Decimal(str(body.get("amount"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return web.json_response({"error": "invalid_amount"}, status=400)
    if amount < Decimal("100.00"):
        return web.json_response({"error": "amount_too_small"}, status=400)

    # Verification is the final gate after the client has entered the form data.
    # It must be returned even when the account currently has insufficient funds,
    # so unverified users always see the verification message they expect.
    if not user.get("verified") or int(user.get("level") or 0) < 1:
        return web.json_response({
            "error": "verification_required",
            "message": "Для вывода необходимо пройти верификацию."
        }, status=403)

    try:
        method = str(body.get("method") or "")
        country = str(body.get("country") or "")
        details = body.get("details") or {}
        if method == "CARD":
            surname = str(details.get("surname") or "").strip()
            name = str(details.get("name") or "").strip()
            card = re.sub(r"\s+", "", str(details.get("card") or ""))
            if not surname or not name or not re.fullmatch(r"\d{12,19}", card):
                return web.json_response({"error": "invalid_card_details"}, status=400)
            details = {"surname": surname, "name": name, "card": card}
        elif method == "STARS":
            raw_recipient = str(
                details.get("recipient")
                or details.get("username")
                or details.get("recipient_id")
                or ""
            ).strip()
            if raw_recipient.startswith("@"):
                raw_recipient = raw_recipient[1:]

            if re.fullmatch(r"[A-Za-z0-9_]{5,32}", raw_recipient):
                details = {
                    "recipient_type": "username",
                    "recipient": raw_recipient,
                    "username": raw_recipient,
                }
            elif re.fullmatch(r"\d{5,20}", raw_recipient):
                details = {
                    "recipient_type": "telegram_id",
                    "recipient": raw_recipient,
                    "recipient_id": raw_recipient,
                }
            else:
                return web.json_response({"error": "invalid_username"}, status=400)
        elif method == "CRYPTO":
            asset = str(details.get("asset") or "USDT").upper()
            network = str(details.get("network") or "").strip()
            address = str(details.get("address") or "").strip()
            if asset not in {"USDT", "TON", "BTC", "ETH"} or len(address) < 12 or len(address) > 160 or not network:
                return web.json_response({"error": "invalid_crypto_address"}, status=400)
            details = {"asset": asset, "network": network, "address": address}
        else:
            return web.json_response({"error": "invalid_method"}, status=400)

        request_id = _withdraw_request_id()
        with db_connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (f"deelo_user_{user_id}",)).fetchone()
            if not row:
                return web.json_response({"error": "user_not_found"}, status=404)
            rec = json.loads(row[0])
            balance = Decimal(str(rec.get("balance") or 0)).quantize(Decimal("0.01"))
            if amount > balance:
                return web.json_response({"error": "insufficient_balance"}, status=400)
            rec["balance"] = float(balance - amount)
            conn.execute("UPDATE kv SET value=?, updated_at=strftime('%s','now') WHERE key=?", (json.dumps(rec, ensure_ascii=False), f"deelo_user_{user_id}"))
            req = {
                "id": request_id, "user_id": str(user_id), "amount": f"{amount:.2f}",
                "method": method, "country": country, "details": details,
                "status": "pending", "status_text": "Ожидает выплаты",
                "created_at": __import__("time").time(),
                "user": {"first_name": user.get("username") or "Пользователь", "username": user.get("username"), "telegram_username": user.get("telegramUsername")},
            }
            conn.execute("INSERT INTO kv(key,value,shared,updated_at) VALUES(?,?,1,strftime('%s','now'))", (_withdraw_key(request_id), json.dumps(req, ensure_ascii=False)))
            conn.commit()
    except Exception:
        log.exception("withdraw create failed")
        return web.json_response({"error": "server_error"}, status=500)

    await _notify_admins_withdraw(req)
    append_notification(user_id, f"Заявка на вывод {amount:.2f} ₽ создана. Ожидайте выплату в течение 24 часов.", "Вывод средств", "💸")
    append_payment_history(user_id, "withdraw", -amount, "Вывод средств", f"Заявка №{request_id} · ожидает выплаты", "pending", request_id)
    return web.json_response({"ok": True, "requestId": request_id, "balance": f"{rec['balance']:.2f}"})


def _withdraw_reason_text(reason: str) -> str:
    return {
        "verification": "❌ Заявка отклонена: для вывода необходимо пройти верификацию.",
        "details": "❌ Заявка отклонена: реквизиты требуют исправления. Средства возвращены на баланс.",
        "limits": "❌ Заявка отклонена: вывод не прошёл проверку/лимиты. Средства возвращены на баланс.",
    }.get(reason, "❌ Заявка отклонена. Средства возвращены на баланс.")


async def _withdraw_finish(request_id: str, status: str, reason: str | None = None, admin_id: str | None = None):
    req = db_get_json(_withdraw_key(request_id))
    if not req:
        return False, "Заявка не найдена"
    if req.get("status") != "pending":
        return False, f"Заявка уже обработана: {req.get('status_text') or req.get('status')}"
    if status == "rejected":
        uid = str(req.get("user_id"))
        with db_connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (f"deelo_user_{uid}",)).fetchone()
            if row:
                rec = json.loads(row[0])
                rec["balance"] = float(Decimal(str(rec.get("balance") or 0)) + Decimal(str(req.get("amount") or 0)))
                conn.execute("UPDATE kv SET value=?, updated_at=strftime('%s','now') WHERE key=?", (json.dumps(rec, ensure_ascii=False), f"deelo_user_{uid}"))
            req["status"] = "rejected"
            req["status_text"] = "Отклонена · деньги возвращены"
            req["reason"] = reason or "other"
            req["admin_id"] = admin_id
            conn.execute("UPDATE kv SET value=?, updated_at=strftime('%s','now') WHERE key=?", (json.dumps(req, ensure_ascii=False), _withdraw_key(request_id)))
            conn.commit()
        append_payment_history(uid, "withdraw_refund", Decimal(str(req.get("amount") or 0)), "Возврат вывода", f"Заявка №{request_id} отклонена · средства возвращены", "completed", request_id)
        append_notification(uid, f"Вывод {Decimal(str(req.get('amount') or 0)):.2f} ₽ отклонён. Средства возвращены на баланс.", "Возврат средств", "↩️")
        await bot.send_message(int(uid), _withdraw_reason_text(reason or "other"))
    else:
        req["status"] = "approved"
        req["status_text"] = "Одобрена · ожидает выплаты"
        req["admin_id"] = admin_id
        db_set_json(_withdraw_key(request_id), req, 1)
        await bot.send_message(int(req["user_id"]), "✅ Заявка на вывод одобрена. Ожидайте выплату в течение 24 часов.")
    return True, req.get("status_text")


@dp.callback_query(F.data.startswith("wd_approve:"))
async def cb_withdraw_approve(callback: CallbackQuery):
    if str(callback.from_user.id) not in _admin_ids():
        await callback.answer("Нет доступа", show_alert=True); return
    request_id = callback.data.split(":", 1)[1]
    ok, text = await _withdraw_finish(request_id, "approved", admin_id=str(callback.from_user.id))
    await callback.answer(text, show_alert=not ok)
    if ok:
        try: await callback.message.edit_reply_markup(reply_markup=None)
        except Exception: pass


@dp.callback_query(F.data.startswith("wd_reject:"))
async def cb_withdraw_reject(callback: CallbackQuery):
    if str(callback.from_user.id) not in _admin_ids():
        await callback.answer("Нет доступа", show_alert=True); return
    _, request_id, reason = callback.data.split(":", 2)
    ok, text = await _withdraw_finish(request_id, "rejected", reason=reason, admin_id=str(callback.from_user.id))
    await callback.answer(text, show_alert=not ok)
    if ok:
        try: await callback.message.edit_reply_markup(reply_markup=None)
        except Exception: pass


@dp.callback_query(F.data.startswith("wd_custom:"))
async def cb_withdraw_custom(callback: CallbackQuery):
    if str(callback.from_user.id) not in _admin_ids():
        await callback.answer("Нет доступа", show_alert=True); return
    request_id = callback.data.split(":", 1)[1]
    db_set_json(f"deelo_admin_withdraw_custom_{callback.from_user.id}", {"request_id": request_id}, 1)
    await callback.answer()
    await callback.message.answer("Напишите причину отказа одним сообщением.")


@dp.callback_query(F.data.startswith("wd_message:"))
async def cb_withdraw_message(callback: CallbackQuery):
    if str(callback.from_user.id) not in _admin_ids():
        await callback.answer("Нет доступа", show_alert=True); return
    request_id = callback.data.split(":", 1)[1]
    req = db_get_json(_withdraw_key(request_id))
    if not req:
        await callback.answer("Заявка не найдена", show_alert=True); return
    db_set_json(f"deelo_admin_withdraw_message_{callback.from_user.id}", {"user_id": req["user_id"]}, 1)
    await callback.answer()
    await callback.message.answer("Напишите сообщение пользователю одним сообщением.")


# ============================================================
# 31. TOME: СОЗДАНИЕ ПЛАТЕЖА
# ============================================================
async def api_tome_create_payment(request: web.Request):
    """Create a Tome payment for the authenticated Telegram Mini App user."""
    if not TOME_SHOP_ID or not TOME_SECRET_KEY:
        return web.json_response({"error": "tome_not_configured"}, status=503)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    init_data = str(body.get("initData") or "")
    user_id = telegram_user_from_init_data(init_data)
    if not user_id:
        return web.json_response({"error": "invalid_telegram_init_data"}, status=401)

    try:
        amount = Decimal(str(body.get("amount"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return web.json_response({"error": "invalid_amount"}, status=400)

    if amount < Decimal("0.01"):
        return web.json_response({"error": "amount_too_small"}, status=400)
    if amount > Decimal("1000000.00"):
        return web.json_response({"error": "amount_too_large"}, status=400)

    user_rec = db_get_json(f"deelo_user_{user_id}")
    if not user_rec:
        return web.json_response({"error": "user_not_found"}, status=404)

    payment_payload = {
        "account_type": "merchant",
        "amount": {
            "value": f"{amount:.2f}",
            "currency": "RUB",
        },
        # customer намеренно не передаём:
        # на странице Tome пользователь сможет выбрать доступный способ оплаты.
        "confirmation": {
            "type": "redirect",
            "return_url": f"{WEBAPP_URL}#wallet",
        },
        "description": f"Пополнение баланса пользователя {user_id}",
        "metadata": {
            "user_id": str(user_id),
        },
    }

    try:
        timeout = __import__("aiohttp").ClientTimeout(total=20)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{TOME_API_URL}/payments",
                auth=BasicAuth(TOME_SHOP_ID, TOME_SECRET_KEY),
                headers={
                    "Idempotency-Key": str(uuid.uuid4()),
                    "Content-Type": "application/json",
                },
                json=payment_payload,
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    log.error("Tome create payment failed: status=%s body=%s", resp.status, data)
                    err = data.get("error") if isinstance(data, dict) else None
                    safe_code = err.get("code") if isinstance(err, dict) else None
                    safe_description = err.get("description") if isinstance(err, dict) else None
                    return web.json_response(
                        {
                            "error": "tome_api_error",
                            "tome_code": safe_code,
                            "tome_description": safe_description,
                        },
                        status=502,
                    )
    except Exception:
        log.exception("Tome create payment request failed")
        return web.json_response({"error": "tome_request_failed"}, status=502)

    confirmation_url = ((data.get("confirmation") or {}).get("confirmation_url"))
    payment_id = data.get("id")
    if not confirmation_url or not payment_id:
        log.error("Tome response has no payment URL/id: %s", data)
        return web.json_response({"error": "invalid_tome_response"}, status=502)

    return web.json_response({
        "ok": True,
        "paymentId": payment_id,
        "status": data.get("status"),
        "confirmationUrl": confirmation_url,
    })

# ============================================================
# 30. SUPPORT: FULL LOCAL KNOWLEDGE ENGINE v8 — USER FRIENDLY
# ============================================================

SUPPORT_VERIFY_RUB_MIN = 1000.0
SUPPORT_VERIFY_STARS_MIN = 500
SUPPORT_WITHDRAW_MIN = 100.0
SUPPORT_COMMISSION_PCT = 12.5
_SUPPORT_CARD_MARKER = "__VERIFICATION_CARD__"
_SUPPORT_CARD_SENTINEL = "\u241E"


def _support_norm(text: str) -> str:
    t = str(text or "").lower().replace("ё", "е")
    repl = {
        "верифка":"верификация","верифку":"верификация","верефикация":"верификация",
        "вериф":"верификация","верифицироваться":"пройти верификацию","верифицирован":"верификация есть",
        "галочка":"значок","значек":"значок","паполнить":"пополнить","попалнить":"пополнить",
        "закинуть":"пополнить","балик":"баланс","бабки":"деньги","вывисти":"вывести",
        "снять деньги":"вывести деньги","забрать деньги":"вывести деньги",
        "звезды":"stars","звёзды":"stars","звезд":"stars","старсы":"stars","старс":"stars",
        "telegram stars":"stars","тг старс":"stars","крипта":"криптовалюта","криптой":"криптовалюта",
        "юсдт":"usdt","сделку":"сделка","сделки":"сделка","сделке":"сделка",
        "создать гарант":"создать сделка","сделать гарант":"создать сделка",
        "продаван":"продавец","покупашка":"покупатель","саппорт":"поддержка","админ":"администратор",
    }
    for a,b in repl.items():
        t=t.replace(a,b)
    t=re.sub(r"[^a-zа-я0-9@#₽⭐+._,\- ]+"," ",t)
    return re.sub(r"\s+"," ",t).strip()


def _support_tokens(text: str) -> list[str]:
    return [x for x in _support_norm(text).split() if len(x)>=2]


def _support_word_like(a: str,b: str,cutoff: float=.72)->bool:
    from difflib import SequenceMatcher
    a,b=_support_norm(a),_support_norm(b)
    if not a or not b:return False
    if a==b:return True
    if len(a)>=5 and len(b)>=5 and (a.startswith(b[:5]) or b.startswith(a[:5])):return True
    if min(len(a),len(b))<=3:return False
    return SequenceMatcher(None,a,b).ratio()>=cutoff


def _support_has(text: str,*targets: str)->bool:
    ws=_support_tokens(text)
    return any(_support_word_like(w,q) for w in ws for q in targets)


def _support_phrase_score(text: str,phrases: list[str])->float:
    from difflib import SequenceMatcher
    t=_support_norm(text); ws=_support_tokens(t); best=0.0
    for raw in phrases:
        p=_support_norm(raw)
        if not p:continue
        if p in t:
            best=max(best,1.0);continue
        pws=_support_tokens(p)
        if not pws:continue
        matched=sum(1 for q in pws if any(_support_word_like(w,q) for w in ws))
        best=max(best,(matched/len(pws))*.82+SequenceMatcher(None,t,p).ratio()*.18)
    return best


def _support_last_user_messages(user_id: str,limit: int=10)->list[str]:
    h=db_get_json(f"deelo_support_{user_id}",[]) or []
    out=[str(m.get("text")) for m in h[-60:] if m.get("role")=="user" and m.get("text")]
    return out[-limit:]


def _support_server_context(user_id: str)->dict:
    user=db_get_json(f"deelo_user_{user_id}",{}) or {}
    deals=[]
    for deal_id in (db_get_json(f"deelo_dealindex_{user_id}",[]) or [])[-40:]:
        d=db_get_json(f"deelo_deal_{deal_id}")
        if d:
            deals.append({
                "id":d.get("id"),"code":deal_code_server(d.get("id")),"status":d.get("status"),
                "title":d.get("title") or d.get("desc"),"amount":d.get("amount"),
                "amountRub":d.get("amountRub"),"buyerPays":d.get("buyerPays"),
                "buyerPaysRub":d.get("buyerPaysRub"),"currency":d.get("currency","RUB"),
                "sellerId":str(d.get("sellerId") or ""),"buyerId":str(d.get("buyerId") or ""),
                "dispute":d.get("dispute"),"disputeOpen":bool(d.get("disputeOpen")),
            })
    try: payments=db_get_json(_payment_history_key(str(user_id)),[]) or []
    except Exception: payments=[]
    return {
        "user":{k:user.get(k) for k in [
            "id","username","telegramUsername","phone","balance","level","verified","verifiedAt",
            "verificationReason","topUpTotal","dealsTotal","dealsSuccess","ratingCount","ratingSum","banned"
        ]},
        "recent_deals":deals,
        "recent_notifications":(db_get_json(f"deelo_notifs_{user_id}",[]) or [])[-20:],
        "recent_payments":payments[-20:],
    }


_INTENTS = {
"verify_how":["как получить верификацию","как пройти верификацию","как сделать верификацию","как оформить верификацию","как можно получить верификацию","как стать верифицированным","хочу верификацию","как получить значок","где взять значок"],
"verify_why":["для чего нужна верификация","зачем нужна верификация","что дает верификация","что даст верификация","зачем значок","плюсы верификации","преимущества верификации"],
"verify_what":["что такое верификация","что значит верификация","что означает верификация"],
"verify_status":["есть ли у меня верификация","я верифицирован","мой уровень верификации","какой у меня уровень","проверь верификацию"],
"verify_amount":["сколько надо для верификации","сколько пополнить для верификации","какая сумма для верификации","минимум для верификации","1000 для верификации"],
"verify_999":["можно 999","999 для верификации","если пополню 999"],
"verify_split":["можно несколькими платежами","можно частями","два платежа по 500","500 и 500","суммируются платежи"],
"verify_stars":["верификация stars","можно stars","500 stars","499 stars","звездами верификация"],
"verify_crypto":["верификация криптой","можно криптовалютой верификацию","usdt для верификации","ton для верификации"],
"verify_money":["деньги сгорят","куда денутся 1000","это комиссия за верификацию","деньги останутся","плата за значок"],
"verify_existing":["у меня уже есть 1000 на балансе","если на балансе уже 1000","деньги уже на балансе верификация"],
"verify_after":["что после верификации","что делать после пополнения 1000","когда появится значок","значок сам появится","после оплаты что дальше"],
"verify_missing":["пополнил 1000 но нет верификации","значок не появился","верификация не появилась","уровень остался 0","оплатил а верификации нет"],
"verify_level2":["как получить 2 уровень","как пройти второй уровень","что такое 2 уровень","подтвердить номер","верификация телефона","второй уровень"],
"verify_withdraw":["нужна верификация для вывода","можно вывести без верификации","почему без верификации нельзя вывести","вывод без значка"],

"topup_how":["как пополнить баланс","как пополнить кошелек","как закинуть деньги","где пополнить","хочу пополнить","пополнение баланса","как внести деньги"],
"topup_methods":["какие способы пополнения","чем можно пополнить","способы пополнения","можно картой пополнить","можно stars пополнить","можно криптой пополнить"],
"topup_limits":["минимальное пополнение","максимальное пополнение","сколько минимум пополнить","сколько максимум пополнить","лимит пополнения"],
"topup_missing":["пополнил но деньги не пришли","баланс не пополнился","оплатил но денег нет","платеж прошел но баланс старый","не зачислилось пополнение"],
"topup_stars":["как пополнить stars","пополнить звездами","счет stars","курс stars"],
"topup_crypto":["как пополнить криптой","пополнить crypto","пополнить usdt","crypto bot пополнение"],

"withdraw_how":["как вывести баланс","как вывести деньги","как вывести средства","где вывести","хочу вывести","снять баланс","вывести с кошелька"],
"withdraw_problem":["не могу вывести","почему не выводит","вывод отклонен","вывод недоступен","не получается вывести"],
"withdraw_min":["минимальный вывод","сколько минимум вывести","минимум на вывод"],
"withdraw_methods":["куда можно вывести","способы вывода","вывод на карту","вывод stars","вывод крипта","crypto bot вывод"],
"withdraw_pending":["сколько ждать вывод","когда придет вывод","вывод в обработке","заявка на вывод ожидает","как долго вывод"],
"withdraw_reserved":["почему деньги списались после заявки","баланс уменьшился после вывода","деньги зарезервированы на вывод"],

"deal_create":["как создать сделку","как сделать сделку","как начать сделку","как открыть сделку","создать новую сделку","как создать гарант","как оформить сделку","хочу сделать сделку"],
"deal_role":["что выбрать продавец покупатель","какую роль выбрать","кто продавец кто покупатель","роль в сделке"],
"deal_user":["как указать участника","ник второго участника","куда писать username","ник без @","не находит участника"],
"deal_amount":["какую сумму указывать","сумма продавца","почему покупатель платит больше","сколько получит продавец","сколько платит покупатель"],
"deal_commission":["какая комиссия","комиссия сделки","12 5 комиссия","кто платит комиссию","почему комиссия сверху"],
"deal_flow":["как работает сделка","этапы сделки","что происходит после создания","как проходит сделка","что дальше по сделке"],
"deal_pay":["как оплатить сделку","кто оплачивает сделку","оплата сделки","недостаточно денег для сделки"],
"deal_reserve":["что такое резерв","деньги в резерве","деньги заморожены","когда продавец получит деньги"],
"deal_transfer":["как передать товар","товар был передан","я передал товар","когда нажимать товар передан"],
"deal_receive":["как подтвердить получение","товар получен","покупатель получил","когда подтверждать товар"],
"deal_dispute":["как открыть спор","меня обманули","продавец пропал","покупатель пропал","товар не соответствует","подключить администратора"],
"deal_accept":["как принять сделку","принять приглашение","мне пришла сделка","подтвердить сделку"],
"deal_decline":["как отклонить сделку","как отказаться от сделки","не хочу принимать сделку"],
"deal_target_missing":["пользователь не найден","не находит участника","target not found","второй участник не найден"],
"deal_currency":["какие валюты в сделке","валюта сделки","можно usd","можно uah","можно kzt","можно usdt","можно stars в сделке"],
"deal_link":["ссылка на сделку","как отправить ссылку на сделку","не пришла сделка участнику","поделиться сделкой"],
"deal_chat":["где чат сделки","чат сделки","как написать участнику","переписка сделки"],
"deal_templates":["шаблон сообщения","шаблоны сделки","сохранить шаблон","вставить логин","вставить ключ"],
"deal_search":["как найти сделку","поиск сделки","фильтр сделок","найти по коду"],
"deal_repeat":["повторить сделку","повтор последней сделки","создать такую же сделку"],
"payment_history":["история платежей","история операций","история пополнений","история выводов","посмотреть платежи"],
"payment_status":["статус платежа","платеж в обработке","что значит в обработке","что значит выполнено"],
"profile_general":["мой профиль","что показывает профиль","данные профиля"],
"profile_level":["что такое уровень","мой уровень","как повысить уровень","уровень аккаунта"],
"profile_rating":["что такое рейтинг","как считается рейтинг","мой рейтинг","почему нет рейтинга"],
"profile_stats":["статистика сделок","сколько у меня сделок","успешные сделки"],
"profile_personal":["как изменить имя","как изменить фамилию","личные данные","персональные данные"],
"profile_phone":["как привязать номер","подтвердить номер","верификация телефона","почему нет кнопки подтвердить номер"],
"profile_banned":["аккаунт заблокирован","меня забанили","почему бан","как снять блокировку"],
"notifications":["где уведомления","как открыть уведомления","колокольчик","уведомления сделки"],
"notification_settings":["отключить уведомления","включить уведомления","настройки уведомлений","уведомления платежей"],
"settings_general":["где настройки","настройки приложения","настройки профиля"],
"reviews_general":["где отзывы","что такое отзывы","посмотреть отзывы"],
"review_leave":["как оставить отзыв","оставить отзыв","написать отзыв","поставить звезды"],
"review_limit":["почему не могу оставить отзыв","отзыв недоступен","не дает написать отзыв"],
"referral_general":["реферальная программа","реферал","пригласить друга","бонус за друга","реферальная ссылка"],
"rules":["правила сервиса","что запрещено","правила сделки","справка и правила"],
"privacy":["политика конфиденциальности","какие данные храните","privacy"],
"security":["безопасность","мошенничество","как не попасть на мошенника","просят код","просят пароль","просят данные карты"],
"direct_payment":["можно перевести напрямую","перевод мимо сделки","оплатить продавцу напрямую","продавец просит перевод на карту"],
"app_open":["как открыть приложение","mini app не открывается","откройте mini app из telegram","не работает в браузере"],
"app_refresh":["данные не обновились","старый баланс","старый статус","обновить приложение"],
"attachments":["можно отправить скриншот","прикрепил фото","посмотри скрин","вложение","файл в поддержку"],
"support_scope":["что ты умеешь","с чем можешь помочь","какие вопросы можно задать","помощь"],
"service_how":["как работает сервис","как работает playerok","что это за сервис","как это работает"],

}


def _support_best_intent(text: str,previous: str="")->tuple[str,float]:
    t=_support_norm(text)
    scores={k:_support_phrase_score(t,v) for k,v in _INTENTS.items()}
    hv=_support_has(t,"верификация","значок","уровень")
    if hv and "999" in t:scores["verify_999"]=1.4
    if hv and "stars" in t:scores["verify_stars"]=max(scores["verify_stars"],1.3)
    if hv and any(x in t for x in ("криптовалюта","usdt","ton")):scores["verify_crypto"]=max(scores["verify_crypto"],1.3)
    if hv and _support_has(t,"вывести","вывод"):scores["verify_withdraw"]=max(scores["verify_withdraw"],1.3)
    if _support_has(t,"сделка","гарант") and _support_has(t,"комиссия"):scores["deal_commission"]=max(scores["deal_commission"],1.3)
    if "отзыв" in t and any(x in t for x in ("не могу","нельзя","недоступ","не дает","почему не")):scores["review_limit"]=max(scores["review_limit"],1.35)
    if previous and len(_support_tokens(t))<=8:
        p=_support_norm(previous)
        if _support_has(p,"верификация"):
            if "stars" in t:scores["verify_stars"]=max(scores["verify_stars"],1.3)
            elif "999" in t:scores["verify_999"]=max(scores["verify_999"],1.3)
            elif any(x in t for x in ("криптовалюта","usdt","ton")):scores["verify_crypto"]=max(scores["verify_crypto"],1.3)
            elif _support_has(t,"зачем","дает","нужна"):scores["verify_why"]=max(scores["verify_why"],1.2)
    key=max(scores,key=scores.get)
    return key,scores[key]


def _active_deal(ctx: dict):
    active={"pending_accept","accepted","awaiting_payment","paid","reserved","transferred","delivered","dispute"}
    for d in reversed(ctx.get("recent_deals") or []):
        if str(d.get("status") or "").lower() in active:return d
    return None


def _deal_role(d: dict,user_id: str)->str:
    if str(d.get("sellerId") or "")==str(user_id):return "sell"
    if str(d.get("buyerId") or "")==str(user_id):return "buy"
    return ""


def _deal_next(d: dict,user_id: str)->str:
    st=str(d.get("status") or "").lower(); role=_deal_role(d,user_id)
    if d.get("disputeOpen") or st=="dispute":return "По сделке открыт спор. Не подтверждай завершение и дождись администратора."
    if st=="pending_accept":return "Сейчас второй участник должен подтвердить приглашение."
    if st in {"accepted","awaiting_payment"}:return "Оплати сделку с внутреннего баланса — деньги уйдут в резерв." if role=="buy" else "Ожидай оплату покупателя; до оплаты товар не передавай."
    if st in {"paid","reserved"}:return "Передай товар в чате и нажми «Товар был передан»." if role=="sell" else "Деньги в резерве; ожидай товар от продавца."
    if st in {"transferred","delivered"}:return "Проверь товар: если всё хорошо — нажми «Товар получен», иначе открой спор." if role=="buy" else "Ожидай подтверждение покупателя."
    if st in {"completed","done","finished"}:return "Сделка завершена; выплата продавцу производится после подтверждения."
    if st=="declined":return "Приглашение отклонено; для продолжения создай новую сделку."
    return "Открой карточку сделки — там показан текущий этап."


def _m(v)->str:
    try:return f"{float(v or 0):.2f}".replace(".",",")
    except Exception:return "0,00"


def _core_reply(intent: str,user_id: str,ctx: dict)->str:
    u=ctx.get("user") or {}; bal=float(u.get("balance") or 0); verified=bool(u.get("verified")); level=int(u.get("level") or 0)

    if intent=="verify_how":
        if verified and level>=1:
            return f"У тебя верификация уже пройдена — сейчас {level} уровень. Повторно проходить её не нужно."
        ans=("Вот шаги, коротко:\n\n"
             "1. Открой «Верификация» в профиле или «Кошелёк» → «Пополнить».\n"
             "2. Для банковской карты пополни баланс одним успешным платежом минимум на 1 000 ₽.\n"
             "3. Также можно пройти верификацию через Telegram Stars — от 500 ⭐. Криптопополнение для получения значка не подходит.\n"
             "4. Деньги не являются платой за значок — они остаются на внутреннем балансе.\n\n"
             "После получения 1 уровня станет доступен вывод средств.")
        return ans

    if intent=="verify_why":
        return ("Верификация нужна в первую очередь для вывода средств — без неё заявку на вывод не примут. "
                "Кроме этого, в профиле появляется значок подтверждённого аккаунта и снимаются ограничения для новых пользователей на сделки. "
                "Пополнение для верификации не является платой или комиссией за значок: деньги остаются у тебя на балансе.")

    if intent=="verify_what":return "Верификация — это подтверждение аккаунта. После 1 уровня появляется значок, становится доступен вывод средств и снимаются ограничения для новых пользователей на сделки."
    if intent=="verify_status":return f"Сейчас у тебя: верификация — {'есть' if verified else 'нет'}, уровень — {level}, баланс — {_m(bal)} ₽."
    if intent=="verify_amount":return "Для верификации банковской картой нужен один успешный платёж от 1 000 ₽. Через Telegram Stars — от 500 ⭐. Деньги после оплаты остаются на балансе."
    if intent=="verify_999":return "999 ₽ недостаточно. Для верификации банковской картой нужен один успешный платёж минимум на 1 000 ₽."
    if intent=="verify_split":return "Для банковского способа проверяется конкретный оплаченный платёж, поэтому два платежа по 500 ₽ не заменяют один платёж от 1 000 ₽."
    if intent=="verify_stars":return "Да, верификацию можно получить через Telegram Stars. Нужно успешно оплатить от 500 ⭐ за один раз; 499 ⭐ недостаточно. После оплаты рублёвый эквивалент Stars зачислится на баланс, а 1 уровень появится автоматически."
    if intent=="verify_crypto":return "Криптовалютой баланс пополнить можно, но такое пополнение не выдаёт верификацию. Если нужен значок, используй банковскую карту от 1 000 ₽ одним платежом или Telegram Stars от 500 ⭐."
    if intent=="verify_money":return "Деньги не сгорают как плата за значок: подтверждённое пополнение зачисляется на внутренний баланс."
    if intent=="verify_existing":return f"Сейчас на балансе {_m(bal)} ₽, но сам остаток на балансе не выдаёт верификацию автоматически. Значок появляется после подходящего успешного пополнения."
    if intent=="verify_after":return "После успешного подходящего платежа 1 уровень должен появиться автоматически. Открой профиль и проверь статус; если значок сразу не обновился, закрой приложение и открой его снова из Telegram."
    if intent=="verify_missing":return "Если платёж уже успешно зачислен, а верификация не появилась, проверь «Историю платежей» и переоткрой приложение из Telegram. Если уровень всё равно не изменился — напиши в поддержку способ оплаты, сумму и примерное время платежа."
    if intent=="verify_level2":
        if level<1:return "Сначала нужен 1 уровень. После него в профиле появляется подтверждение номера для 2 уровня; отправлять нужно именно свой Telegram-контакт."
        if level>=2:return "У тебя уже 2 уровень; повторно подтверждать номер не нужно."
        return "Для 2 уровня открой профиль → «Подтвердить номер» и отправь свой Telegram-контакт."
    if intent=="verify_withdraw":
        if verified and level>=1:return "Да, для вывода нужна верификация, и у тебя она уже есть. Открой «Кошелёк» → «Вывести», укажи сумму от 100 ₽ и выбери способ получения."
        return "Да, для вывода нужна верификация. Без неё заявку на вывод не примут. Сначала получи 1 уровень: пополни баланс банковской картой от 1 000 ₽ одним платежом или через Telegram Stars от 500 ⭐."

    if intent=="topup_how":
        extra="" if verified else "\n\nЕсли хочешь одновременно получить верификацию: банковский платёж — от 1 000 ₽ одним платежом, либо Telegram Stars — от 500 ⭐."
        return ("Пополнение делается внутри Mini App: «Кошелёк» → «Пополнить», введи сумму и выбери способ. "
                "Доступны банковская карта через ЮMoney, Telegram Stars и криптовалюта через Crypto Pay. После успешной оплаты деньги появятся на балансе."+extra)
    if intent=="topup_methods":return "Пополнить можно банковской картой РФ через ЮMoney, Telegram Stars или криптовалютой через Crypto Pay/Crypto Bot."
    if intent=="topup_limits":return "Основной интерфейс кошелька показывает диапазон пополнения 100–50 000 ₽; у конкретного платёжного способа могут быть свои проверки."
    if intent=="topup_missing":return f"Текущий баланс {_m(bal)} ₽. Открой «История платежей» и проверь статус. Если зачисления нет, напиши способ, сумму и было ли фактическое списание/подтверждение."
    if intent=="topup_stars":return f"Открой «Кошелёк» → «Пополнить» → Telegram Stars. Бот пришлёт счёт в чат. Сейчас 1 ⭐ зачисляет {float(STARS_RUB_PER_STAR):g} ₽ на баланс; после успешной оплаты деньги появятся автоматически."
    if intent=="topup_crypto":return "Открой «Кошелёк» → «Пополнить» → «КриптоВалюта». Дальше откроется оплата через Crypto Pay. После подтверждения сумма зачислится на баланс в рублёвом эквиваленте."

    if intent in {"withdraw_how","withdraw_problem"}:
        if not verified or level<1:
            return ("Вывод делается через «Кошелёк» → «Вывести». Но сейчас у тебя нет верификации, поэтому отправить заявку на вывод не получится. "
                    "Сначала получи 1 уровень: пополни баланс банковской картой от 1 000 ₽ одним платежом или через Telegram Stars от 500 ⭐. "
                    "Деньги останутся на балансе. После появления значка снова открой «Вывести», укажи сумму от 100 ₽ и реквизиты.")
        if bal<SUPPORT_WITHDRAW_MIN:return f"Верификация есть, но баланс {_m(bal)} ₽. Минимальный вывод — 100 ₽."
        return f"Открой «Кошелёк» → «Вывести», введи сумму от 100 ₽, выбери способ и реквизиты. Твой баланс сейчас {_m(bal)} ₽. После создания заявки сумма резервируется, статус — ожидание выплаты."
    if intent=="withdraw_min":return "Минимальный вывод — 100 ₽."
    if intent=="withdraw_methods":return "Вывести средства можно на банковскую карту, в Telegram Stars или в криптовалюте. Для банковских карт доступны Россия, Казахстан, Украина, Беларусь и Узбекистан."
    if intent=="withdraw_pending":return "После создания заявка получает статус «Ожидает выплаты», а уведомление говорит ожидать выплату в течение 24 часов."
    if intent=="withdraw_reserved":return "При создании заявки сумма сразу вычитается из доступного баланса, чтобы её нельзя было вывести повторно; при отклонении логика проекта предусматривает возврат."

    if intent=="deal_create":return ("Чтобы создать сделку:\n\n1. Нажми «Новая сделка».\n2. Выбери роль — продавец или покупатель.\n3. Укажи Telegram-ник второго участника.\n4. Заполни товар/условия.\n5. Укажи сумму продавца и валюту.\n6. Проверь итог: покупатель платит сумму плюс комиссию 12,5%.\n7. Нажми «Отправить заявку».\n\nДальше второй участник принимает приглашение, покупатель оплачивает с внутреннего баланса, деньги идут в резерв.")
    if intent=="deal_role":return "Продавец — тот, кто передаёт товар и получает деньги. Покупатель — тот, кто оплачивает и после проверки подтверждает получение."
    if intent=="deal_user":return "В форме новой сделки укажи Telegram-ник второго участника и перепроверь его перед отправкой заявки."
    if intent=="deal_amount":return "Указывается сумма продавца. Покупатель платит больше, потому что комиссия 12,5% добавляется сверху."
    if intent=="deal_commission":return "Комиссия — 12,5%, её платит покупатель сверху. При сумме продавца 1 000 ₽ покупатель платит 1 125 ₽."
    if intent=="deal_flow":
        d=_active_deal(ctx)
        return (f"По текущей сделке #{d.get('code') or d.get('id')}: {_deal_next(d,user_id)}" if d else
                "Этапы: приглашение → подтверждение → оплата покупателем → резерв → передача товара → подтверждение покупателем → выплата продавцу.")
    if intent=="deal_pay":return f"Платит покупатель с внутреннего баланса. Твой текущий баланс {_m(bal)} ₽. После оплаты деньги находятся в резерве."
    if intent=="deal_reserve":return "Резерв означает: покупатель уже оплатил, но продавец ещё не получил деньги. Выплата идёт после подтверждения покупателем."
    if intent=="deal_transfer":return "Продавцу нужно фактически передать товар/данные, затем нажать «Товар был передан»."
    if intent=="deal_receive":return "Покупателю нужно сначала проверить товар. Если всё соответствует — «Товар получен»; если нет — не подтверждать и открыть спор."
    if intent=="deal_dispute":return "Открой проблемную сделку → «Открыть спор». Не подтверждай получение, пока проблема не решена, и описывай важные детали в чате сделки."
    if intent=="deal_accept":return "Открой входящую сделку и нажми «Принять», если роль, товар, условия и сумма верные. После принятия ожидается оплата покупателем."
    if intent=="deal_decline":return "Пока приглашение ожидает принятия, его можно отклонить кнопкой «Отклонить». Для новых условий потом создаётся новая сделка."
    if intent=="deal_target_missing":return "Если пользователь не найден, он должен хотя бы один раз открыть бота через /start. Затем перепроверь его Telegram username и повтори создание сделки."
    if intent=="deal_currency":return "В интерфейсе сделок поддерживаются RUB, UAH, BYN, KZT, USD, USDT и STARS. Внутренний баланс хранится в рублях."
    if intent=="deal_link":return "Если второй участник не увидел уведомление, открой карточку сделки, скопируй ссылку и отправь её ему в Telegram."
    if intent=="deal_chat":return "Чат находится внутри конкретной сделки. Важные условия и доказательства лучше оставлять там — это помогает при споре."
    if intent=="deal_templates":return "В Mini App можно сохранять шаблоны сообщений для чата сделки — например логин, ключ или инструкцию — и затем быстро вставлять их."
    if intent=="deal_search":return "Открой раздел «Сделки»: сверху есть поиск и фильтры. Если знаешь код сделки, введи его — это самый точный способ найти нужную."
    if intent=="deal_repeat":return "В форме новой сделки можно повторить последнюю сделку как основу. Перед отправкой перепроверь участника, роль, товар и сумму."
    if intent=="payment_history":return "История платежей открывается из кошелька. Там видны пополнения, выводы и другие операции с суммой, датой, статусом и описанием."
    if intent=="payment_status":return "«В обработке» означает, что операция ещё не завершена; «Выполнено» — операция подтверждена системой."
    if intent=="profile_general":return f"В профиле показываются имя/Telegram, уровень, сделки, успешные сделки и рейтинг. Сейчас: уровень {level}, баланс {_m(bal)} ₽, верификация — {'есть' if verified else 'нет'}."
    if intent=="profile_level":
        if level>=2:return "Сейчас у тебя 2 уровень — номер уже подтверждён."
        if level==1:return "Сейчас у тебя 1 уровень. Чтобы получить 2 уровень, открой профиль и подтверди свой номер Telegram."
        return "Сейчас у тебя 0 уровень. 1 уровень получается после прохождения верификации, а 2 уровень — после подтверждения собственного номера Telegram."
    if intent=="profile_rating":return "Рейтинг формируется из оценок/отзывов. Если оценок пока нет, вместо числа может показываться «—»."
    if intent=="profile_stats":return f"По аккаунту: всего сделок — {int(user.get('dealsTotal') or 0)}, успешных — {int(user.get('dealsSuccess') or 0)}, оценок — {int(user.get('ratingCount') or 0)}."
    if intent=="profile_personal":return "В профиле можно изменить имя, фамилию и телефон и сохранить данные. Для получения 2 уровня номер подтверждается отдельно через бота."
    if intent=="profile_phone":return "Для 2 уровня открой профиль → «Подтвердить номер» и отправь только свой Telegram-контакт. Если кнопки ещё нет, сначала нужно получить 1 уровень верификации."
    if intent=="profile_banned":return "Если аккаунт заблокирован администрацией, локальная поддержка снять бан не может. Для разбора укажи Telegram ID/username и причину, почему считаешь блокировку ошибочной."
    if intent=="notifications":return "Уведомления открываются по колокольчику и приходят по сделкам, платежам, выводам и верификации."
    if intent=="notification_settings":return "В настройках профиля можно отдельно включать/выключать уведомления сделок, платежей, выводов и сообщений чата сделки."
    if intent=="settings_general":return "Основные настройки находятся в профиле: личные данные, уведомления, валюта-подсказка, рефералы и шаблоны сообщений."
    if intent=="reviews_general":return "Раздел отзывов показывает среднюю оценку, количество отзывов, распределение звёзд и список отзывов."
    if intent=="review_leave":return "Открой отзывы, выбери 1–5 звёзд, введи текст и отправь. Если форма неактивна, интерфейс показывает, какого условия не хватает."
    if intent=="review_limit":return "Если отзыв недоступен, посмотри текст над формой: Mini App указывает условие допуска; оно может зависеть от числа завершённых сделок."
    if intent=="referral_general":return "В профиле есть реферальная программа: можно поделиться своей ссылкой. Интерфейс показывает бонус 50 ₽ за приглашённого друга и счётчик приглашённых."
    if intent=="rules":return "Используй сервис только для разрешённых товаров/услуг, точно указывай участника, роль, описание и сумму; спорные ситуации решай через спор. Не переводи деньги мимо безопасной сделки."
    if intent=="privacy":return "Политика конфиденциальности открывается из профиля. Там находится полный текст о данных и их обработке."
    if intent=="security":return "Не передавай посторонним одноразовые коды, пароль Telegram, данные карты и другие секреты. Платежи запускай только из Mini App/официального бота."
    if intent=="direct_payment":return "Не переводи деньги продавцу напрямую мимо сделки: при безопасной сделке средства находятся в резерве и можно открыть спор."
    if intent=="app_open":return "Открывай приложение именно из Telegram. Если открыть его обычной ссылкой в браузере, часть функций аккаунта, платежей и сделок может быть недоступна."
    if intent=="app_refresh":return "Если баланс, уровень или статус операции не обновился, полностью закрой приложение и открой его снова из Telegram. Если ничего не изменилось — напиши, какая операция была и что сейчас отображается."
    if intent=="attachments":return "Файл/скрин можно прикрепить, но локальная поддержка не распознаёт содержимое изображения. Напиши текстом ошибку или опиши, что на скриншоте."
    if intent=="support_scope":return "Могу помочь с верификацией, пополнением и выводом, платежами, сделками, комиссией, резервом, спорами, профилем, отзывами, уведомлениями, реферальной программой, правилами и безопасностью."
    if intent=="service_how":return "Схема сервиса: создаётся сделка → второй участник принимает → покупатель платит → деньги в резерве → продавец передаёт товар → покупатель подтверждает → продавец получает выплату."
    return ""


def _local_support_reply(user_id: str,text: str)->tuple[str|None,float,str]:
    t=_support_norm(text)
    if not t:return "Напиши, что хочешь сделать в Playerok — помогу разобраться.",1.0,"empty"
    if t in {"привет","здравствуйте","хай","hello","добрый день","добрый вечер"}:
        return "Привет! Конечно, помогу. Можешь спросить про верификацию, пополнение, вывод, сделку, комиссию, резерв, спор или любую функцию приложения.",1.0,"hello"
    prev=" ".join(_support_last_user_messages(user_id,8)[-2:])
    intent,conf=_support_best_intent(t,prev)
    if conf>=.46:
        reply=_core_reply(intent,user_id,_support_server_context(user_id))
        if reply:return reply,conf,intent
    return (
        "Не совсем понял, о чём именно ты спрашиваешь. Напиши чуть подробнее, что хочешь сделать или что сейчас не получается. Например: «как пройти верификацию», «как пополнить баланс», «как вывести деньги», «как создать сделку» или пришли текст ошибки.",
        conf,
        "unknown",
    )



async def api_support_ai_status(request: web.Request):
    return web.json_response({"ok":True,"configured":True,"mode":"local_only","model":"playerok-local-support-v8","external_ai":False})

async def api_support_ai(request: web.Request):
    try: body=await request.json()
    except Exception: return web.json_response({"error":"invalid_json"},status=400)
    user_id=telegram_user_from_init_data(str(body.get("initData") or ""))
    if not user_id:return web.json_response({"error":"invalid_telegram_init_data","message":"Откройте Mini App из Telegram."},status=401)
    text=str(body.get("message") or "").strip()[:5000]
    attachments=body.get("attachments") or []
    if not text and attachments:text="прикрепил файл в поддержку"
    if not text:return web.json_response({"error":"empty_message"},status=400)
    try:
        reply,conf,intent=_local_support_reply(str(user_id),text)
        return web.json_response({"ok":True,"reply":reply,"mode":"local_only","intent":intent,"confidence":round(float(conf or 0),3),"external_ai":False})
    except Exception:
        log.exception("local support failed")
        return web.json_response({"ok":True,"reply":"Не удалось разобрать сообщение. Напиши вопрос короче или пришли текст ошибки.","mode":"local_error_fallback","external_ai":False})


def _support_self_test()->dict:
    tests={
      "как получить верификацию":"verify_how","как пройти верифку":"verify_how","для чего нужна верификация":"verify_why","что дает верификация":"verify_why",
      "что такое верификация":"verify_what","есть ли у меня верификация":"verify_status","сколько надо для верификации":"verify_amount","можно 999":"verify_999",
      "можно 500 stars":"verify_stars","можно криптой верификацию":"verify_crypto","деньги сгорят":"verify_money","как получить 2 уровень":"verify_level2",
      "как пополнить баланс":"topup_how","какие способы пополнения":"topup_methods","пополнил но деньги не пришли":"topup_missing","как пополнить stars":"topup_stars","как пополнить криптой":"topup_crypto",
      "как вывести баланс":"withdraw_how","почему не могу вывести":"withdraw_problem","минимальный вывод":"withdraw_min","куда можно вывести":"withdraw_methods","сколько ждать вывод":"withdraw_pending",
      "как создать сделку":"deal_create","как сделать гарант":"deal_create","какую роль выбрать":"deal_role","пользователь не найден":"deal_target_missing","какая комиссия":"deal_commission","как проходит сделка":"deal_flow","что такое резерв":"deal_reserve","как открыть спор":"deal_dispute",
      "как принять сделку":"deal_accept","как отклонить сделку":"deal_decline","какие валюты в сделке":"deal_currency","где чат сделки":"deal_chat","как найти сделку":"deal_search",
      "история платежей":"payment_history","что значит платеж в обработке":"payment_status","что показывает профиль":"profile_general","как повысить уровень":"profile_level","что такое рейтинг":"profile_rating",
      "как изменить имя":"profile_personal","как привязать номер":"profile_phone","меня забанили":"profile_banned","где уведомления":"notifications","как отключить уведомления":"notification_settings",
      "как оставить отзыв":"review_leave","почему не могу оставить отзыв":"review_limit","пригласить друга":"referral_general","правила сервиса":"rules","политика конфиденциальности":"privacy","как не попасть на мошенника":"security",
      "можно перевести напрямую":"direct_payment","mini app не открывается":"app_open","данные не обновились":"app_refresh","что ты умеешь":"support_scope","как работает сервис":"service_how"
    }
    result={};ok=0
    for q,exp in tests.items():
        got,score=_support_best_intent(q,""); passed=(got==exp); ok+=int(passed)
        result[q]={"expected":exp,"got":got,"score":round(float(score),3),"ok":passed}
    result["_summary"]={"passed":ok,"total":len(tests)}
    return result

# ============================================================
# 30.1. SUPPORT: SMART LOCAL LAYER v9
# СТАВИТЬ СРАЗУ НИЖЕ:
# # 30. SUPPORT: FULL LOCAL KNOWLEDGE ENGINE v8 — USER FRIENDLY
# ============================================================
#
# Этот блок НЕ заменяет v8, а расширяет его.
# Он использует уже существующие функции v8:
#   _support_norm, _support_has, _support_server_context,
#   _support_best_intent, _core_reply, _deal_role, _deal_next,
#   _m, db_get_json, db_set_json, telegram_user_from_init_data.
#
# Что добавляет v9:
# - реальную память диалога;
# - понимание коротких продолжений: "а stars?", "а 999?", "а потом?";
# - несколько вопросов в одном сообщении;
# - извлечение сумм и расчёт комиссии;
# - диагностику вывода по состоянию аккаунта;
# - диагностику верификации;
# - диагностику текущей сделки;
# - более разговорные формулировки;
# - ответы на "что мне делать сейчас?";
# - обработку "я продавец / я покупатель";
# - защиту от повторения одного и того же ответа;
# - локальные подсказки без внешнего AI.
#
# ВАЖНО:
# Пользовательские ответы не содержат названий файлов,
# внутренних переменных, API, backend/server terminology и т.п.
# ============================================================

import re
import time


SMART_SUPPORT_VERSION = "playerok-local-support-v9-smart"
SMART_SUPPORT_HISTORY_LIMIT = 30
SMART_SUPPORT_COMMISSION = 0.125
SMART_SUPPORT_VERIFY_RUB = 1000.0
SMART_SUPPORT_VERIFY_STARS = 500
SMART_SUPPORT_WITHDRAW_MIN = 100.0


# ------------------------------------------------------------
# 30.1.1. ПАМЯТЬ ДИАЛОГА
# ------------------------------------------------------------

def _smart_history_key(user_id: str) -> str:
    # Внутренняя память ИИ хранится отдельно от видимого чата поддержки.
    return f"deelo_support_memory_{user_id}"


def _smart_history(user_id: str) -> list:
    h = db_get_json(_smart_history_key(str(user_id)), []) or []
    return h[-SMART_SUPPORT_HISTORY_LIMIT:]


def _smart_history_add(user_id: str, role: str, text: str, intent: str = ""):
    if not text:
        return
    h = db_get_json(_smart_history_key(str(user_id)), []) or []
    h.append({
        "role": str(role),
        "text": str(text)[:6000],
        "intent": str(intent or ""),
        "ts": int(time.time()),
    })
    h = h[-SMART_SUPPORT_HISTORY_LIMIT:]
    db_set_json(_smart_history_key(str(user_id)), h)


def _smart_last_intent(user_id: str) -> str:
    for item in reversed(_smart_history(user_id)):
        if item.get("role") == "assistant" and item.get("intent"):
            return str(item["intent"])
    return ""


def _smart_last_user_text(user_id: str) -> str:
    for item in reversed(_smart_history(user_id)):
        if item.get("role") == "user" and item.get("text"):
            return str(item["text"])
    return ""


def _smart_topic_from_intent(intent: str) -> str:
    i = str(intent or "")
    if i.startswith("verify"): return "verification"
    if i.startswith("topup"): return "topup"
    if i.startswith("withdraw"): return "withdraw"
    if i.startswith("deal"): return "deal"
    if i.startswith("payment"): return "payment"
    if i.startswith("profile"): return "profile"
    if i.startswith("review"): return "review"
    if i.startswith("notification"): return "notifications"
    if i.startswith("referral"): return "referral"
    if i in {"security", "direct_payment"}: return "security"
    return ""


# ------------------------------------------------------------
# 30.1.2. РАЗБОР СООБЩЕНИЯ
# ------------------------------------------------------------

def _smart_amounts(text: str) -> list[float]:
    """Достаёт денежные суммы из обычной фразы."""
    t = str(text or "").lower().replace(",", ".")
    result = []

    # "2к", "2.5к"
    for x in re.findall(r"(?<!\w)(\d+(?:\.\d+)?)\s*[кk](?!\w)", t):
        try:
            result.append(float(x) * 1000)
        except Exception:
            pass

    # обычные числа
    for x in re.findall(r"(?<![\w.])(\d{1,7}(?:\.\d{1,2})?)(?![\w.])", t):
        try:
            n = float(x)
            if n not in result:
                result.append(n)
        except Exception:
            pass

    return result


def _smart_first_amount(text: str):
    vals = _smart_amounts(text)
    return vals[0] if vals else None


def _smart_is_short_followup(text: str) -> bool:
    t = _support_norm(text)
    words = t.split()
    if len(words) <= 5:
        return True
    return any(t.startswith(x) for x in (
        "а если ", "а потом", "а stars", "а крипт", "а карт",
        "а 999", "а 500", "а можно", "а как", "а где",
        "и что", "что дальше", "потом что",
    ))


def _smart_role_from_text(text: str) -> str:
    t = _support_norm(text)
    if _support_has(t, "продавец") or any(x in t for x in ("я продаю", "я продал", "товар передал")):
        return "sell"
    if _support_has(t, "покупатель") or any(x in t for x in ("я покупаю", "я купил", "я оплатил")):
        return "buy"
    return ""


def _smart_question_flags(text: str) -> dict:
    t = _support_norm(text)
    return {
        "verification": _support_has(t, "верификация", "значок", "уровень"),
        "topup": _support_has(t, "пополнить", "пополнение", "зачислить"),
        "withdraw": _support_has(t, "вывести", "вывод"),
        "deal": _support_has(t, "сделка", "гарант", "резерв"),
        "commission": _support_has(t, "комиссия"),
        "stars": "stars" in t or "⭐" in str(text),
        "crypto": any(x in t for x in ("криптовалюта", "usdt", "ton", "crypto")),
        "card": _support_has(t, "карта", "сбп", "юмани"),
        "problem": any(x in t for x in (
            "не могу", "не получается", "не работает", "ошибка",
            "не приш", "не появ", "пропал", "отклони", "завис",
        )),
        "next": any(x in t for x in (
            "что дальше", "что теперь", "что делать", "дальше что",
            "потом что", "следующий шаг", "куда дальше",
        )),
    }


def _smart_resolve_followup(user_id: str, text: str) -> str:
    """
    Добавляет смысл предыдущей темы к короткой реплике.
    Возвращает текст только для классификации; пользователю он не показывается.
    """
    t = _support_norm(text)
    if not _smart_is_short_followup(t):
        return t

    last_intent = _smart_last_intent(user_id)
    topic = _smart_topic_from_intent(last_intent)
    if not topic:
        return t

    if topic == "verification":
        if "stars" in t: return f"верификация stars {t}"
        if "999" in t: return f"999 для верификации {t}"
        if "500" in t and "stars" not in t: return f"500 рублей для верификации {t}"
        if any(x in t for x in ("криптовалюта", "usdt", "ton")): return f"верификация криптовалютой {t}"
        if _support_has(t, "деньги", "останутся", "сгорят"): return f"деньги после верификации {t}"
        if _support_has(t, "вывести", "вывод"): return f"вывод после верификации {t}"
        if _support_has(t, "потом", "дальше", "после"): return f"что после верификации {t}"
        return f"верификация {t}"

    if topic == "withdraw":
        if _support_has(t, "сколько", "минимум"): return f"минимальный вывод {t}"
        if _support_has(t, "куда", "способ", "карта") or "stars" in t: return f"способы вывода {t}"
        if _support_has(t, "ждать", "когда"): return f"сколько ждать вывод {t}"
        if _support_has(t, "верификация", "значок"): return f"верификация для вывода {t}"
        return f"вывод {t}"

    if topic == "topup":
        if "stars" in t: return f"пополнение stars {t}"
        if any(x in t for x in ("криптовалюта", "usdt", "ton")): return f"пополнение криптовалютой {t}"
        if _support_has(t, "минимум", "сколько"): return f"минимальное пополнение {t}"
        return f"пополнение баланса {t}"

    if topic == "deal":
        if _support_has(t, "комиссия", "сколько"): return f"комиссия сделки {t}"
        if _support_has(t, "дальше", "потом", "делать"): return f"что дальше по сделке {t}"
        if _support_has(t, "спор", "обманули"): return f"спор по сделке {t}"
        return f"сделка {t}"

    return t


# ------------------------------------------------------------
# 30.1.3. ПОИСК ТЕКУЩЕЙ СДЕЛКИ
# ------------------------------------------------------------

def _smart_active_deal(ctx: dict):
    deals = ctx.get("recent_deals") or []
    active_statuses = {
        "pending_accept", "accepted", "awaiting_payment",
        "paid", "reserved", "transferred", "delivered", "dispute"
    }
    for d in reversed(deals):
        if str(d.get("status") or "").lower() in active_statuses:
            return d
    return deals[-1] if len(deals) == 1 else None


def _smart_find_deal(ctx: dict, text: str):
    t = _support_norm(text)
    for d in reversed(ctx.get("recent_deals") or []):
        code = str(d.get("code") or "").lower()
        did = str(d.get("id") or "").lower()
        if code and code in t:
            return d
        if did and did in t:
            return d
    return _smart_active_deal(ctx)


def _smart_status_name(status: str) -> str:
    return {
        "pending_accept": "ожидает подтверждения второго участника",
        "accepted": "ожидает оплаты",
        "awaiting_payment": "ожидает оплаты",
        "paid": "оплачена, деньги в резерве",
        "reserved": "деньги в резерве",
        "transferred": "товар отмечен как переданный",
        "delivered": "товар отмечен как переданный",
        "completed": "завершена",
        "done": "завершена",
        "finished": "завершена",
        "declined": "отклонена",
        "dispute": "открыт спор",
    }.get(str(status or "").lower(), "статус обновляется")


def _smart_deal_answer(user_id: str, ctx: dict, text: str):
    t = _support_norm(text)
    d = _smart_find_deal(ctx, t)
    if not d:
        return None

    status = str(d.get("status") or "").lower()
    role = _deal_role(d, str(user_id))
    code = d.get("code") or d.get("id") or ""
    prefix = f"По сделке #{code} " if code else "По этой сделке "

    if d.get("disputeOpen") or status == "dispute":
        return (
            prefix + "уже открыт спор. Пока ничего не подтверждай и не завершай вручную. "
            "Опиши проблему в чате сделки и дождись подключения администратора."
        )

    if any(x in t for x in ("какой статус", "статус сделки", "что со сделкой")):
        return prefix + f"сейчас статус: «{_smart_status_name(status)}». " + _deal_next(d, str(user_id))

    if any(x in t for x in ("что дальше", "что теперь", "что делать", "следующий шаг", "дальше что")):
        return prefix + _deal_next(d, str(user_id))

    if "я продавец" in t or role == "sell":
        if status in {"accepted", "awaiting_payment", "pending_accept"}:
            return (
                prefix + "пока не нужно передавать товар. Сначала дождись, когда покупатель оплатит "
                "и в сделке появится, что деньги находятся в резерве."
            )
        if status in {"paid", "reserved"}:
            return (
                prefix + "оплата уже в резерве. Теперь можешь передать товар или данные покупателю. "
                "После фактической передачи нажми «Товар был передан»."
            )
        if status in {"transferred", "delivered"}:
            return (
                prefix + "товар уже отмечен как переданный. Теперь остаётся дождаться, "
                "пока покупатель проверит его и подтвердит получение."
            )

    if "я покупатель" in t or role == "buy":
        if status in {"accepted", "awaiting_payment"}:
            return (
                prefix + "твой следующий шаг — оплатить её с баланса. "
                "После оплаты деньги будут удерживаться в резерве до завершения сделки."
            )
        if status in {"paid", "reserved"}:
            return (
                prefix + "ты уже оплатил, деньги находятся в резерве. "
                "Теперь дождись передачи товара продавцом."
            )
        if status in {"transferred", "delivered"}:
            return (
                prefix + "продавец отметил товар как переданный. Сначала всё проверь. "
                "Если всё соответствует условиям — нажми «Товар получен». "
                "Если есть проблема — не подтверждай получение и открой спор."
            )

    return None


# ------------------------------------------------------------
# 30.1.4. УМНАЯ ДИАГНОСТИКА
# ------------------------------------------------------------

def _smart_verify_diagnostic(ctx: dict, text: str):
    t = _support_norm(text)
    u = ctx.get("user") or {}
    verified = bool(u.get("verified"))
    level = int(u.get("level") or 0)
    balance = float(u.get("balance") or 0)

    if any(x in t for x in ("есть ли у меня", "прошел ли я", "какой у меня уровень", "моя верификация")):
        if verified or level >= 1:
            return (
                f"Да, верификация у тебя уже есть — {max(level, 1)} уровень. "
                f"Баланс сейчас {_m(balance)} ₽."
            )
        return (
            f"Сейчас верификации у тебя нет, уровень 0. Баланс — {_m(balance)} ₽. "
            "Чтобы получить 1 уровень, пополни баланс одним банковским платежом от 1 000 ₽ "
            "или через Telegram Stars от 500 ⭐."
        )

    if any(x in t for x in ("не появилась", "нет значка", "не дали", "не получил")):
        if verified or level >= 1:
            return f"Всё уже в порядке: у тебя есть верификация, текущий уровень — {max(level, 1)}."
        return (
            "Если подходящее пополнение уже успешно завершилось, а значка всё ещё нет, "
            "сначала полностью закрой Mini App и открой его заново. "
            "Если ничего не изменится, напиши способ оплаты, сумму и примерно когда платил."
        )

    return None


def _smart_withdraw_diagnostic(ctx: dict, text: str):
    t = _support_norm(text)
    u = ctx.get("user") or {}
    verified = bool(u.get("verified"))
    level = int(u.get("level") or 0)
    balance = float(u.get("balance") or 0)

    if not any(x in t for x in (
        "вывести", "вывод", "снять деньги", "забрать деньги",
        "не могу вывести", "не получается вывести"
    )):
        return None

    if not verified or level < 1:
        return (
            "Сейчас вывести деньги не получится, потому что у тебя ещё нет верификации. "
            "Сначала получи 1 уровень: пополни баланс одним банковским платежом от 1 000 ₽ "
            "или через Telegram Stars от 500 ⭐. Деньги останутся на балансе. "
            "После появления значка снова открой «Кошелёк» → «Вывести»."
        )

    if balance < SMART_SUPPORT_WITHDRAW_MIN:
        return (
            f"Верификация у тебя есть, но сейчас на балансе {_m(balance)} ₽. "
            f"Минимальная сумма вывода — {_m(SMART_SUPPORT_WITHDRAW_MIN)} ₽, "
            "поэтому сначала нужно, чтобы на балансе было хотя бы 100 ₽."
        )

    if any(x in t for x in ("не могу", "не получается", "ошибка", "почему")):
        return (
            f"Верификация у тебя есть и баланс {_m(balance)} ₽ — по этим двум условиям вывод доступен. "
            "Открой «Кошелёк» → «Вывести», введи сумму от 100 ₽ и проверь реквизиты. "
            "Если появляется ошибка, пришли её текст — по нему можно будет точнее понять причину."
        )

    return (
        f"У тебя есть верификация, баланс сейчас {_m(balance)} ₽. "
        "Для вывода открой «Кошелёк» → «Вывести», укажи сумму от 100 ₽, "
        "выбери способ и заполни реквизиты. После отправки заявка появится в обработке."
    )


def _smart_topup_diagnostic(ctx: dict, text: str):
    t = _support_norm(text)
    u = ctx.get("user") or {}
    balance = float(u.get("balance") or 0)

    if not any(x in t for x in ("пополнил", "оплатил", "деньги не приш", "баланс не")):
        return None

    if any(x in t for x in ("не приш", "не попол", "не зачисл", "не обнов")):
        return (
            f"Сейчас у тебя отображается {_m(balance)} ₽. "
            "Если деньги уже списались, но баланс не изменился, открой историю платежей и проверь статус операции. "
            "Если платёж отмечен как завершённый, полностью перезапусти Mini App. "
            "Если сумма всё равно не появилась — напиши способ оплаты, сумму и время платежа."
        )

    return None


# ------------------------------------------------------------
# 30.1.5. ЛОКАЛЬНЫЙ КАЛЬКУЛЯТОР СДЕЛКИ
# ------------------------------------------------------------

def _smart_calculator(text: str):
    t = _support_norm(text)
    amounts = _smart_amounts(t)
    if not amounts:
        return None

    amount = float(amounts[0])
    if amount <= 0:
        return None

    commission = amount * SMART_SUPPORT_COMMISSION
    buyer = amount + commission

    # "хочу получить 5000", "продавцу 5000"
    if (
        _support_has(t, "комиссия")
        or "сколько заплатит покупатель" in t
        or "сколько покупатель" in t
        or "сколько будет с комиссией" in t
        or "хочу получить" in t
        or "продавцу" in t
    ):
        return (
            f"Если продавец должен получить {_m(amount)} ₽, комиссия составит {_m(commission)} ₽. "
            f"Покупатель заплатит {_m(buyer)} ₽."
        )

    return None


# ------------------------------------------------------------
# 30.1.6. СОСТАВНЫЕ ВОПРОСЫ
# ------------------------------------------------------------

def _smart_multi_answer(user_id: str, ctx: dict, text: str):
    """
    Обрабатывает наиболее полезные сочетания двух тем.
    Не пытаемся разбить абсолютно любое предложение — только сценарии,
    где пользователю действительно нужен объединённый ответ.
    """
    t = _support_norm(text)
    flags = _smart_question_flags(t)

    if flags["verification"] and flags["withdraw"]:
        u = ctx.get("user") or {}
        if bool(u.get("verified")) or int(u.get("level") or 0) >= 1:
            return (
                "Верификация у тебя уже есть, поэтому заново проходить её не нужно. "
                "Для вывода открой «Кошелёк» → «Вывести», укажи сумму от 100 ₽, "
                "выбери способ и заполни реквизиты."
            )
        return (
            "Сначала нужно получить верификацию, а уже после этого подавать заявку на вывод. "
            "Для 1 уровня пополни баланс одним банковским платежом от 1 000 ₽ "
            "или через Telegram Stars от 500 ⭐ — деньги останутся на балансе. "
            "Когда значок появится, открой «Кошелёк» → «Вывести» и укажи сумму от 100 ₽."
        )

    if flags["topup"] and flags["verification"]:
        return (
            "Если хочешь одновременно пополнить баланс и получить верификацию, "
            "сделай один банковский платёж от 1 000 ₽ или используй Telegram Stars от 500 ⭐. "
            "Пополнение останется на балансе, а после успешной оплаты появится 1 уровень."
        )

    if flags["deal"] and flags["commission"]:
        calc = _smart_calculator(t)
        if calc:
            return calc
        return (
            "В сделке указывается сумма, которую должен получить продавец. "
            "Комиссия сервиса — 12,5% и добавляется к сумме покупателя сверху. "
            "Например, при сумме продавца 1 000 ₽ покупатель заплатит 1 125 ₽."
        )

    return None


# ------------------------------------------------------------
# 30.1.7. ЖИВЫЕ СПЕЦИАЛЬНЫЕ ФРАЗЫ
# ------------------------------------------------------------

def _smart_natural_special(user_id: str, ctx: dict, text: str):
    t = _support_norm(text)

    if any(x in t for x in (
        "с чего начать", "я первый раз", "я новичок", "ничего не понимаю",
        "как пользоваться приложением"
    )):
        return (
            "Если ты здесь впервые, начни с профиля и кошелька. "
            "Если планируешь выводить деньги — сначала получи верификацию. "
            "Для безопасной покупки или продажи создавай сделку через «Новая сделка»: "
            "так деньги покупателя будут находиться в резерве до подтверждения получения."
        )

    if any(x in t for x in ("меня обманули", "кинули", "мошенник", "скамер")):
        d = _smart_active_deal(ctx)
        if d:
            return (
                "Если проблема связана с текущей сделкой, не подтверждай получение и не завершай её. "
                "Открой спор и подробно опиши ситуацию в чате сделки. "
                "Не продолжай переводить деньги напрямую второй стороне."
            )
        return (
            "Не отправляй дополнительные деньги и не передавай коды или данные аккаунта. "
            "Если проблема относится к сделке в приложении — открой её и создай спор. "
            "Если сделки в приложении не было, не совершай новые переводы этому человеку."
        )

    if any(x in t for x in ("покупатель не подтверждает", "покуп не подтверждает")):
        return (
            "Если товар уже реально передан и ты нажал «Товар был передан», "
            "не проси покупателя переводить деньги отдельно. Напиши ему в чате сделки и дождись подтверждения. "
            "Если возник спор по факту передачи — используй кнопку открытия спора."
        )

    if any(x in t for x in ("продавец не передает", "продавец пропал", "продавец не отвечает")):
        return (
            "Если сделка уже оплачена, не подтверждай получение товара. "
            "Напиши продавцу в чате сделки. Если товар не передаётся или продавец пропал — открой спор."
        )

    if any(x in t for x in ("случайно нажал товар передан", "случайно отметил товар")):
        return (
            "Если ты случайно отметил товар как переданный, сразу напиши об этом в чате сделки. "
            "Если из-за этого возник риск неправильного завершения — открой спор и не проси покупателя подтверждать получение."
        )

    if any(x in t for x in ("можно напрямую", "перевести напрямую", "скинуть на карту продавцу")):
        return (
            "Лучше не переводить деньги напрямую второй стороне. "
            "При оплате через сделку деньги остаются в резерве до проверки товара, "
            "а при проблеме можно открыть спор."
        )

    return None


# ------------------------------------------------------------
# 30.1.8. АНТИ-ПОВТОР
# ------------------------------------------------------------

def _smart_avoid_repeat(user_id: str, reply: str) -> str:
    """
    Если один и тот же ответ уже только что выдавался,
    слегка меняем концовку, чтобы чат не выглядел зацикленным.
    """
    last_assistant = ""
    for item in reversed(_smart_history(user_id)):
        if item.get("role") == "assistant":
            last_assistant = str(item.get("text") or "")
            break

    if not last_assistant or last_assistant != reply:
        return reply

    return (
        reply
        + "\n\nЕсли ты уже сделал это и проблема осталась, напиши, "
          "на каком именно шаге остановился и что сейчас отображается на экране."
    )


# ------------------------------------------------------------
# 30.1.9. ГЛАВНЫЙ SMART ROUTER
# ------------------------------------------------------------

def _smart_local_support_reply(user_id: str, text: str) -> tuple[str, float, str]:
    raw = str(text or "").strip()
    t = _support_norm(raw)
    ctx = _support_server_context(str(user_id))

    if not t:
        return "Напиши, что хочешь сделать — помогу разобраться.", 1.0, "empty"

    # Приветствия.
    if t in {
        "привет", "здравствуйте", "здорово", "здарова", "хай",
        "hello", "добрый день", "добрый вечер"
    }:
        return (
            "Привет! Помогу разобраться с верификацией, балансом, выводом, "
            "сделками, оплатой, комиссией, резервом, спором и другими функциями приложения.",
            1.0,
            "hello",
        )

    # Представление помощника / имя.
    if (
        t in {"кто ты", "ты кто", "как тебя зовут", "как твое имя", "как твоё имя", "твое имя", "твоё имя"}
        or "как тебя зовут" in t
        or "как его зовут" in t
        or "кто такой помощник" in t
    ):
        return (
            "Я помощник Playerok 🤝 Помогаю с верификацией, пополнением, выводом, "
            "сделками и другими вопросами по сервису.",
            1.0,
            "who_are_you",
        )

    # 1. Самые естественные сценарии.
    special = _smart_natural_special(str(user_id), ctx, t)
    if special:
        return special, 1.0, "smart_special"

    # 2. Составные вопросы.
    multi = _smart_multi_answer(str(user_id), ctx, t)
    if multi:
        return multi, 1.0, "smart_multi"

    # 3. Расчёт комиссии.
    calc = _smart_calculator(t)
    if calc:
        return calc, 1.0, "deal_calculator"

    # 4. Диагностика конкретной сделки.
    if _support_has(t, "сделка", "продавец", "покупатель", "товар", "резерв"):
        deal_reply = _smart_deal_answer(str(user_id), ctx, t)
        if deal_reply and any(x in t for x in (
            "что дальше", "что теперь", "что делать", "статус",
            "я продавец", "я покупатель", "оплатил", "передал", "получил"
        )):
            return deal_reply, 1.0, "deal_scenario"

    # 5. Диагностика вывода.
    withdraw_reply = _smart_withdraw_diagnostic(ctx, t)
    if withdraw_reply and (
        _support_has(t, "вывести", "вывод")
        or any(x in t for x in ("не могу вывести", "не получается вывести"))
    ):
        return withdraw_reply, 1.0, "withdraw_scenario"

    # 6. Диагностика верификации.
    verify_reply = _smart_verify_diagnostic(ctx, t)
    if verify_reply:
        return verify_reply, 1.0, "verify_scenario"

    # 7. Диагностика пополнения.
    topup_reply = _smart_topup_diagnostic(ctx, t)
    if topup_reply:
        return topup_reply, 1.0, "topup_scenario"

    # 8. Короткое продолжение предыдущего разговора.
    classify_text = _smart_resolve_followup(str(user_id), t)
    intent, confidence = _support_best_intent(classify_text, _smart_last_user_text(str(user_id)))

    if confidence >= 0.43:
        reply = _core_reply(intent, str(user_id), ctx)
        if reply:
            return reply, confidence, intent

    # 9. Последняя попытка — старая v8-база, но уже с расширенным контекстом.
    try:
        old_reply, old_conf, old_intent = _local_support_reply_v8(str(user_id), classify_text)
        if old_reply and old_intent != "unknown" and float(old_conf or 0) >= 0.40:
            return old_reply, float(old_conf or 0), old_intent
    except Exception:
        pass

    # 10. Уточнение вместо бессмысленного угадывания.
    flags = _smart_question_flags(t)

    if flags["problem"]:
        return (
            "Понял, что что-то не работает, но пока не хватает одной детали. "
            "Что именно не получается: верификация, пополнение, вывод или сделка? "
            "Можешь просто написать, что нажимаешь и что появляется после этого.",
            0.25,
            "clarify_problem",
        )

    return (
        "Не до конца понял вопрос. Напиши его чуть конкретнее — например: "
        "«как пройти верификацию», «пополнил, но деньги не пришли», "
        "«не могу вывести», «как создать сделку» или «я продавец, покупатель оплатил — что дальше?».",
        0.20,
        "clarify",
    )


# ------------------------------------------------------------
# 30.1.10. СОХРАНЯЕМ v8 И ПЕРЕКЛЮЧАЕМСЯ НА v9
# ------------------------------------------------------------

# Сохраняем старый роутер v8, чтобы v9 мог использовать его как дополнительную базу.
_local_support_reply_v8 = _local_support_reply


def _local_support_reply(user_id: str, text: str) -> tuple[str, float, str]:
    reply, confidence, intent = _smart_local_support_reply(str(user_id), text)
    reply = str(reply or "").replace(_SUPPORT_CARD_MARKER, "").replace(_SUPPORT_CARD_SENTINEL, "").strip()
    return _smart_avoid_repeat(str(user_id), reply), confidence, intent


# ------------------------------------------------------------
# 30.1.11. API — ПОЛНОСТЬЮ ЛОКАЛЬНЫЙ
# Имена маршрутов оставлены прежними для совместимости index.html.
# ------------------------------------------------------------

async def api_support_ai_status(request: web.Request):
    return web.json_response({
        "ok": True,
        "configured": True,
        "mode": "smart_local_only",
        "model": SMART_SUPPORT_VERSION,
        "external_ai": False,
        "memory": True,
        "scenario_engine": True,
        "calculator": True,
    })


async def api_support_ai(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({
            "error": "invalid_json",
            "message": "Не удалось прочитать сообщение."
        }, status=400)

    user_id = telegram_user_from_init_data(str(body.get("initData") or ""))
    if not user_id:
        return web.json_response({
            "error": "invalid_telegram_init_data",
            "message": "Открой Mini App из Telegram."
        }, status=401)

    user_id = str(user_id)
    text = str(body.get("message") or "").strip()[:5000]
    attachments = body.get("attachments") or []

    if not text and attachments:
        text = "прикрепил файл в поддержку"

    if not text:
        return web.json_response({"error": "empty_message"}, status=400)

    try:
        # Сохраняем вопрос ДО ответа, но роутеру нужен предыдущий контекст,
        # поэтому сначала получаем ответ, затем пишем обе реплики в историю.
        reply, confidence, intent = _local_support_reply(user_id, text)

        _smart_history_add(user_id, "user", text, "")
        _smart_history_add(user_id, "assistant", reply, intent)

        return web.json_response({
            "ok": True,
            "reply": reply,
            "mode": "smart_local_only",
            "intent": intent,
            "confidence": round(float(confidence or 0), 3),
            "external_ai": False,
        })

    except Exception:
        log.exception("smart local support failed")
        return web.json_response({
            "ok": True,
            "reply": (
                "Не получилось разобрать сообщение. Напиши вопрос немного короче "
                "или опиши, что именно ты хотел сделать и что произошло."
            ),
            "mode": "local_error_fallback",
            "external_ai": False,
        })


# ------------------------------------------------------------
# 30.1.12. ДОПОЛНИТЕЛЬНЫЕ ТЕСТЫ v9
# ------------------------------------------------------------

def _smart_support_self_test() -> dict:
    """
    Тесты не трогают реальные данные пользователей.
    Здесь проверяется разбор текста, follow-up и калькулятор.
    """
    checks = {}

    # суммы
    checks["amount_1000"] = _smart_first_amount("комиссия с 1000") == 1000
    checks["amount_2k"] = _smart_first_amount("комиссия с 2к") == 2000

    # роли
    checks["role_seller"] = _smart_role_from_text("я продавец что делать") == "sell"
    checks["role_buyer"] = _smart_role_from_text("я покупатель и уже оплатил") == "buy"

    # flags
    f = _smart_question_flags("как пройти верификацию и потом вывести деньги")
    checks["multi_verify_withdraw"] = bool(f["verification"] and f["withdraw"])

    f = _smart_question_flags("как создать сделку и какая комиссия")
    checks["multi_deal_commission"] = bool(f["deal"] and f["commission"])

    # calculator
    calc = _smart_calculator("если продавцу 1000 сколько заплатит покупатель")
    checks["calculator"] = bool(calc and "1 125" in calc)

    checks["_summary"] = {
        "passed": sum(1 for k, v in checks.items() if k != "_summary" and v),
        "total": sum(1 for k in checks if k != "_summary"),
    }
    return checks

# ============================================================
# 30.2. SUPPORT: ADMIN MANUAL MODE v1
# СТАВИТЬ НИЖЕ v8 / v9, ДО create_app()
# ============================================================
#
# Режимы поддержки для каждого пользователя:
#   auto   — отвечает локальная поддержка;
#   manual — локальная поддержка молчит, отвечает администратор.
#
# Переключатель хранится отдельно для каждого пользователя.
# Администратор включает/выключает режим из чата поддержки.
# ============================================================

def _support_manual_key(user_id: str) -> str:
    return f"deelo_support_manual_{user_id}"


def _support_is_manual(user_id: str) -> bool:
    state = db_get_json(_support_manual_key(str(user_id)), {}) or {}
    return bool(state.get("enabled"))


def _support_set_manual(user_id: str, enabled: bool, admin_id: str = ""):
    db_set_json(_support_manual_key(str(user_id)), {
        "enabled": bool(enabled),
        "adminId": str(admin_id or ""),
        "updatedAt": int(time.time()),
    })


def _support_append_message(user_id: str, message: dict):
    key = f"deelo_support_{user_id}"
    history = db_get_json(key, []) or []
    history.append(message)
    history = history[-80:]
    db_set_json(key, history)


async def api_support_manual_mode(request: web.Request):
    """
    Администратор включает/выключает ручной режим конкретному пользователю.
    """
    admin_id = telegram_user_from_init_data(
        request.headers.get("X-Telegram-Init-Data", "")
    )

    # На случай если initData передаётся в body.
    try:
        body = await request.json()
    except Exception:
        body = {}

    if not admin_id:
        admin_id = telegram_user_from_init_data(str(body.get("initData") or ""))

    if not admin_id or int(admin_id) not in ADMIN_IDS:
        return web.json_response(
            {"ok": False, "error": "forbidden", "message": "Нет доступа."},
            status=403
        )

    target_id = str(body.get("userId") or "").strip()
    enabled = bool(body.get("enabled"))

    if not target_id.isdigit():
        return web.json_response(
            {"ok": False, "error": "invalid_user", "message": "Некорректный пользователь."},
            status=400
        )

    _support_set_manual(target_id, enabled, str(admin_id))

    # Системное сообщение в самом диалоге.
    _support_append_message(target_id, {
        "role": "assistant",
        "text": (
            "К диалогу подключился оператор поддержки. Теперь на сообщения будет отвечать человек."
            if enabled else
            "Ручной режим завершён. Дальше снова будет отвечать автоматическая поддержка."
        ),
        "time": int(time.time() * 1000),
        "system": True,
    })

    return web.json_response({
        "ok": True,
        "userId": target_id,
        "manual": enabled,
    })


async def api_support_manual_status(request: web.Request):
    """
    Статус ручного режима.
    Администратор может смотреть любого пользователя.
    Обычный пользователь — только самого себя.
    """
    init_data = (
        request.headers.get("X-Telegram-Init-Data", "")
        or request.query.get("initData", "")
    )
    caller_id = telegram_user_from_init_data(str(init_data or ""))

    if not caller_id:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    target_id = str(request.query.get("userId") or caller_id)

    if str(caller_id) != target_id and int(caller_id) not in ADMIN_IDS:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)

    return web.json_response({
        "ok": True,
        "userId": target_id,
        "manual": _support_is_manual(target_id),
    })


# Сохраняем текущую умную локальную функцию ответа (v8 или v9).
_api_support_auto_reply = api_support_ai


async def api_support_ai(request: web.Request):
    """
    Главная точка поддержки:
    - manual OFF -> обычная локальная поддержка;
    - manual ON  -> автоматический ответ НЕ создаётся.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({
            "error": "invalid_json",
            "message": "Не удалось прочитать сообщение."
        }, status=400)

    user_id = telegram_user_from_init_data(str(body.get("initData") or ""))
    if not user_id:
        return web.json_response({
            "error": "invalid_telegram_init_data",
            "message": "Открой Mini App из Telegram."
        }, status=401)

    user_id = str(user_id)

    if _support_is_manual(user_id):
        # ВАЖНО: сообщение пользователя уже сохраняется index.html.
        # Здесь просто запрещаем локальному помощнику отвечать.
        return web.json_response({
            "ok": True,
            "manual": True,
            "skipAutoReply": True,
            "reply": "",
            "mode": "human_support",
            "external_ai": False,
        })

    # request body уже был прочитан, поэтому создаём клон request нельзя.
    # Вызываем локальный движок напрямую.
    text = str(body.get("message") or "").strip()[:5000]
    attachments = body.get("attachments") or []

    if not text and attachments:
        text = "прикрепил файл в поддержку"

    if not text:
        return web.json_response({"error": "empty_message"}, status=400)

    try:
        # Если установлен v9 — будет вызвана его переопределённая функция.
        reply, confidence, intent = _local_support_reply(user_id, text)

        # v9 сам сохранял историю внутри своего api_support_ai.
        # Здесь сохраняем только если доступны v9-функции памяти.
        if "_smart_history_add" in globals():
            try:
                _smart_history_add(user_id, "user", text, "")
                _smart_history_add(user_id, "assistant", reply, intent)
            except Exception:
                pass

        return web.json_response({
            "ok": True,
            "reply": reply or "",
            "manual": False,
            "skipAutoReply": False,
            "mode": "smart_local_only" if "_smart_local_support_reply" in globals() else "local_only",
            "intent": intent,
            "confidence": round(float(confidence or 0), 3),
            "external_ai": False,
        })

    except Exception:
        log.exception("support reply failed")
        return web.json_response({
            "ok": True,
            "reply": "Не получилось обработать сообщение. Попробуй отправить его ещё раз.",
            "manual": False,
            "skipAutoReply": False,
            "mode": "local_error_fallback",
            "external_ai": False,
        })



# ============================================================
# 31. TOME: СОЗДАНИЕ ПЛАТЕЖА
# ============================================================
async def api_tome_create_payment(request: web.Request):
    """Create a Tome payment for the authenticated Telegram Mini App user."""
    if not TOME_SHOP_ID or not TOME_SECRET_KEY:
        return web.json_response({"error": "tome_not_configured"}, status=503)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    init_data = str(body.get("initData") or "")
    user_id = telegram_user_from_init_data(init_data)
    if not user_id:
        return web.json_response({"error": "invalid_telegram_init_data"}, status=401)

    try:
        amount = Decimal(str(body.get("amount"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return web.json_response({"error": "invalid_amount"}, status=400)

    if amount < Decimal("0.01"):
        return web.json_response({"error": "amount_too_small"}, status=400)
    if amount > Decimal("1000000.00"):
        return web.json_response({"error": "amount_too_large"}, status=400)

    user_rec = db_get_json(f"deelo_user_{user_id}")
    if not user_rec:
        return web.json_response({"error": "user_not_found"}, status=404)

    payment_payload = {
        "account_type": "merchant",
        "amount": {
            "value": f"{amount:.2f}",
            "currency": "RUB",
        },
        # customer намеренно не передаём:
        # на странице Tome пользователь сможет выбрать доступный способ оплаты.
        "confirmation": {
            "type": "redirect",
            "return_url": f"{WEBAPP_URL}#wallet",
        },
        "description": f"Пополнение баланса пользователя {user_id}",
        "metadata": {
            "user_id": str(user_id),
        },
    }

    try:
        timeout = __import__("aiohttp").ClientTimeout(total=20)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{TOME_API_URL}/payments",
                auth=BasicAuth(TOME_SHOP_ID, TOME_SECRET_KEY),
                headers={
                    "Idempotency-Key": str(uuid.uuid4()),
                    "Content-Type": "application/json",
                },
                json=payment_payload,
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    log.error("Tome create payment failed: status=%s body=%s", resp.status, data)
                    err = data.get("error") if isinstance(data, dict) else None
                    safe_code = err.get("code") if isinstance(err, dict) else None
                    safe_description = err.get("description") if isinstance(err, dict) else None
                    return web.json_response(
                        {
                            "error": "tome_api_error",
                            "tome_code": safe_code,
                            "tome_description": safe_description,
                        },
                        status=502,
                    )
    except Exception:
        log.exception("Tome create payment request failed")
        return web.json_response({"error": "tome_request_failed"}, status=502)

    confirmation_url = ((data.get("confirmation") or {}).get("confirmation_url"))
    payment_id = data.get("id")
    if not confirmation_url or not payment_id:
        log.error("Tome response has no payment URL/id: %s", data)
        return web.json_response({"error": "invalid_tome_response"}, status=502)

    return web.json_response({
        "ok": True,
        "paymentId": payment_id,
        "status": data.get("status"),
        "confirmationUrl": confirmation_url,
    })


# ============================================================
# 30. TOME: ПОДПИСЬ WEBHOOK И ЗАЧИСЛЕНИЕ
# ============================================================
def _tome_signature(payment_id: str) -> str:
    return hashlib.sha256((str(payment_id) + TOME_SECRET_KEY).encode("utf-8")).hexdigest()


def _credit_tome_payment(payment_obj: dict) -> tuple[bool, str]:
    payment_id = str(payment_obj.get("id") or "")
    metadata = payment_obj.get("metadata") or {}
    user_id = str(metadata.get("user_id") or "")
    if not payment_id or not user_id:
        return False, "missing_payment_or_user"

    amount_obj = payment_obj.get("amount") or {}
    try:
        amount = Decimal(str(amount_obj.get("value"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return False, "invalid_amount"
    if amount <= 0:
        return False, "invalid_amount"

    with db_connect() as conn:
        marker_key = f"deelo_tome_paid_{payment_id}"
        inserted = conn.execute(
            "INSERT OR IGNORE INTO kv(key,value,shared,updated_at) VALUES(?,?,1,strftime('%s','now'))",
            (marker_key, json.dumps({"user_id": user_id, "amount": f"{amount:.2f}"}, ensure_ascii=False)),
        ).rowcount
        if inserted == 0:
            return True, "already_processed"

        row = conn.execute(
            "SELECT value FROM kv WHERE key=?",
            (f"deelo_user_{user_id}",),
        ).fetchone()
        if not row:
            conn.execute("DELETE FROM kv WHERE key=?", (marker_key,))
            return False, "user_not_found"

        rec = json.loads(row[0])
        rec["balance"] = float(Decimal(str(rec.get("balance") or 0)) + amount)
        rec["topUpTotal"] = float(Decimal(str(rec.get("topUpTotal") or 0)) + amount)
        conn.execute(
            "UPDATE kv SET value=?, updated_at=strftime('%s','now') WHERE key=?",
            (json.dumps(rec, ensure_ascii=False), f"deelo_user_{user_id}"),
        )
        conn.commit()

    return True, "credited"


# ============================================================
# 31. TOME: WEBHOOK УСПЕШНОГО ПЛАТЕЖА
# ============================================================
async def tome_webhook(request: web.Request):
    if not TOME_SECRET_KEY:
        return web.Response(status=503, text="Tome is not configured")

    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid json")

    payment_obj = payload.get("object") or {}
    payment_id = str(payment_obj.get("id") or "")
    signature = str(payload.get("signature") or "")
    expected = _tome_signature(payment_id) if payment_id else ""

    if not signature or not expected or not hmac.compare_digest(signature, expected):
        log.warning("Rejected Tome webhook: invalid signature for payment=%s", payment_id)
        return web.Response(status=403, text="invalid signature")

    event = str(payload.get("event") or "")
    if event == "payment.succeeded" and payment_obj.get("status") == "succeeded":
        ok, reason = _credit_tome_payment(payment_obj)
        if not ok:
            log.error("Tome payment %s was not credited: %s", payment_id, reason)
            return web.Response(status=500, text=reason)
        log.info("Tome payment %s processed: %s", payment_id, reason)

    return web.Response(status=200, text="ok")


# ============================================================
# 32. СОЗДАНИЕ WEB-ПРИЛОЖЕНИЯ И РЕГИСТРАЦИЯ ROUTES
# ============================================================
async def health_handler(request: web.Request):
    return web.json_response({"ok": True, "service": "deelo", "webhook_path": WEBHOOK_PATH})



# ============================================================
# RESTORED SUPPORT / ADMIN CHAT LAYER
# Restores admin notifications, support inbox, manual mode and stickers.
# ============================================================
def _support_manual_key(user_id: str) -> str:
    # Новый ключ намеренно игнорирует старые случайно сохранённые manual=true.
    # После обновления ИИ снова включён у всех по умолчанию.
    return f"deelo_support_manual_v2_{user_id}"


def _support_is_manual(user_id: str) -> bool:
    """
    False = ИИ включён. Это состояние по умолчанию для любого пользователя.
    True появляется только после явного действия администратора.
    """
    state = db_get_json(_support_manual_key(str(user_id)), {}) or {}
    return bool(state.get("enabled") is True and state.get("setByAdmin") is True)


def _support_set_manual(user_id: str, enabled: bool, admin_id: str = ""):
    db_set_json(_support_manual_key(str(user_id)), {
        "enabled": bool(enabled),
        "setByAdmin": True,
        "adminId": str(admin_id or ""),
        "updatedAt": int(time.time()),
    }, 1)


def _support_append_message(user_id: str, message: dict):
    key = f"deelo_support_{user_id}"
    history = db_get_json(key, []) or []
    history.append(message)
    history = history[-80:]
    db_set_json(key, history)


async def api_support_manual_mode(request: web.Request):
    """
    Администратор включает/выключает ручной режим конкретному пользователю.
    """
    admin_id = telegram_user_from_init_data(
        request.headers.get("X-Telegram-Init-Data", "")
    )

    # На случай если initData передаётся в body.
    try:
        body = await request.json()
    except Exception:
        body = {}

    if not admin_id:
        admin_id = telegram_user_from_init_data(str(body.get("initData") or ""))

    if not admin_id or int(admin_id) not in ADMIN_IDS:
        return web.json_response(
            {"ok": False, "error": "forbidden", "message": "Нет доступа."},
            status=403
        )

    target_id = str(body.get("userId") or "").strip()
    enabled = bool(body.get("enabled"))

    if not target_id.isdigit():
        return web.json_response(
            {"ok": False, "error": "invalid_user", "message": "Некорректный пользователь."},
            status=400
        )

    # Переключение полностью тихое для обычного пользователя.
    # В чате не появляется никаких служебных сообщений про оператора.
    _support_set_manual(target_id, enabled, str(admin_id))

    return web.json_response({
        "ok": True,
        "userId": target_id,
        "manual": enabled,
    })


async def api_support_manual_status(request: web.Request):
    """
    Статус ручного режима.
    Администратор может смотреть любого пользователя.
    Обычный пользователь — только самого себя.
    """
    init_data = (
        request.headers.get("X-Telegram-Init-Data", "")
        or request.query.get("initData", "")
    )
    caller_id = telegram_user_from_init_data(str(init_data or ""))

    if not caller_id:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    target_id = str(request.query.get("userId") or caller_id)

    if str(caller_id) != target_id and int(caller_id) not in ADMIN_IDS:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)

    return web.json_response({
        "ok": True,
        "userId": target_id,
        "manual": _support_is_manual(target_id),
    })



def _support_visible_key(user_id: str) -> str:
    return f"deelo_support_{user_id}"


def _support_visible_history(user_id: str) -> list:
    items = db_get_json(_support_visible_key(str(user_id)), []) or []
    if not isinstance(items, list):
        return []

    # Старые версии случайно писали внутреннюю память ИИ в тот же ключ,
    # что и видимый чат. Такие записи имеют ts/intent, но не имеют id/time.
    clean = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not item.get("id") and item.get("ts") is not None and item.get("time") is None:
            continue
        clean.append(item)

    # Дополнительная защита от точных дублей по id.
    result = []
    seen_ids = set()
    for item in clean:
        mid = str(item.get("id") or "")
        if mid and mid in seen_ids:
            continue
        if mid:
            seen_ids.add(mid)
        result.append(item)
    return result


def _support_append_visible(
    user_id: str,
    role: str,
    text: str = "",
    *,
    message_id: str = "",
    attachments=None,
    intent: str = "",
    system: bool = False,
) -> tuple[dict, bool]:
    """Единственная серверная точка записи видимого чата поддержки."""
    uid = str(user_id)
    items = _support_visible_history(uid)
    mid = str(message_id or "").strip()

    if mid:
        for old in items:
            if str(old.get("id") or "") == mid:
                return old, False

    msg = {
        "id": mid or ("support_" + uuid.uuid4().hex),
        "role": str(role),
        "text": str(text or "")[:12000],
        "time": int(time.time() * 1000),
    }
    if attachments:
        msg["attachments"] = attachments
    if intent:
        msg["intent"] = str(intent)
    if system:
        msg["system"] = True

    items.append(msg)
    db_set_json(_support_visible_key(uid), items[-200:], 1)
    return msg, True


async def api_admin_support_chats(request: web.Request):
    admin_id = _admin_auth(request)
    if not admin_id:
        return web.json_response({"error": "forbidden"}, status=403)

    result = []
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT key, value, updated_at FROM kv "
            "WHERE key LIKE 'deelo_support_%' ORDER BY updated_at DESC"
        ).fetchall()

    for key, raw, updated_at in rows:
        m = re.fullmatch(r"deelo_support_(\d+)", str(key))
        if not m:
            continue
        uid = m.group(1)

        try:
            msgs = json.loads(raw) if raw else []
        except Exception:
            msgs = []

        if not isinstance(msgs, list) or not msgs:
            continue

        user = db_get_json(f"deelo_user_{uid}", {}) or {}
        last = msgs[-1] if msgs else {}
        last_user = next((x for x in reversed(msgs) if x.get("role") == "user"), last)

        result.append({
            "userId": uid,
            "username": str(user.get("telegramUsername") or ""),
            "name": str(user.get("username") or user.get("firstName") or ("Пользователь " + uid[-4:])),
            "manual": _support_is_manual(uid),
            "lastText": str((last_user or {}).get("text") or (last or {}).get("text") or ""),
            "lastTime": int((last or {}).get("time") or (((last or {}).get("ts") or 0) * 1000) or 0),
            "messages": len(msgs),
        })

    result.sort(key=lambda x: x.get("lastTime", 0), reverse=True)
    return web.json_response(
        {"ok": True, "items": result},
        headers={"Cache-Control": "no-store"}
    )


async def api_admin_support_chat(request: web.Request):
    admin_id = _admin_auth(request)
    if not admin_id:
        return web.json_response({"error": "forbidden"}, status=403)

    uid = str(request.query.get("userId") or "").strip()
    if not uid.isdigit():
        return web.json_response({"error": "invalid_user"}, status=400)

    user = db_get_json(f"deelo_user_{uid}", {}) or {}
    return web.json_response({
        "ok": True,
        "user": {
            "id": uid,
            "username": str(user.get("telegramUsername") or ""),
            "name": str(user.get("username") or user.get("firstName") or ("Пользователь " + uid[-4:])),
        },
        "manual": _support_is_manual(uid),
        "messages": _support_visible_history(uid)[-200:],
    }, headers={"Cache-Control": "no-store"})


async def api_admin_support_reply(request: web.Request):
    admin_id = _admin_auth(request)
    if not admin_id:
        return web.json_response({"error": "forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    uid = str(body.get("userId") or "").strip()
    text = _normalize_human_text(body.get("text"), limit=5000)
    message_id = str(body.get("messageId") or "").strip()

    if not uid.isdigit():
        return web.json_response({"error": "invalid_user"}, status=400)
    if not text:
        return web.json_response({"error": "empty_message"}, status=400)

    # Сам ответ администратора не переключает режим.
    # ИИ отключается только отдельной явной кнопкой для этого пользователя.
    manual_now = _support_is_manual(uid)

    msg, added = _support_append_visible(
        uid,
        "admin",
        text,
        message_id=message_id or ("admin_" + uuid.uuid4().hex),
    )
    if added:
        append_notification(uid, text, 'Ответ поддержки Playerok', '🛡️', kind='support_message', actionLabel='Открыть поддержку')
        try:
            await bot.send_message(
                int(uid),
                '🛡️ <b>Поддержка Playerok ответила</b>\n\n' + escape_html_server(text),
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text='🚀 Открыть Playerok',
                        web_app=WebAppInfo(url=f'{WEBAPP_URL}?screen=support')
                    )
                ]])
            )
        except Exception:
            log.exception('admin support reply bot notification failed user=%s', uid)
    return web.json_response({"ok": True, "message": msg, "manual": manual_now})



def _support_admin_keyboard(user_id: str, manual: bool) -> InlineKeyboardMarkup:
    """
    Кнопки под уведомлением администратору о новом сообщении в поддержку.
    Первая кнопка открывает сразу конкретный диалог в операторской консоли.
    Вторая включает/выключает локальный ИИ для этого пользователя.
    """
    uid = str(user_id)
    mode_text = "🤖 Включить ИИ" if manual else "🧑‍💻 Забрать чат"
    mode_cb = f"support_ai_on:{uid}" if manual else f"support_ai_off:{uid}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="💬 Открыть диалог",
                web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=admin-support&userId={quote(uid)}")
            )
        ],
        [
            InlineKeyboardButton(text=mode_text, callback_data=mode_cb)
        ],
        [
            InlineKeyboardButton(
                text="👑 Админ-панель",
                web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=admin")
            )
        ],
    ])


def _html_safe(value) -> str:
    """Minimal HTML escaping for Telegram parse_mode=HTML messages."""
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


async def _notify_admins_support_message(user_id: str, text: str, *, manual: bool):
    """
    Уведомляет всех админов о каждом НОВОМ пользовательском сообщении в поддержку.
    Повторный HTTP-запрос с тем же messageId сюда не попадёт, потому что
    вызывается только когда _support_append_visible(...)[1] == True.
    """
    uid = str(user_id)
    rec = db_get_json(f"deelo_user_{uid}", {}) or {}
    name = str(rec.get("username") or rec.get("firstName") or ("Пользователь " + uid[-4:]))
    username = str(rec.get("telegramUsername") or "").lstrip("@")
    preview = _normalize_human_text(text).replace("\n", " ")
    if len(preview) > 500:
        preview = preview[:497] + "..."

    state = "🟢 Ручной режим" if manual else "🤖 ИИ включён"
    username_line = f"@{_html_safe(username)}" if username else "без username"

    msg = (
        "🔔 <b>Новое сообщение в поддержку</b>\n"
        "<u>━━━━━━━━━━━━━━━━━━━━</u>\n"
        f"👤 <b>{_html_safe(name)}</b> · {username_line}\n"
        f"🆔 <code>{_html_safe(uid)}</code>\n"
        f"⚙️ {state}\n\n"
        f"💬 <b>Сообщение:</b>\n{_html_safe(preview)}\n\n"
        "Открой диалог, чтобы ответить пользователю от имени поддержки."
    )

    kb = _support_admin_keyboard(uid, manual)
    for aid in _admin_ids():
        try:
            await bot.send_message(int(aid), msg, parse_mode="HTML", reply_markup=kb)
        except Exception:
            log.exception("Не удалось отправить support-уведомление админу %s", aid)


@dp.callback_query(F.data.regexp(r"^support_ai_(on|off):\d+$"))
async def cb_support_ai_mode(callback: CallbackQuery):
    if str(callback.from_user.id) not in _admin_ids():
        await callback.answer("Нет доступа", show_alert=True)
        return

    raw = str(callback.data or "")
    m = re.fullmatch(r"support_ai_(on|off):(\d+)", raw)
    if not m:
        await callback.answer("Некорректная команда", show_alert=True)
        return

    action, uid = m.group(1), m.group(2)

    # Переключение режима видно только администратору.
    if action == "off":
        _support_set_manual(uid, True, str(callback.from_user.id))
        await callback.answer("ИИ выключен. Теперь отвечаешь ты.")
        status_text = "🟢 Ручной режим"
    else:
        _support_set_manual(uid, False, str(callback.from_user.id))
        await callback.answer("ИИ снова включён.")
        status_text = "🤖 ИИ включён"

    try:
        await callback.message.edit_reply_markup(
            reply_markup=_support_admin_keyboard(uid, _support_is_manual(uid))
        )
    except Exception:
        pass

    try:
        await callback.message.answer(
            f"{status_text} для пользователя <code>{uid}</code>.",
            parse_mode="HTML"
        )
    except Exception:
        pass



async def api_support_sticker(request: web.Request):
    """Сохраняет emoji/встроенный стикер пользователя в чат поддержки."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    user_id = telegram_user_from_init_data(str(body.get("initData") or ""))
    if not user_id:
        return web.json_response({"error": "invalid_telegram_init_data"}, status=401)

    sticker = str(body.get("sticker") or "").strip()[:500]
    message_id = str(body.get("messageId") or "").strip()

    if not sticker:
        return web.json_response({"error": "empty_sticker"}, status=400)

    msg, added = _support_append_visible(
        str(user_id),
        "user",
        "",
        message_id=message_id or ("sticker_user_" + uuid.uuid4().hex),
    )

    # _support_append_visible знает о тексте, поэтому поле sticker дописываем атомарно.
    if added:
        items = _support_visible_history(str(user_id))
        for item in reversed(items):
            if str(item.get("id") or "") == str(msg.get("id") or ""):
                item["sticker"] = sticker
                break
        db_set_json(_support_visible_key(str(user_id)), items[-200:], 1)

        await _notify_admins_support_message(
            str(user_id),
            f"[стикер] {sticker}",
            manual=_support_is_manual(str(user_id)),
        )

    return web.json_response({
        "ok": True,
        "manual": _support_is_manual(str(user_id)),
    })


async def api_admin_support_sticker(request: web.Request):
    """Администратор отправляет стикер в тот же чат поддержки."""
    admin_id = _admin_auth(request)
    if not admin_id:
        return web.json_response({"error": "forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    uid = str(body.get("userId") or "").strip()
    sticker = str(body.get("sticker") or "").strip()[:500]
    message_id = str(body.get("messageId") or "").strip()

    if not uid.isdigit():
        return web.json_response({"error": "invalid_user"}, status=400)
    if not sticker:
        return web.json_response({"error": "empty_sticker"}, status=400)

    msg, added = _support_append_visible(
        uid,
        "admin",
        "",
        message_id=message_id or ("sticker_admin_" + uuid.uuid4().hex),
    )

    if added:
        items = _support_visible_history(uid)
        for item in reversed(items):
            if str(item.get("id") or "") == str(msg.get("id") or ""):
                item["sticker"] = sticker
                break
        db_set_json(_support_visible_key(uid), items[-200:], 1)

    return web.json_response({
        "ok": True,
        "manual": _support_is_manual(uid),
    })


async def api_support_ai(request: web.Request):
    """
    Единый обработчик поддержки:
    - пишет вопрос пользователя в видимый чат ровно один раз;
    - при auto генерирует и сохраняет ответ ровно один раз;
    - при manual только сохраняет вопрос и ждёт оператора.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    user_id = telegram_user_from_init_data(str(body.get("initData") or ""))
    if not user_id:
        return web.json_response({
            "error": "invalid_telegram_init_data",
            "message": "Открой Mini App из Telegram."
        }, status=401)

    user_id = str(user_id)
    text = str(body.get("message") or "").strip()[:5000]
    attachments = body.get("attachments") or []
    message_id = str(body.get("messageId") or "").strip()

    if not text and attachments:
        text = "Пользователь прикрепил файл к обращению."
    if not text:
        return web.json_response({"error": "empty_message"}, status=400)

    user_mid = message_id or ("user_" + uuid.uuid4().hex)
    _, user_added = _support_append_visible(
        user_id,
        "user",
        text,
        message_id=user_mid,
        attachments=attachments,
    )

    if user_added:
        try:
            await _notify_admins_support_message(
                user_id,
                text,
                manual=_support_is_manual(user_id),
            )
        except Exception:
            # Админ-уведомление не должно ломать саму поддержку для пользователя.
            log.exception("support admin notification failed for user %s", user_id)

    assistant_mid = f"answer_{user_mid}"
    existing = next(
        (m for m in _support_visible_history(user_id)
         if str(m.get("id") or "") == assistant_mid),
        None
    )
    if existing:
        return web.json_response({
            "ok": True,
            "reply": str(existing.get("text") or ""),
            "manual": False,
            "skipAutoReply": False,
            "mode": "smart_local_only",
            "intent": str(existing.get("intent") or ""),
            "confidence": 1.0,
            "external_ai": False,
        })

    if _support_is_manual(user_id):
        return web.json_response({
            "ok": True,
            "manual": True,
            "skipAutoReply": True,
            "reply": "",
            "mode": "human_support",
            "external_ai": False,
        })

    try:
        reply, confidence, intent = _local_support_reply(user_id, text)

        _support_append_visible(
            user_id,
            "assistant",
            reply or "",
            message_id=assistant_mid,
            intent=intent,
        )

        # Память v9 хранится отдельно в deelo_support_memory_*.
        if user_added and "_smart_history_add" in globals():
            try:
                _smart_history_add(user_id, "user", text, "")
                _smart_history_add(user_id, "assistant", reply, intent)
            except Exception:
                pass

        return web.json_response({
            "ok": True,
            "reply": reply or "",
            "manual": False,
            "skipAutoReply": False,
            "mode": "smart_local_only" if "_smart_local_support_reply" in globals() else "local_only",
            "intent": intent,
            "confidence": round(float(confidence or 0), 3),
            "external_ai": False,
        })

    except Exception:
        log.exception("support reply failed")
        fallback = "Не получилось обработать сообщение. Попробуй отправить его ещё раз."
        _support_append_visible(
            user_id,
            "assistant",
            fallback,
            message_id=assistant_mid,
            intent="error",
        )
        return web.json_response({
            "ok": True,
            "reply": fallback,
            "manual": False,
            "skipAutoReply": False,
            "mode": "local_error_fallback",
            "external_ai": False,
        })



async def api_subscription_status(request: web.Request):
    """Signed Mini App check: promo users are blocked until subscribed to ShadowTeamReserve."""
    init_data = request.headers.get("X-Telegram-Init-Data", "") or request.query.get("initData", "")
    user_id = telegram_user_from_init_data(str(init_data))
    if not user_id:
        return web.json_response({"error": "invalid_telegram_init_data"}, status=401)

    required = _subscription_required(user_id)
    subscribed = True
    if required:
        subscribed = await _subscription_is_member(user_id)

    return web.json_response({
        "ok": True,
        "required": bool(required),
        "subscribed": bool(subscribed),
        "allowed": bool((not required) or subscribed),
        "username": REQUIRED_SUB_USERNAME,
        "url": REQUIRED_SUB_URL,
    }, headers={"Cache-Control": "no-store"})


def create_app() -> web.Application:
    app = web.Application()
    log.info("YOOMONEY WEBHOOK URL: %s%s", WEBAPP_URL.rstrip("/"), YOOMONEY_WEBHOOK_PATH)
    log.info("TOME VERIFY ROUTE REGISTERED")

    app.router.add_get("/stickers/{name:.*}", sticker_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/api/subscription/status", api_subscription_status)
    app.router.add_get("/", index_handler)
    app.router.add_get("/verification.txt", verification_handler)
    app.router.add_get("/index.html", index_handler)
    app.router.add_get("/app", index_handler)
    app.router.add_get("/app/", index_handler)
    app.router.add_get("/api/store/{key:.*}", store_get)
    app.router.add_post("/api/store/{key:.*}", store_set)
    app.router.add_delete("/api/store/{key:.*}", store_delete)
    app.router.add_get("/api/store-list", store_list)
    app.router.add_post("/api/deals/create", api_create_deal)
    app.router.add_post("/api/deals/respond", api_respond_deal)
    app.router.add_post("/api/deals/sale-info", api_deal_sale_info)
    app.router.add_post("/api/deals/sale-info-heartbeat", api_deal_sale_info_heartbeat)
    app.router.add_post("/api/deals/sale-info-cancel", api_deal_sale_info_cancel)
    app.router.add_post("/api/deals/confirm-amount", api_deal_confirm_amount)
    app.router.add_post("/api/deals/complete", api_complete_deal)
    app.router.add_post("/api/deals/action", api_deal_action)
    app.router.add_get("/api/deals/search", api_search_deals)
    app.router.add_get("/api/deals/item", api_deal_item)
    app.router.add_get("/api/deals/chat", api_deal_chat_get)
    app.router.add_post("/api/deals/chat", api_deal_chat_post)
    app.router.add_post("/api/deals/dispute", api_deal_dispute)
    app.router.add_post("/api/deals/admin-join", api_deal_admin_join)
    app.router.add_post("/api/deals/admin-release-seller", api_deal_admin_release_seller)
    app.router.add_post("/api/deals/admin-refund-buyer", api_deal_admin_refund_buyer)
    app.router.add_get("/api/admin/user", api_admin_user)
    app.router.add_get("/api/admin/dashboard", api_admin_dashboard)
    app.router.add_get("/api/admin/find-user", api_admin_find_user)
    app.router.add_post("/api/admin/user-action", api_admin_user_action)
    app.router.add_post("/api/admin/message-user", api_admin_message_user)
    app.router.add_get("/api/admin/stars-balance", api_admin_stars_balance)
    app.router.add_post("/api/admin/gift-premium", api_admin_gift_premium)
    app.router.add_post("/api/admin/unverify-all", api_admin_unverify_all)
    app.router.add_post("/api/request-contact", api_request_contact)
    app.router.add_post("/api/tome/create-payment", api_tome_create_payment)
    app.router.add_post(TOME_WEBHOOK_PATH, tome_webhook)
    app.router.add_post("/api/yoomoney/create-payment", api_yoomoney_create_payment)
    app.router.add_post("/api/stars/create-payment", api_stars_create_payment)
    app.router.add_post("/api/crypto/create-payment", api_crypto_create_payment)
    app.router.add_post("/api/withdraw/create", api_withdraw_create)
    app.router.add_get("/api/payments/history", api_payment_history)
    app.router.add_get("/api/crypto/payment-status", api_crypto_payment_status)
    app.router.add_get("/api/support/status", api_support_ai_status)
    app.router.add_get("/api/support/manual-status", api_support_manual_status)
    app.router.add_post("/api/support/manual-mode", api_support_manual_mode)
    app.router.add_post("/api/support/ai", api_support_ai)
    app.router.add_get("/api/admin/support-chats", api_admin_support_chats)
    app.router.add_get("/api/admin/support-chat", api_admin_support_chat)
    app.router.add_post("/api/admin/support-reply", api_admin_support_reply)
    app.router.add_post("/api/support/sticker", api_support_sticker)
    app.router.add_post("/api/admin/support-sticker", api_admin_support_sticker)
    app.router.add_get(YOOMONEY_PAY_PATH, yoomoney_pay_page)
    app.router.add_get(YOOMONEY_WEBHOOK_PATH, yoomoney_webhook_health)
    app.router.add_post(YOOMONEY_WEBHOOK_PATH, yoomoney_webhook)
    app.router.add_post(YOOMONEY_WEBHOOK_PATH + "/", yoomoney_webhook)
    app.router.add_post("/yoomoney/notification", yoomoney_webhook)

    # Register dispatcher lifecycle handlers BEFORE aiohttp wires dispatcher startup.
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    return app

# ============================================================
# 33. ТОЧКА ЗАПУСКА
# ============================================================
if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)