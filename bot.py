import asyncio
import csv
import html
import io
import json
import logging
import hmac
import hashlib
import os
import re
import sqlite3
import zipfile
from html.parser import HTMLParser
from urllib.parse import urlparse
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from aiohttp import web
import aiosqlite
from offer_card import render_offer_card
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InlineQuery, InlineQueryResultArticle, InlineQueryResultPhoto, InputTextMessageContent, LinkPreviewOptions, Message, SwitchInlineQueryChosenChat
from dotenv import load_dotenv
from telethon import TelegramClient, events, functions, types as tl_types
from telethon.extensions import html as telethon_html

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")],
)
ENV_FILE = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_FILE, encoding="utf-8-sig", override=True)


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Настройка {name} должна быть целым числом; сейчас указано: {raw!r}") from exc
    if value < minimum:
        raise RuntimeError(f"Настройка {name} должна быть не меньше {minimum}")
    return value


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip().strip('"').strip("'")
GROUP_BOT_TOKEN = os.getenv("GROUP_BOT_TOKEN", "").strip().strip('"').strip("'")
raw_mtproto_api_id = os.getenv("MTPROTO_API_ID", "").strip()
try:
    MTPROTO_API_ID = int(raw_mtproto_api_id) if raw_mtproto_api_id else None
except ValueError as exc:
    raise RuntimeError("MTPROTO_API_ID должен быть числовым API ID из my.telegram.org.") from exc
MTPROTO_API_HASH = os.getenv("MTPROTO_API_HASH", "").strip() or None
raw_bonus_group_id = os.getenv("BONUS_GROUP_ID", "").strip()
try:
    BONUS_GROUP_ID = int(raw_bonus_group_id) if raw_bonus_group_id else None
except ValueError as exc:
    raise RuntimeError("BONUS_GROUP_ID должен быть числовым ID группы Telegram вида -100...") from exc
if GROUP_BOT_TOKEN and BONUS_GROUP_ID is None:
    logging.warning("GROUP_BOT_TOKEN задан, но BONUS_GROUP_ID пуст: групповой бонус будет недоступен до настройки ID группы")
if not BOT_TOKEN or BOT_TOKEN == "put_fresh_token_here":
    raise RuntimeError("Не заполнен BOT_TOKEN в .env. Вставь новый токен от @BotFather без текста BOT_TOKEN= и без пробелов вокруг него.")

raw_admin_ids = os.getenv("ADMIN_IDS", "").strip()
admin_parts = [part.strip() for part in raw_admin_ids.split(",") if part.strip()]
if not admin_parts or not all(part.isdigit() for part in admin_parts):
    raise RuntimeError("Неверный ADMIN_IDS в .env. Укажи числовой Telegram ID, например ADMIN_IDS=123456789; несколько ID разделяй запятой.")
ROOT_ADMIN_IDS = frozenset(int(part) for part in admin_parts)
ADMIN_IDS = set(ROOT_ADMIN_IDS)

STAR_RECEIVE_USERNAME = os.getenv("STAR_RECEIVE_USERNAME", "").strip().lstrip("@")
if STAR_RECEIVE_USERNAME.lower() in {"your_username", "username", "replace_with_stars_receiver_username"} or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", STAR_RECEIVE_USERNAME):
    raise RuntimeError("Неверный STAR_RECEIVE_USERNAME в .env. Укажи username получателя Stars без @, например STAR_RECEIVE_USERNAME=myname.")

CRYPTOPAY_TOKEN = os.getenv("CRYPTOPAY_TOKEN", "").strip()
raw_usdt_rate = os.getenv("USDT_PER_STAR", "0").strip() or "0"
try:
    USDT_PER_STAR = Decimal(raw_usdt_rate)
except InvalidOperation as exc:
    raise RuntimeError("USDT_PER_STAR должен быть числом, например 0.02. Оставь 0, чтобы временно выключить Crypto Pay.") from exc
if USDT_PER_STAR < 0:
    raise RuntimeError("USDT_PER_STAR не может быть отрицательным.")
PREMIUM_PRICE_STARS = env_int("PREMIUM_PRICE_STARS", 50)
PREMIUM_DAYS = env_int("PREMIUM_DAYS", 30)
OFFER_TTL_HOURS = env_int("OFFER_TTL_HOURS", 48)
GIFT_CUSTODY_USERNAME = "staroffers_bank"
MAX_SQLITE_INTEGER = (1 << 63) - 1
DEFAULT_SUBSCRIPTION_PLANS = (
    # offers_per_day is a legacy DB column and is always ignored; offer count has no daily cap.
    ("free", "Бесплатный", 0, 0, 0, 3, 0),
    ("start", "Старт", 19, 30, 0, 15, 1),
    ("plus", "Плюс", 39, 30, 0, 50, 2),
    ("pro", "Про", 79, 30, 0, 150, 3),
)
BACKUP_RETENTION_DAYS = env_int("BACKUP_RETENTION_DAYS", 7)
OFFERS_PAUSED = False
raw_db_path_value = os.getenv("DATABASE_PATH", "offers.sqlite3").strip() or "offers.sqlite3"
raw_db_path = Path(raw_db_path_value).expanduser()
DB_PATH = str(raw_db_path if raw_db_path.is_absolute() else BASE_DIR / raw_db_path)
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
NFT_PHOTO_DIR = (BASE_DIR / "data" / "nft_photos") if Path(DB_PATH).parent == BASE_DIR else (Path(DB_PATH).parent / "nft_photos")
OFFER_CARD_DIR = Path(DB_PATH).parent / "offer_cards"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
if not PUBLIC_BASE_URL and os.getenv("RAILWAY_PUBLIC_DOMAIN"):
    PUBLIC_BASE_URL = "https://" + os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip().strip("/")
CARD_SERVER_PORT = env_int("PORT", 8080)
CARD_WEB_RUNNER = None
if PUBLIC_BASE_URL and not PUBLIC_BASE_URL.startswith("https://"):
    logging.warning("PUBLIC_BASE_URL must use https://; custom offer-card artwork is disabled")
    PUBLIC_BASE_URL = ""

try:
    MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:
    MOSCOW_TIMEZONE = timezone(timedelta(hours=3), name="Europe/Moscow")

try:
    TIMEZONE = ZoneInfo("Europe/Minsk")
except ZoneInfoNotFoundError:
    # Windows Python distributions may lack the IANA timezone database.
    # Minsk currently uses UTC+3 year-round; tzdata is also installed from requirements.
    TIMEZONE = timezone(timedelta(hours=3), name="Europe/Minsk")

try:
    bot = Bot(BOT_TOKEN)
except Exception as exc:
    raise RuntimeError("Не удалось прочитать BOT_TOKEN. Проверь, что в .env указан полный новый токен от @BotFather без кавычек, пробелов и слова BOT_TOKEN.") from exc

dp = Dispatcher()
group_dp = Dispatcher() if GROUP_BOT_TOKEN else None
group_bot = Bot(GROUP_BOT_TOKEN) if GROUP_BOT_TOKEN else None
offers_mtproto_client = None


class PaymentForm(StatesGroup):
    stars_amount = State()
    operation_id = State()
    seller_gift_link = State()
    admin_note = State()
    dispute_reason = State()


class AdminForm(StatesGroup):
    lookup_id = State()
    amount = State()
    subscription_days = State()
    bulk_amount = State()
    bulk_subscription_days = State()
    config_value = State()
    offer_search = State()
    plan_config = State()
    group_bonus_amount = State()
    group_bonus_period = State()
    group_bonus_fund = State()


class ParserForm(StatesGroup):
    nft_link = State()


def now_local() -> datetime:
    return datetime.now(TIMEZONE)


def now_moscow() -> datetime:
    return datetime.now(MOSCOW_TIMEZONE)


def format_moscow(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MOSCOW_TIMEZONE)
        return parsed.astimezone(MOSCOW_TIMEZONE).strftime("%d.%m.%Y %H:%M МСК")
    except (ValueError, TypeError):
        return str(raw)


class OpenGraphParser(HTMLParser):
    """Read public OG tags and the visible attribute table on a t.me NFT page."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = {}
        self.rows = {}
        self._row_active = False
        self._cell_tag = None
        self._cell_text = []
        self._current_header = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        values = {key.lower(): value for key, value in attrs if key and value is not None}
        if tag == "meta":
            prop = (values.get("property") or values.get("name") or "").lower()
            if prop in {"og:title", "og:image", "og:description", "twitter:title"}:
                self.values[prop] = values.get("content", "").strip()
        elif tag == "tr":
            self._row_active = True
            self._current_header = None
        elif tag in {"th", "td"} and self._row_active:
            self._cell_tag = tag
            self._cell_text = []

    def handle_data(self, data):
        if self._cell_tag:
            self._cell_text.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"th", "td"} and self._cell_tag == tag:
            value = " ".join("".join(self._cell_text).split())
            if tag == "th":
                self._current_header = value
            elif self._current_header and value:
                self.rows[self._current_header] = value
            self._cell_tag = None
            self._cell_text = []
        elif tag == "tr":
            self._row_active = False
            self._current_header = None


def parsed_nft_attributes(parser: OpenGraphParser) -> dict:
    traits = {}
    rarities = {}
    for key in ("Model", "Backdrop", "Symbol"):
        raw = parser.rows.get(key, "")
        match = re.fullmatch(r"(.+?)\s+([0-9]+(?:\.[0-9]+)?)%", raw)
        if match:
            traits[key.lower()] = match.group(1).strip()
            rarities[key.lower()] = float(match.group(2))
        elif raw:
            traits[key.lower()] = raw.strip()
    if not traits:
        desc = parser.values.get("og:description", "")
        for key in ("Model", "Backdrop", "Symbol"):
            match = re.search(rf"(?:^|\s){key}:\s*(.*?)(?=\s+(?:Model|Backdrop|Symbol):|$)", desc)
            if match and match.group(1).strip():
                traits[key.lower()] = match.group(1).strip()
    owner = parser.rows.get("Owner", "").strip()
    quantity = parser.rows.get("Quantity", "").strip()
    return {"traits": traits, "rarities": rarities, "owner": owner, "quantity": quantity}


def normalize_nft_link(raw_link: str):
    try:
        parsed = urlparse(raw_link.strip())
    except ValueError:
        return None
    path = parsed.path.strip("/")
    parts = path.split("/")
    if (parsed.scheme != "https" or parsed.netloc.lower() != "t.me" or parsed.query or parsed.fragment
            or len(parts) != 2 or parts[0].lower() != "nft"
            or not re.fullmatch(r"[A-Za-z0-9_]+-[0-9]+", parts[1])):
        return None
    slug = parts[1]
    gift_number = slug.rsplit("-", 1)[1]
    return f"https://t.me/nft/{slug}", slug, gift_number


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def button(text, data, style=None):
    return InlineKeyboardButton(text=text, callback_data=data, **({"style": style} if style else {}))


def main_menu(user_id: int | None = None) -> InlineKeyboardMarkup:
    rows = [
        [button("🎁 Оферы", "menu:new", "success")],
        [button("🧩 Парсер", "menu:parser", "primary")],
        [button("💰 Баланс и пополнение", "menu:wallet", "primary"), button("👤 Профиль", "menu:profile", "primary")],
        [button("⭐ Подписка", "menu:premium"), button("ℹ️ Помощь", "menu:help")],
        [button("🆔 Мой ID", "menu:id")],
    ]
    if user_id in ADMIN_IDS:
        rows.append([button("🛠 Админ-панель", "menu:admin", "primary")])
    return kb(rows)


def crypto_pay_enabled() -> bool:
    return bool(CRYPTOPAY_TOKEN) and USDT_PER_STAR > 0


def payment_menu(purpose: str) -> InlineKeyboardMarkup:
    rows = [[button("⭐ Stars на username", f"paystars:{purpose}", "primary")]]
    if crypto_pay_enabled():
        rows.append([button("💎 Crypto Pay", f"paycrypto:{purpose}", "success")])
    rows.append([button("↩️ Назад", "menu:wallet")])
    return kb(rows)


def offer_card(row) -> str:
    nft_label = f"{row['nft_title']} #{row['nft_number']}" if row["nft_title"] and row["nft_number"] else "NFT-подарок"
    return (
        "🎁 <b>ОФФЕР НА NFT-ПОДАРОК</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Сделка № <b>{row['id']}</b>\n"
        f"👤 Покупатель: @{html.escape(row['buyer_username'] or 'без username')}\n"
        f"💰 Цена: <b>{row['amount']} ⭐-единиц</b>\n"
        f"📦 Куда отправить NFT: @{html.escape(row['receiver_username'])}\n"
        f"🖼️ NFT: {html.escape(nft_label)}\n"
        f"🔗 Ссылка на NFT: {html.escape(row['gift_url'])}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Если готов продать этот подарок — используй кнопки ниже. "
        "При нажатии «Принять» бот проверит баланс покупателя и обеспечит сделку доступными внутренними средствами."
    )


def seller_accept_keyboard(offer_id: int, photo: bool = False):
    # Preserve the original offer button labels; the suffix only selects caption-vs-text editing for legacy photo cards.
    suffix = ":photo" if photo else ""
    reject = InlineKeyboardButton(text="⊗ Отклонить", callback_data=f"reject:{offer_id}{suffix}")
    accept = InlineKeyboardButton(text="✓ Принять", callback_data=f"accept:{offer_id}{suffix}")
    return kb([[reject, accept]])


def admin_offer_keyboard(offer_id: int, stage: str):
    if stage == "review":
        return kb([
            [button("✅ Подарок получен", f"giftok:{offer_id}", "success"), button("❌ Отклонить", f"giftno:{offer_id}", "danger")],
            [button("📝 Примечание", f"note:offer:{offer_id}")],
        ])
    return kb([
        [button("💸 Отметить выплаченным", f"paid:{offer_id}", "success")],
        [button("📝 Примечание", f"note:offer:{offer_id}")],
    ])


def participant_dispute_keyboard(offer_id: int):
    return kb([[button("🆘 Проблема / открыть спор", f"dispute:{offer_id}", "danger")]])


def admin_dispute_keyboard(offer_id: int):
    return kb([
        [button("✅ Подтвердить подарок → выплата", f"disputeok:{offer_id}", "success")],
        [button("↩️ Вернуть резерв покупателю", f"disputerefund:{offer_id}", "danger")],
        [button("📝 Примечание", f"note:offer:{offer_id}")],
    ])


def admin_waiting_keyboard(offer_id: int):
    return kb([
        [button("🔔 Напомнить участникам", f"remind:{offer_id}", "primary")],
        [button("📝 Примечание", f"note:offer:{offer_id}")],
    ])


def rating_keyboard(offer_id: int):
    return kb([[button(f"{score}⭐", f"rate:{offer_id}:{score}") for score in range(1, 6)]])


def admin_payment_keyboard(payment_id: int):
    return kb([
        [button("✅ Одобрить", f"payok:{payment_id}", "success"), button("❌ Отклонить", f"payno:{payment_id}", "danger")],
        [button("📝 Примечание", f"note:payment:{payment_id}")],
    ])


def admin_user_keyboard(row):
    blocked_text = "🔓 Разблокировать" if row["blocked"] else "🚫 Заблокировать"
    blocked_action = "unblock" if row["blocked"] else "block"
    return kb([
        [button("➕ Начислить ⭐", f"user:add:{row['user_id']}", "success"), button("➖ Списать ⭐", f"user:remove:{row['user_id']}", "danger")],
        [button(blocked_text, f"user:{blocked_action}:{row['user_id']}", "danger" if not row["blocked"] else "success")],
        [button("⭐ Выдать тариф", f"user:grant:{row['user_id']}", "primary")],
    ])


async def init_db():
    global USDT_PER_STAR, PREMIUM_PRICE_STARS, PREMIUM_DAYS, OFFER_TTL_HOURS, OFFERS_PAUSED
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS delegated_admins (user_id INTEGER PRIMARY KEY, added_by INTEGER NOT NULL, added_at TEXT NOT NULL)")
        admin_rows = await (await db.execute("SELECT user_id FROM delegated_admins")).fetchall()
        ADMIN_IDS.clear()
        ADMIN_IDS.update(ROOT_ADMIN_IDS)
        ADMIN_IDS.update(row[0] for row in admin_rows)
        await db.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
        defaults = {
            "usdt_per_star": str(USDT_PER_STAR),
            "premium_price_stars": str(PREMIUM_PRICE_STARS),
            "premium_days": str(PREMIUM_DAYS),
            "offer_ttl_hours": str(OFFER_TTL_HOURS),
            "offers_paused": "0",
            "group_bonus_amount": "0",
            "group_bonus_period_hours": "24",
            "group_bonus_pool": "0",
            "group_bonus_enabled": "0",
            "group_bonus_message_id": "0",
        }
        stamp = now_local().isoformat()
        await db.executemany("INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
                             [(key, value, stamp) for key, value in defaults.items()])
        settings_cur = await db.execute("SELECT key,value FROM app_settings")
        saved_settings = dict(await settings_cur.fetchall())
        try:
            saved_rate = Decimal(saved_settings["usdt_per_star"])
            if not saved_rate.is_finite() or saved_rate < 0:
                raise ValueError("saved rate must be finite and non-negative")
            USDT_PER_STAR = saved_rate
        except (InvalidOperation, TypeError, ValueError, KeyError):
            logging.exception("Invalid saved Crypto Pay rate; using environment setting")
        int_settings = {
            "premium_price_stars": ("PREMIUM_PRICE_STARS", 1, 1_000_000),
            "premium_days": ("PREMIUM_DAYS", 1, 3650),
            "offer_ttl_hours": ("OFFER_TTL_HOURS", 1, 720),
        }
        for key, (variable, minimum, maximum) in int_settings.items():
            try:
                value = int(saved_settings[key])
                if not minimum <= value <= maximum:
                    raise ValueError("saved setting is outside permitted bounds")
                globals()[variable] = value
            except (ValueError, TypeError, KeyError):
                logging.exception("Invalid saved setting %s; using environment default", key)
        OFFERS_PAUSED = saved_settings.get("offers_paused", "0") == "1"
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER NOT NULL DEFAULT 0,
            reserved INTEGER NOT NULL DEFAULT 0, premium_until TEXT, subscription_plan TEXT NOT NULL DEFAULT 'free', blocked INTEGER NOT NULL DEFAULT 0,
            unlimited_offers INTEGER NOT NULL DEFAULT 0, unlimited_balance INTEGER NOT NULL DEFAULT 0,
            unlimited_parser INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        )""")
        cur = await db.execute("PRAGMA table_info(users)")
        user_columns = {row[1] for row in await cur.fetchall()}
        if "unlimited_offers" not in user_columns:
            await db.execute("ALTER TABLE users ADD COLUMN unlimited_offers INTEGER NOT NULL DEFAULT 0")
        if "unlimited_balance" not in user_columns:
            await db.execute("ALTER TABLE users ADD COLUMN unlimited_balance INTEGER NOT NULL DEFAULT 0")
        if "unlimited_parser" not in user_columns:
            await db.execute("ALTER TABLE users ADD COLUMN unlimited_parser INTEGER NOT NULL DEFAULT 0")
        if "subscription_plan" not in user_columns:
            await db.execute("ALTER TABLE users ADD COLUMN subscription_plan TEXT NOT NULL DEFAULT 'free'")
            await db.execute("UPDATE users SET subscription_plan='plus' WHERE premium_until IS NOT NULL")
        # Keep the legacy column for schema compatibility; offer quantity is no longer user-configurable.
        await db.execute("UPDATE users SET unlimited_offers=0")
        await db.execute("""CREATE TABLE IF NOT EXISTS subscription_plans (
            code TEXT PRIMARY KEY, name TEXT NOT NULL, price_units INTEGER NOT NULL DEFAULT 0,
            duration_days INTEGER NOT NULL DEFAULT 0, offers_per_day INTEGER NOT NULL,
            parser_per_day INTEGER NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1
        )""")
        await db.executemany(
            "INSERT OR IGNORE INTO subscription_plans(code,name,price_units,duration_days,offers_per_day,parser_per_day,sort_order,enabled) VALUES(?,?,?,?,?,?,?,1)",
            DEFAULT_SUBSCRIPTION_PLANS,
        )
        # Keep the legacy column for existing databases, but normalize it to zero and never enforce it.
        await db.execute("UPDATE subscription_plans SET offers_per_day=0")
        await db.execute("CREATE TABLE IF NOT EXISTS parser_usage (user_id INTEGER NOT NULL, usage_date TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,usage_date))")
        await db.execute("CREATE TABLE IF NOT EXISTS group_bonus_claims (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, group_id INTEGER NOT NULL, amount INTEGER NOT NULL, claimed_at TEXT NOT NULL)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_group_bonus_claims_user_time ON group_bonus_claims(user_id,claimed_at)")
        await db.execute("CREATE TABLE IF NOT EXISTS business_connections(connection_id TEXT PRIMARY KEY,owner_user_id INTEGER NOT NULL,dc_id INTEGER NOT NULL,can_reply INTEGER NOT NULL DEFAULT 0,is_enabled INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_business_connections_owner ON business_connections(owner_user_id,is_enabled)")
        await db.execute("""CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, buyer_id INTEGER NOT NULL, buyer_username TEXT,
            receiver_username TEXT NOT NULL, gift_url TEXT NOT NULL, amount INTEGER NOT NULL,
            nft_title TEXT, nft_number TEXT,
            seller_id INTEGER, seller_username TEXT, proof_url TEXT, proof_photo_file_id TEXT, status TEXT NOT NULL,
            dispute_reason TEXT, virtual_reserve INTEGER NOT NULL DEFAULT 0, rejected_by INTEGER,
            business_connection_id TEXT, business_chat_id INTEGER, business_command_id INTEGER, business_message_id INTEGER,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        cur = await db.execute("PRAGMA table_info(offers)")
        offer_columns = {row[1] for row in await cur.fetchall()}
        if "dispute_reason" not in offer_columns:
            await db.execute("ALTER TABLE offers ADD COLUMN dispute_reason TEXT")
        if "virtual_reserve" not in offer_columns:
            await db.execute("ALTER TABLE offers ADD COLUMN virtual_reserve INTEGER NOT NULL DEFAULT 0")
        if "rejected_by" not in offer_columns:
            await db.execute("ALTER TABLE offers ADD COLUMN rejected_by INTEGER")
        if "proof_photo_file_id" not in offer_columns:
            await db.execute("ALTER TABLE offers ADD COLUMN proof_photo_file_id TEXT")
        if "nft_title" not in offer_columns:
            await db.execute("ALTER TABLE offers ADD COLUMN nft_title TEXT")
        if "nft_number" not in offer_columns:
            await db.execute("ALTER TABLE offers ADD COLUMN nft_number TEXT")
        if "business_connection_id" not in offer_columns:
            await db.execute("ALTER TABLE offers ADD COLUMN business_connection_id TEXT")
        if "business_chat_id" not in offer_columns:
            await db.execute("ALTER TABLE offers ADD COLUMN business_chat_id INTEGER")
        if "business_command_id" not in offer_columns:
            await db.execute("ALTER TABLE offers ADD COLUMN business_command_id INTEGER")
        if "business_message_id" not in offer_columns:
            await db.execute("ALTER TABLE offers ADD COLUMN business_message_id INTEGER")
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_offer_business_command ON offers(business_connection_id,business_command_id) WHERE business_connection_id IS NOT NULL AND business_command_id IS NOT NULL")
        await db.execute("CREATE TABLE IF NOT EXISTS nft_cache (slug TEXT PRIMARY KEY, title TEXT NOT NULL, gift_number TEXT NOT NULL, image_url TEXT NOT NULL, image_path TEXT, attributes_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL)")
        nft_cache_columns = {row[1] for row in await (await db.execute("PRAGMA table_info(nft_cache)")).fetchall()}
        if "attributes_json" not in nft_cache_columns:
            await db.execute("ALTER TABLE nft_cache ADD COLUMN attributes_json TEXT NOT NULL DEFAULT '{}'")
        await db.execute("""CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, username TEXT,
            method TEXT NOT NULL, purpose TEXT NOT NULL, amount_units INTEGER NOT NULL,
            crypto_invoice_id TEXT, operation_id TEXT, status TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS admin_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL,
            entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, note TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS balance_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            delta_available INTEGER NOT NULL, delta_reserved INTEGER NOT NULL,
            event TEXT NOT NULL, reference_id INTEGER, actor_id INTEGER,
            note TEXT, created_at TEXT NOT NULL
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS admin_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL,
            target_user_id INTEGER, action TEXT NOT NULL, details TEXT,
            created_at TEXT NOT NULL
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, offer_id INTEGER NOT NULL,
            rater_id INTEGER NOT NULL, target_user_id INTEGER NOT NULL,
            score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 5), created_at TEXT NOT NULL,
            UNIQUE(offer_id,rater_id)
        )""")
        await db.commit()


def create_database_backup() -> Path:
    source_path = Path(DB_PATH)
    backup_dir = source_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_local().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"offers_backup_{stamp}.sqlite3"
    with sqlite3.connect(source_path) as source, sqlite3.connect(backup_path) as destination:
        source.backup(destination)
    cutoff = datetime.now().timestamp() - BACKUP_RETENTION_DAYS * 86400
    for old_backup in backup_dir.glob("offers_backup_*.sqlite3"):
        if old_backup != backup_path and old_backup.stat().st_mtime < cutoff:
            old_backup.unlink(missing_ok=True)
    return backup_path


async def backup_loop():
    while True:
        try:
            path = await asyncio.to_thread(create_database_backup)
            logging.info("Database backup created: %s", path.name)
        except Exception:
            logging.exception("Database backup failed")
        await asyncio.sleep(24 * 60 * 60)


async def ensure_user(user):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""INSERT INTO users(user_id,username,created_at) VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username""",
            (user.id, user.username, now_local().isoformat()))
        await db.commit()


async def row_one(query, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        return await cur.fetchone()


async def rows_all(query, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        return await cur.fetchall()



def offer_card_signature(offer_id: int) -> str:
    return hmac.new(BOT_TOKEN.encode(), f"offer-card:{offer_id}".encode(), hashlib.sha256).hexdigest()[:32]


async def serve_offer_card(request: web.Request):
    try:
        offer_id = int(request.match_info["offer_id"])
    except (KeyError, ValueError):
        raise web.HTTPNotFound()
    supplied = request.match_info.get("signature", "")
    if not hmac.compare_digest(supplied, offer_card_signature(offer_id)):
        raise web.HTTPNotFound()
    path = OFFER_CARD_DIR / f"{offer_id}.jpg"
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


async def serve_nft_photo(request: web.Request):
    slug = request.match_info.get("slug", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", slug):
        raise web.HTTPNotFound()
    row = await row_one("SELECT image_path,image_url FROM nft_cache WHERE slug=?", (slug,))
    if not row:
        raise web.HTTPNotFound()
    if row["image_path"]:
        path = Path(row["image_path"]).resolve()
        try:
            path.relative_to(NFT_PHOTO_DIR.resolve())
        except ValueError:
            raise web.HTTPNotFound()
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
            return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})
    # If the local download missed, proxy only Telegram's public CDN image for this cached NFT.
    image_url = row["image_url"] or ""
    parsed = urlparse(image_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "telesco.pe" or host.endswith(".telesco.pe")):
        raise web.HTTPNotFound()
    timeout = aiohttp.ClientTimeout(total=5, connect=2, sock_read=3)
    try:
        image_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
            "Accept": "image/jpeg,image/png,image/*,*/*;q=0.8",
        }
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(image_url, headers=image_headers, allow_redirects=True) as response:
                final_host = (response.url.host or "").lower()
                if response.status != 200 or not (final_host == "telesco.pe" or final_host.endswith(".telesco.pe")):
                    raise web.HTTPNotFound()
                data = await response.content.read(8_000_001)
                if len(data) > 8_000_000:
                    raise web.HTTPNotFound()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise web.HTTPServiceUnavailable()
    if data.startswith(b"\xff\xd8\xff"):
        content_type = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        content_type = "image/png"
    else:
        raise web.HTTPNotFound()
    return web.Response(body=data, content_type=content_type, headers={"Cache-Control": "public, max-age=86400"})


async def start_card_web_server():
    global CARD_WEB_RUNNER
    if not PUBLIC_BASE_URL:
        logging.warning("Public Railway domain is not configured; inline NFT images use Telegram's CDN directly")
        return
    app = web.Application(client_max_size=1_000_000)
    app.router.add_get("/offer-card/{offer_id:\\d+}/{signature}.jpg", serve_offer_card)
    app.router.add_get("/nft-photo/{slug}.jpg", serve_nft_photo)
    CARD_WEB_RUNNER = web.AppRunner(app, access_log=None)
    await CARD_WEB_RUNNER.setup()
    await web.TCPSite(CARD_WEB_RUNNER, host="0.0.0.0", port=CARD_SERVER_PORT).start()
    logging.info("Offer image endpoint listening on 0.0.0.0:%s; public URL %s", CARD_SERVER_PORT, PUBLIC_BASE_URL)


async def fetch_nft_metadata(raw_link: str):
    """Fetch public Telegram NFT OG metadata safely; cache metadata and a local photo copy."""
    normalized = normalize_nft_link(raw_link)
    if not normalized:
        return None
    canonical, slug, expected_number = normalized
    cached = await row_one("SELECT * FROM nft_cache WHERE slug=?", (slug,))
    if cached:
        try:
            checked = datetime.fromisoformat(cached["updated_at"])
            image_path = Path(cached["image_path"]) if cached["image_path"] else None
            image_exists = bool(image_path and image_path.is_file())
            if now_local() - checked < timedelta(hours=24) and image_exists:
                try:
                    attributes = json.loads(cached["attributes_json"] or "{}")
                except (KeyError, TypeError, json.JSONDecodeError):
                    attributes = {}
                return {"title": cached["title"], "number": cached["gift_number"],
                        "image_url": cached["image_url"], "image_path": str(image_path),
                        "attributes": attributes}
        except (OSError, TypeError, ValueError):
            pass

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    image_headers = {"User-Agent": headers["User-Agent"], "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}
    timeout = aiohttp.ClientTimeout(total=4.5, connect=2.5, sock_read=3.5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(canonical, headers=headers, allow_redirects=True) as response:
                final_host = (response.url.host or "").lower()
                if response.status != 200 or final_host != "t.me":
                    return None
                page = await response.content.read(1_000_001)
                if len(page) > 1_000_000:
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError):
            return None

        parser = OpenGraphParser()
        try:
            parser.feed(page.decode("utf-8", "replace"))
        except Exception:
            return None
        raw_title = html.unescape(parser.values.get("og:title") or parser.values.get("twitter:title", "")).strip()
        image_url = html.unescape(parser.values.get("og:image", "")).strip()
        attributes = parsed_nft_attributes(parser)
        title_match = re.fullmatch(r"(.+?)\s*#\s*([0-9]+)", raw_title)
        if not title_match or raw_title.lower().startswith("telegram"):
            return None
        title, gift_number = title_match.group(1).strip(), title_match.group(2)
        if not title or gift_number != expected_number:
            return None
        try:
            image_parsed = urlparse(image_url)
            image_host = (image_parsed.hostname or "").lower()
            if image_parsed.scheme != "https" or not (image_host == "telesco.pe" or image_host.endswith(".telesco.pe")):
                return None
        except ValueError:
            return None

        local_image = None
        try:
            async with session.get(image_url, headers=image_headers, allow_redirects=True,
                                   timeout=aiohttp.ClientTimeout(total=2.0, connect=1.2, sock_read=1.5)) as response:
                final_host = (response.url.host or "").lower()
                if response.status == 200 and (final_host == "telesco.pe" or final_host.endswith(".telesco.pe")):
                    length = response.headers.get("Content-Length")
                    if not length or int(length) <= 8_000_000:
                        image_data = await response.content.read(8_000_001)
                        if len(image_data) <= 8_000_000:
                            if image_data.startswith(b"\xff\xd8\xff"):
                                extension = ".jpg"
                            elif image_data.startswith(b"\x89PNG\r\n\x1a\n"):
                                extension = ".png"
                            else:
                                extension = None
                            if extension:
                                NFT_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
                                destination = NFT_PHOTO_DIR / f"{slug}{extension}"
                                await asyncio.to_thread(destination.write_bytes, image_data)
                                local_image = str(destination)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            logging.info("NFT photo cache miss for %s; using the public preview URL", slug)

    now = now_local().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO nft_cache(slug,title,gift_number,image_url,image_path,attributes_json,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(slug) DO UPDATE SET title=excluded.title,gift_number=excluded.gift_number,image_url=excluded.image_url,image_path=excluded.image_path,attributes_json=excluded.attributes_json,updated_at=excluded.updated_at",
            (slug, title, gift_number, image_url, local_image, json.dumps(attributes, ensure_ascii=False), now),
        )
        await db.commit()
    return {"title": title, "number": gift_number, "image_url": image_url, "image_path": local_image,
            "attributes": attributes}


async def ledger_entry(db, user_id, delta_available, delta_reserved, event, reference_id=None, actor_id=None, note=None):
    await db.execute(
        "INSERT INTO balance_ledger(user_id,delta_available,delta_reserved,event,reference_id,actor_id,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (user_id, delta_available, delta_reserved, event, reference_id, actor_id, note, now_local().isoformat()),
    )


async def log_admin_action(db, admin_id, target_user_id, action, details=""):
    await db.execute(
        "INSERT INTO admin_actions(admin_id,target_user_id,action,details,created_at) VALUES(?,?,?,?,?)",
        (admin_id, target_user_id, action, details, now_local().isoformat()),
    )


async def admin_notify(text, reply_markup=None):
    for admin_id in tuple(ADMIN_IDS):
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            logging.exception("Could not notify admin %s", admin_id)


async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def is_blocked(user_id: int) -> bool:
    row = await row_one("SELECT blocked FROM users WHERE user_id=?", (user_id,))
    return bool(row and row["blocked"])


class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user") or getattr(event, "from_user", None)
        if user and user.id not in ADMIN_IDS:
            row = await row_one("SELECT blocked FROM users WHERE user_id=?", (user.id,))
            if row and row["blocked"]:
                if isinstance(event, CallbackQuery):
                    await event.answer("Доступ ограничен. Для связи: @walooon", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("⛔ Доступ к сервису ограничен. Поддержка: @walooon")
                elif isinstance(event, InlineQuery):
                    await event.answer([], cache_time=0, is_personal=True)
                return
        return await handler(event, data)


async def get_subscription_plan(user_id: int):
    user = await row_one("SELECT subscription_plan,premium_until,unlimited_parser FROM users WHERE user_id=?", (user_id,))
    active_code = "free"
    if user and user["premium_until"]:
        try:
            if datetime.fromisoformat(user["premium_until"]) > now_local():
                active_code = user["subscription_plan"] or "plus"
        except (TypeError, ValueError):
            pass
    plan = await row_one("SELECT * FROM subscription_plans WHERE code=? AND enabled=1", (active_code,))
    if not plan:
        plan = await row_one("SELECT * FROM subscription_plans WHERE code='free'")
    if not plan:
        return {"code": "free", "name": "Бесплатный", "price_units": 0, "duration_days": 0,
                "offers_per_day": 0, "parser_per_day": 3, "enabled": 1}
    return dict(plan)


async def premium_active(user_id: int) -> bool:
    plan = await get_subscription_plan(user_id)
    return plan.get("code") != "free"


async def can_purchase_plan(user_id: int, plan_code: str) -> tuple[bool, str]:
    current = await get_subscription_plan(user_id)
    user = await row_one("SELECT premium_until FROM users WHERE user_id=?", (user_id,))
    if user and user["premium_until"] and current["code"] != "free" and current["code"] != plan_code:
        return False, "Текущий тариф ещё действует. Сейчас можно продлить только его; сменить тариф получится после окончания срока."
    cutoff = (now_local() - timedelta(minutes=45)).isoformat()
    pending = await row_one(
        "SELECT id FROM payments WHERE user_id=? AND purpose LIKE 'sub:%' AND (status='pending_review' OR (status IN ('creating','pending','awaiting_operation_id') AND created_at>=?)) LIMIT 1",
        (user_id, cutoff))
    if pending:
        return False, f"Уже есть неоплаченная заявка на тариф (№{pending['id']}). Сначала дождись проверки или свяжись с поддержкой."
    return True, ""


async def parser_used_today(user_id: int) -> int:
    row = await row_one("SELECT count FROM parser_usage WHERE user_id=? AND usage_date=?", (user_id, now_local().date().isoformat()))
    return int(row["count"]) if row else 0


async def consume_parser_quota(user_id: int) -> tuple[bool, int, int]:
    plan = await get_subscription_plan(user_id)
    limit = int(plan["parser_per_day"])
    user = await row_one("SELECT unlimited_parser FROM users WHERE user_id=?", (user_id,))
    if user_id in ADMIN_IDS or (user and user["unlimited_parser"]):
        return True, await parser_used_today(user_id), limit
    today = now_local().date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT count FROM parser_usage WHERE user_id=? AND usage_date=?", (user_id, today))
        row = await cur.fetchone()
        used = int(row[0]) if row else 0
        if used >= limit:
            await db.rollback()
            return False, used, limit
        await db.execute("INSERT INTO parser_usage(user_id,usage_date,count) VALUES(?,?,1) ON CONFLICT(user_id,usage_date) DO UPDATE SET count=count+1", (user_id, today))
        await db.commit()
    return True, used + 1, limit


async def activate_subscription(db, user_id: int, plan_code: str, days_override: int | None = None):
    plan = await (await db.execute("SELECT * FROM subscription_plans WHERE code=?", (plan_code,))).fetchone()
    if not plan:
        plan = await (await db.execute("SELECT * FROM subscription_plans WHERE code='plus'")).fetchone()
        plan_code = "plus"
    if not plan:
        raise RuntimeError("Subscription plan configuration is missing")
    cur = await db.execute("SELECT subscription_plan,premium_until FROM users WHERE user_id=?", (user_id,))
    user = await cur.fetchone()
    now = now_local()
    base = now
    try:
        existing_until = datetime.fromisoformat(user[1]) if user and user[1] else None
        if existing_until and existing_until > now and (user[0] or "plus") == plan_code:
            base = existing_until
    except (TypeError, ValueError):
        pass
    days = max(1, int(days_override if days_override is not None else plan[3]))
    until = (base + timedelta(days=days)).isoformat()
    await db.execute("UPDATE users SET subscription_plan=?,premium_until=? WHERE user_id=?", (plan_code, until, user_id))
    return plan, until


async def crypto_api(method: str, payload: dict):
    if not CRYPTOPAY_TOKEN:
        raise RuntimeError("Crypto Pay token is not configured")
    headers = {"Crypto-Pay-API-Token": CRYPTOPAY_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.post(f"https://pay.crypt.bot/api/{method}", json=payload, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 300 or not data.get("ok"):
                raise RuntimeError(f"Crypto Pay API error: {data}")
            return data.get("result")


async def create_crypto_payment(user, purpose: str, amount_units: int, chat_id: int):
    if not CRYPTOPAY_TOKEN:
        await bot.send_message(chat_id, "Crypto Pay пока не подключён: администратор должен добавить CRYPTOPAY_TOKEN в .env.")
        return
    if USDT_PER_STAR <= 0:
        await bot.send_message(chat_id, "Crypto Pay пока не настроен: администратор должен задать положительный курс USDT_PER_STAR в .env.")
        return
    usdt = (USDT_PER_STAR * Decimal(amount_units)).quantize(Decimal("0.01"))
    payment_id = None
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO payments(user_id,username,method,purpose,amount_units,status,created_at,updated_at) VALUES(?,?,?,?,?,'creating',?,?)",
            (user.id, user.username, "cryptopay", purpose, amount_units, now_local().isoformat(), now_local().isoformat()),
        )
        payment_id = cur.lastrowid
        await db.commit()
    try:
        invoice = await crypto_api("createInvoice", {
            "asset": "USDT", "amount": str(usdt),
            "description": f"NFT Offer Bot — {purpose} #{payment_id}",
            "payload": f"nftoffer:{payment_id}:{user.id}:{purpose}",
            "allow_comments": False, "allow_anonymous": False, "expires_in": 1800,
        })
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE payments SET crypto_invoice_id=?,status='pending',updated_at=? WHERE id=?",
                             (str(invoice["invoice_id"]), now_local().isoformat(), payment_id))
            await db.commit()
        link = invoice.get("bot_invoice_url") or invoice.get("pay_url")
        await bot.send_message(chat_id,
            f"💎 Счёт на <b>{usdt} USDT</b> создан для {amount_units} ⭐. Он действует 30 минут.",
            reply_markup=kb([[InlineKeyboardButton(text="💳 Оплатить Crypto Pay", url=link, style="success")]]),
            parse_mode="HTML")
        await admin_notify(f"🧾 Создан Crypto Pay-счёт №{payment_id} для @{html.escape(user.username or str(user.id))}: {usdt} USDT / {amount_units} ⭐.")
    except Exception as exc:
        logging.exception("Could not create Crypto Pay invoice")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE payments SET status='error',updated_at=? WHERE id=?", (now_local().isoformat(), payment_id))
            await db.commit()
        await bot.send_message(chat_id, "Не получилось создать Crypto Pay-счёт. Попробуйте позже или выберите Stars.")


async def apply_paid_payment(payment_id: int):
    activation_message = "✅ Оплата подтверждена. Подписка активирована."
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT * FROM payments WHERE id=?", (payment_id,))
        payment = await cur.fetchone()
        if not payment or payment["status"] == "paid":
            await db.rollback()
            return False
        if payment["status"] not in ("pending", "creating"):
            await db.rollback()
            return False
        await db.execute("UPDATE payments SET status='paid',updated_at=? WHERE id=?", (now_local().isoformat(), payment_id))
        if payment["purpose"] == "topup":
            await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (payment["amount_units"], payment["user_id"]))
            await ledger_entry(db, payment["user_id"], payment["amount_units"], 0, "crypto_topup", payment_id)
        elif payment["purpose"].startswith("sub:"):
            plan_code = payment["purpose"].split(":", 1)[1]
            plan, until = await activate_subscription(db, payment["user_id"], plan_code)
            activation_message = f"✅ Тариф «{plan[1]}» активирован до {until[:10]}."
        elif payment["purpose"] == "premium":
            plan, until = await activate_subscription(db, payment["user_id"], "plus", PREMIUM_DAYS)
            activation_message = f"✅ Тариф «{plan[1]}» активирован до {until[:10]}."
        await db.commit()
    try:
        if payment["purpose"] == "topup":
            await bot.send_message(payment["user_id"], f"✅ Оплата подтверждена. На баланс начислено {payment['amount_units']} ⭐-единиц.")
        else:
            await bot.send_message(payment["user_id"], activation_message)
    except Exception:
        pass
    await admin_notify(f"✅ Платёж №{payment_id} подтверждён автоматически: user_id={payment['user_id']}, {payment['amount_units']} ⭐, {payment['purpose']}.")
    return True


async def crypto_poll_loop():
    while True:
        try:
            pending = await rows_all("SELECT id,crypto_invoice_id FROM payments WHERE method='cryptopay' AND status='pending' AND crypto_invoice_id IS NOT NULL")
            for p in pending:
                try:
                    result = await crypto_api("getInvoices", {"invoice_ids": p["crypto_invoice_id"]})
                    items = result.get("items", []) if isinstance(result, dict) else result
                    if items and items[0].get("status") == "paid":
                        await apply_paid_payment(p["id"])
                except Exception:
                    logging.exception("Could not check Crypto Pay invoice %s", p["crypto_invoice_id"])
        except Exception:
            logging.exception("Crypto Pay poll loop failed")
        await asyncio.sleep(20)


async def maintenance_loop():
    while True:
        try:
            now = now_local()
            open_cutoff = (now - timedelta(hours=OFFER_TTL_HOURS)).isoformat()
            expired = await rows_all("SELECT id,buyer_id,status FROM offers WHERE status IN ('open','draft') AND created_at<?", (open_cutoff,))
            if expired:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("BEGIN IMMEDIATE")
                    for item in expired:
                        await db.execute("UPDATE offers SET status='expired',updated_at=? WHERE id=? AND status IN ('open','draft')", (now.isoformat(), item["id"]))
                    await db.commit()
                for item in expired:
                    if item["status"] == "open":
                        try:
                            await bot.send_message(item["buyer_id"], f"⌛ Оффер #{item['id']} истёк без принятия. Резерв не создавался.")
                        except Exception:
                            pass
            stuck_cutoff = (now - timedelta(minutes=60)).isoformat()
            old_items = await rows_all("SELECT id,buyer_id,seller_id,status,amount FROM offers WHERE status IN ('awaiting_gift','review_pending','awaiting_payout') AND updated_at<?", (stuck_cutoff,))
            recent_cutoff = (now - timedelta(hours=6)).isoformat()
            for item in old_items:
                already = await row_one("SELECT id FROM admin_actions WHERE action='stuck_offer_reminder' AND details=? AND created_at>=? LIMIT 1",
                                        (f"offer_id={item['id']}", recent_cutoff))
                if already:
                    continue
                await admin_notify(f"⏰ Сделка #{item['id']} зависла в статусе {html.escape(item['status'])} более часа; сумма {item['amount']} ⭐.")
                async with aiosqlite.connect(DB_PATH) as db:
                    await log_admin_action(db, 0, item["buyer_id"], "stuck_offer_reminder", f"offer_id={item['id']}")
                    await db.commit()
        except Exception:
            logging.exception("Offer maintenance loop failed")
        await asyncio.sleep(60)


@dp.message(CommandStart())
async def start(message: Message, command: CommandStart, state: FSMContext):
    await ensure_user(message.from_user)
    if await is_blocked(message.from_user.id):
        await message.answer("⛔ Доступ к сервису ограничен. Если это ошибка, напишите в поддержку.")
        return
    args = command.args or ""
    if args.startswith("s_") and args[2:].isdigit():
        offer_id = int(args[2:])
        row = await row_one("SELECT * FROM offers WHERE id=?", (offer_id,))
        if not row or row["seller_id"] != message.from_user.id or row["status"] != "awaiting_gift":
            await message.answer("Эта сделка уже изменила статус или ссылка недоступна.", reply_markup=main_menu(message.from_user.id))
            return
        if row["receiver_username"] != GIFT_CUSTODY_USERNAME:
            await message.answer(f"Эта сделка создана по старым правилам. Не отправляй NFT на указанный адрес; обратись к покупателю для создания нового оффера на @{GIFT_CUSTODY_USERNAME}.")
            return
        await state.update_data(offer_id=offer_id)
        await state.set_state(PaymentForm.seller_gift_link)
        await message.answer(
            f"📩 <b>Сделка №{offer_id}</b>\n\nОтправь NFT на @{html.escape(row['receiver_username'])}, затем пришли сюда ссылку на передачу. Фото не требуется.\n"
            "Администратор проверит ссылку.", parse_mode="HTML")
        return
    if args.startswith("d_") and args[2:].isdigit():
        offer_id = int(args[2:])
        row = await row_one("SELECT * FROM offers WHERE id=?", (offer_id,))
        if not row or row["status"] != "draft":
            await message.answer("Черновик оффера истёк или уже активирован.")
            return
        if row["receiver_username"] != GIFT_CUSTODY_USERNAME:
            await message.answer(f"Этот черновик создан по старым правилам. Создай новый оффер: подарок должен отправляться на @{GIFT_CUSTODY_USERNAME}.")
            return
        if OFFERS_PAUSED:
            await message.answer("⏸ Создание новых офферов временно приостановлено. Уже начатые сделки продолжаются.")
            return
        created = datetime.fromisoformat(row["created_at"])
        if now_local() - created > timedelta(hours=OFFER_TTL_HOURS):
            await message.answer("Ссылка на черновик устарела. Создай оффер заново.")
            return
        if row["buyer_id"] == message.from_user.id:
            await message.answer("Это твой черновик. Перешли inline-карточку продавцу; он активирует оффер, открыв эту ссылку.")
            return
        buyer = await row_one("SELECT balance FROM users WHERE user_id=?", (row["buyer_id"],))
        if not buyer:
            await message.answer("Покупатель не найден. Он должен открыть бота и нажать Start.")
            return
        if buyer["balance"] < row["amount"]:
            await message.answer("У покупателя пока недостаточно доступных ⭐. Он должен пополнить баланс перед активацией оффера.")
            try:
                await bot.send_message(row["buyer_id"], f"Для активации inline-оффера #{offer_id} нужно пополнить баланс минимум на {row['amount']} ⭐.", reply_markup=kb([[button("💰 Пополнить", "menu:wallet", "primary")]]))
            except Exception:
                pass
            return
        activated_at = now_local()
        cutoff = (activated_at - timedelta(hours=OFFER_TTL_HOURS)).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute("UPDATE offers SET status='open',virtual_reserve=0,created_at=?,updated_at=? WHERE id=? AND status='draft' AND created_at>=?", (activated_at.isoformat(), activated_at.isoformat(), offer_id, cutoff))
            if cur.rowcount != 1:
                await db.rollback()
                await message.answer("Черновик уже активирован другим продавцом или истёк.")
                return
            if row["buyer_id"] in ADMIN_IDS:
                await log_admin_action(db, row["buyer_id"], row["buyer_id"], "admin_inline_offer_activated",
                                       f"offer_id={offer_id}; amount={row['amount']}; activated_by={message.from_user.id}; balance_checked=1")
            await db.commit()
        row = await row_one("SELECT * FROM offers WHERE id=?", (offer_id,))
        await message.answer(offer_card(row), reply_markup=seller_accept_keyboard(offer_id), parse_mode="HTML", disable_web_page_preview=False)
        return
    if args.startswith("o_") and args[2:].isdigit():
        offer_id = int(args[2:])
        row = await row_one("SELECT * FROM offers WHERE id=?", (offer_id,))
        if not row or row["status"] != "open":
            await message.answer("Этот оффер уже недоступен.")
            return
        if row["receiver_username"] != GIFT_CUSTODY_USERNAME:
            await message.answer(f"Этот оффер создан по старым правилам. Попроси покупателя создать новый: NFT отправляется на @{GIFT_CUSTODY_USERNAME}.")
            return
        if row["business_connection_id"]:
            await message.answer("Этот оффер доступен только в исходном личном чате с покупателем.")
            return
        if row["buyer_id"] == message.from_user.id:
            await message.answer("Это твой оффер — перешли ссылку владельцу подарка.")
            return
        await message.answer(offer_card(row), reply_markup=seller_accept_keyboard(offer_id), parse_mode="HTML", disable_web_page_preview=False)
        return
    balance = await row_one("SELECT balance FROM users WHERE user_id=?", (message.from_user.id,))
    pause_line = "⏸ Сейчас создание новых офферов приостановлено.\n\n" if OFFERS_PAUSED else ""
    await message.answer(
        "🎁 <b>GIFT OFFER</b>\n"
        "Офферы на NFT-подарки — прямо в личном чате с продавцом.\n\n"
        f"{pause_line}"
        f"💰 Баланс GiftOffer: <b>{balance['balance']} ⭐</b>\n"
        "ℹ️ Это внутренний баланс сервиса; Telegram не блокирует Stars.\n\n"
        "Выбери действие в меню ниже.", reply_markup=main_menu(message.from_user.id), parse_mode="HTML")


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "ℹ️ <b>Как составить оффер</b>\n\n"
        "Подключи Offers-бота к профилю через Telegram → Настройки → Автоматизация чатов и разреши ему нужный личный чат. Затем прямо в чате с продавцом отправь:\n"
        "<code>/offer https://t.me/nft/ChillFlame-426591 250</code>\n\n"
        f"Подарок всегда отправляется на аккаунт сервиса <b>@{GIFT_CUSTODY_USERNAME}</b>; username получателя вводить не нужно.\n\n"
        "Сначала открой бота и нажми Start, чтобы получать статусы сделки. Цена — внутренние ⭐-единицы сервиса, это не перевод Stars в Telegram.\n\n"
        "🧩 Парсер доступен в главном меню; его дневная квота зависит от тарифа.\n\n"
        f"В профильной карточке продавец нажимает «✓ Принять» или «⊗ Отклонить». Принятый NFT передаётся только на @{GIFT_CUSTODY_USERNAME}; после передачи продавец подтверждает её кнопкой в том же чате. Администратор вручную проверяет получение и выполняет выплату.\n\n"
        "Если Telegram не даёт начать чат с человеком, у него могут быть ограничения на входящие сообщения. Иногда поможет, если он добавит тебя в контакты, или Telegram Premium. Premium не требуется всем и не отменяет ограничения аккаунта. Поддержка: @walooon.",
        parse_mode="HTML", reply_markup=main_menu(message.from_user.id))


def inline_notice(result_id: str, title: str, description: str, text: str):
    return InlineQueryResultArticle(
        id=result_id, title=title, description=description,
        input_message_content=InputTextMessageContent(message_text=text, parse_mode="HTML"),
    )


@dp.inline_query()
async def inline_create_offer(query: InlineQuery):
    await query.answer([inline_notice(
        "use_chat_automation", "Офферы теперь через /offer", "Подключи Offers-бота в Автоматизации чатов",
        "Inline Mode больше не создаёт офферы. Подключи этого бота через Telegram → Настройки → Автоматизация чатов, выбери разрешённый личный чат и отправь в нём: /offer https://t.me/nft/Название-123 250"
    )], cache_time=0, is_personal=True)


@dp.callback_query(F.data == "menu:new")
async def new_offer_start(callback: CallbackQuery):
    if await is_blocked(callback.from_user.id):
        await callback.answer("Доступ ограничен", show_alert=True)
        return
    if OFFERS_PAUSED:
        await callback.answer("⏸ Создание новых офферов временно приостановлено.", show_alert=True)
        return
    offer_username = "этого бота"
    try:
        username = (await bot.get_me()).username
        if username:
            offer_username = "@" + username
    except Exception:
        pass
    await callback.message.answer(
        "🎁 <b>Оффер в личном чате</b>\n\n"
        f"1. Подключи {html.escape(offer_username)} через Telegram → Настройки → Автоматизация чатов.\n"
        "2. Разреши ему отвечать в нужных личных чатах.\n"
        "3. В чате с продавцом отправь одной строкой:\n"
        "<code>/offer https://t.me/nft/Название-123 250</code>\n\n"
        f"Оффер увидит собеседник в этом же чате. NFT всегда отправляется на <b>@{GIFT_CUSTODY_USERNAME}</b>. Перед публикацией проверяется доступный баланс; продавец принимает или отклоняет предложение. Выплату после проверки ты подтверждаешь вручную.\n\n"
        "Для проверки баланса и пополнения открой основного бота и нажми Start.",
        parse_mode="HTML", reply_markup=kb([[button("↩️ Главное меню", "menu:home")]]))
    await callback.answer()


@dp.callback_query(F.data == "menu:wallet")
async def wallet(callback: CallbackQuery):
    userrow = await row_one("SELECT balance,reserved FROM users WHERE user_id=?", (callback.from_user.id,))
    topup_row = [button("⭐ Пополнить Stars", "topup:stars", "primary")]
    if crypto_pay_enabled():
        topup_row.append(button("💎 Crypto Pay", "topup:crypto", "success"))
    await callback.message.answer(
        f"💰 <b>ТВОЙ БАЛАНС</b>\n\n✅ Доступно: <b>{userrow['balance']} ⭐-единиц</b>\n🔒 В резерве: <b>{userrow['reserved']} ⭐</b>\n\nВыбери способ пополнения:",
        reply_markup=kb([
            topup_row,
            [button("⭐ Тарифы и парсер", "menu:premium", "primary")],
            [button("↩️ Главное меню", "menu:home")],
        ]), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "menu:premium")
async def premium_menu(callback: CallbackQuery):
    plans = await rows_all("SELECT * FROM subscription_plans WHERE enabled=1 ORDER BY sort_order")
    current = await get_subscription_plan(callback.from_user.id)
    user = await row_one("SELECT premium_until FROM users WHERE user_id=?", (callback.from_user.id,))
    expiry = user["premium_until"][:10] if user and user["premium_until"] and current["code"] != "free" else "не активна"
    lines = ["⭐ <b>ТАРИФЫ GIFT OFFER</b>", "", "Тарифы задают дневную квоту запросов парсера:"]
    rows = []
    for plan in plans:
        if plan["code"] == "free":
            lines.append(f"\n<b>{html.escape(plan['name'])}</b> · бесплатно\n🧩 {plan['parser_per_day']} запросов парсера/день")
            continue
        lines.append(f"\n<b>{html.escape(plan['name'])}</b> · {plan['price_units']} ⭐ / {plan['duration_days']} дней\n🧩 {plan['parser_per_day']} запросов парсера/день")
        rows.append([button(f"Выбрать «{plan['name']}» · {plan['price_units']} ⭐", f"planbuy:{plan['code']}", "primary")])
    lines.append(f"\nТекущий тариф: <b>{html.escape(current['name'])}</b> · до {html.escape(expiry)}")
    lines.append("\nПодписка оплачивается внутренними ⭐-единицами: перевод подтверждается вручную или через настроенный Crypto Pay.")
    rows.append([button("↩️ Назад", "menu:wallet")])
    await callback.message.answer("\n".join(lines), reply_markup=kb(rows), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("planbuy:"))
async def plan_buy_menu(callback: CallbackQuery):
    code = callback.data.split(":", 1)[1]
    plan = await row_one("SELECT * FROM subscription_plans WHERE code=? AND enabled=1 AND code!='free'", (code,))
    if not plan:
        await callback.answer("Этот тариф сейчас недоступен.", show_alert=True)
        return
    allowed, reason = await can_purchase_plan(callback.from_user.id, code)
    if not allowed:
        await callback.answer(reason, show_alert=True)
        return
    rows = [[button("⭐ Оплатить переводом Stars", f"paystars:sub:{code}", "primary")]]
    if crypto_pay_enabled():
        rows.append([button("💎 Оплатить Crypto Pay", f"paycrypto:sub:{code}", "success")])
    rows.append([button("↩️ К тарифам", "menu:premium")])
    await callback.message.answer(
        f"⭐ <b>Тариф «{html.escape(plan['name'])}»</b>\n\n"
        f"Стоимость: <b>{plan['price_units']} ⭐-единиц</b> · срок: {plan['duration_days']} дней\n"
        f"🧩 Запросов парсера в день: {plan['parser_per_day']}\n\n"
        "Выбери способ оплаты:", reply_markup=kb(rows), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "menu:home")
async def menu_home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("🏠 Главное меню", reply_markup=main_menu(callback.from_user.id))
    await callback.answer()


@dp.callback_query(F.data == "menu:profile")
async def my_profile(callback: CallbackQuery):
    await ensure_user(callback.from_user)
    user = await row_one("SELECT * FROM users WHERE user_id=?", (callback.from_user.id,))
    stats = await row_one(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN buyer_id=? THEN 1 ELSE 0 END) AS bought, "
        "SUM(CASE WHEN seller_id=? THEN 1 ELSE 0 END) AS sold, "
        "SUM(CASE WHEN status='paid' THEN 1 ELSE 0 END) AS completed, "
        "SUM(CASE WHEN status IN ('draft','open','awaiting_gift','review_pending','dispute','awaiting_payout') THEN 1 ELSE 0 END) AS active "
        "FROM offers WHERE buyer_id=? OR seller_id=?",
        (callback.from_user.id, callback.from_user.id, callback.from_user.id, callback.from_user.id),
    )
    rating = await row_one("SELECT AVG(score) AS avg_score,COUNT(*) AS count FROM ratings WHERE target_user_id=?", (callback.from_user.id,))
    total = stats["total"] or 0
    completed = stats["completed"] or 0
    progress = 0 if not total else min(10, round(10 * completed / total))
    bar = "🟩" * progress + "⬜" * (10 - progress)
    username = f"@{html.escape(callback.from_user.username)}" if callback.from_user.username else html.escape(callback.from_user.full_name or "Пользователь")
    rating_line = f"⭐ Рейтинг продавца: <b>{rating['avg_score']:.1f}/5</b> · {rating['count']} оценок" if rating and rating["count"] else "⭐ Рейтинг продавца: пока нет оценок"
    text = (
        "👤 <b>ТВОЙ ПРОФИЛЬ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{username} · ID <code>{callback.from_user.id}</code>\n\n"
        f"💰 Доступно: <b>{user['balance']} ⭐</b>\n"
        f"🔒 В резерве: <b>{user['reserved']} ⭐</b>\n"
        f"📦 Сделок всего: <b>{total}</b> · 🛍 Покупок: {stats['bought'] or 0} · 🎁 Продаж: {stats['sold'] or 0}\n"
        f"✅ Завершено: <b>{completed}</b> · ⏳ В процессе: {stats['active'] or 0}\n"
        f"{bar}\n{rating_line}\n\n"
        "Открой историю, чтобы посмотреть статус и детали каждой сделки."
    )
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb([
        [button("📋 Мои сделки", "menu:deals", "primary")],
        [button("💰 Баланс и пополнение", "menu:wallet")],
        [button("↩️ Главное меню", "menu:home")],
    ]))
    await callback.answer()


@dp.callback_query(F.data == "menu:id")
async def my_id(callback: CallbackQuery):
    username = f"@{html.escape(callback.from_user.username)}" if callback.from_user.username else "не задан"
    await callback.message.answer(
        f"🆔 <b>Твой Telegram ID:</b> <code>{callback.from_user.id}</code>\n"
        f"👤 Username: {username}\n\nСкопируй ID, если пишешь в поддержку {html.escape('@walooon')}.",
        parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "menu:help")
async def help_menu(callback: CallbackQuery):
    await callback.message.answer(
        "ℹ️ <b>Как работает GiftOffer</b>\n\n"
        "1. Нажми «🎁 Оферы» в главном меню, затем «💬 Выбрать личный чат» и выбери личную переписку с участником сделки. Группы не поддерживаются.\n"
        "2. Введи ссылку на NFT и цену: <code>https://t.me/nft/ChillFlame-426591 250</code>. Username в конец не добавляй.\n"
        f"NFT всегда отправляется на аккаунт сервиса @{GIFT_CUSTODY_USERNAME}. Выбранный чат — продавец; адрес назначения фиксированный.\n"
        "3. Выбери карточку GiftOffer и отправь её.\n"
        "4. Продавец нажимает «✓ Принять» или «⊗ Отклонить». При принятии бот проверяет и резервирует баланс покупателя.\n"
        "5. Если профильный оффер принят, продавец передаёт NFT на аккаунт сервиса и подтверждает передачу кнопкой в том же чате. Администратор проверяет её вручную.\n"
        "6. Администратор проверяет материалы, затем вручную отправляет Stars и отмечает выплату.\n\n"
        "🔒 Баланс в сервисе — внутренний учёт; Stars Telegram не блокируются.\n"
        "💬 Если Telegram не даёт начать чат с продавцом, у него могут быть ограничения на входящие сообщения. Иногда поможет Premium или добавление тебя в контакты; Premium не требуется всем и не снимает ограничения самого аккаунта.\n"
        "🛟 Поддержка: @walooon\n"
        "📖 Подробно о правилах и проверке сделки — нажми «Правила и безопасность».\n\n"
        "GiftOffer — независимый сервис, не официальный продукт Telegram.",
        parse_mode="HTML", reply_markup=kb([[button("📖 Правила и безопасность", "menu:rules", "primary")], [button("↩️ В меню", "menu:home")]]))
    await callback.answer()


@dp.callback_query(F.data == "menu:rules")
async def rules_menu(callback: CallbackQuery):
    await callback.message.answer(
        "🛡 <b>Прозрачность сделки</b>\n\n"
        "• У каждой сделки есть номер и зафиксированные статусы.\n"
        "• Баланс и резерв — внутренний учёт; Telegram не замораживает Stars на кошельке пользователя.\n"
        "• Перед принятием оффера проверяется доступный баланс покупателя. Если его не хватает, сделка не принимается.\n"
        "• После передачи подарка администратор вручную проверяет ссылку/получение и отправляет выплату через платёжный сервис.\n"
        "• Не отправляй пароль, код входа или секретные данные. Проверяй номер сделки и сумму.\n"
        "• По вопросам или спору: @walooon.\n\n"
        "Ни один текст не может гарантировать отсутствие риска. Проверяй условия и не передавай подарок, пока статус резерва не подтверждён.",
        parse_mode="HTML", reply_markup=kb([[button("🆔 Мой ID", "menu:id", "primary")], [button("↩️ В меню", "menu:home")]]))
    await callback.answer()


async def send_user_deals(chat_id: int, user_id: int, page: int = 0):
    page_size = 10
    total_row = await row_one("SELECT COUNT(*) AS n FROM offers WHERE buyer_id=? OR seller_id=?", (user_id, user_id))
    total = total_row["n"] if total_row else 0
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    items = await rows_all(
        "SELECT id,buyer_id,seller_id,amount,status,gift_url,nft_title,nft_number,created_at "
        "FROM offers WHERE buyer_id=? OR seller_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (user_id, user_id, page_size, page * page_size),
    )
    if not items:
        await bot.send_message(
            chat_id, "📋 <b>МОИ СДЕЛКИ</b>\n\nПока здесь пусто. Создай оффер или прими предложение, чтобы начать.",
            parse_mode="HTML", reply_markup=kb([[button("🎁 Создать оффер", "menu:new", "success")], [button("↩️ В профиль", "menu:profile")]]),
        )
        return
    labels = {
        "draft": "📝 Черновик", "open": "🟡 Ожидает продавца", "awaiting_gift": "📦 Ждём передачу",
        "review_pending": "🔎 На проверке", "dispute": "🆘 Спор", "awaiting_payout": "💸 Ожидает выплаты",
        "paid": "✅ Завершена", "refunded": "↩️ Возврат", "rejected": "❌ Отклонена", "expired": "⌛ Истекла",
    }
    cards = []
    for r in items:
        role = "Покупатель" if r["buyer_id"] == user_id else "Продавец"
        title = html.escape(r["nft_title"] or "NFT-подарок")
        number = html.escape(r["nft_number"] or "—")
        status = labels.get(r["status"], html.escape(r["status"]))
        date = html.escape((r["created_at"] or "")[:10])
        cards.append(
            f"<b>#{r['id']} · {title} #{number}</b>\n"
            f"{status} · {role} · <b>{r['amount']} ⭐</b> · {date}\n"
            f"<a href=\"{html.escape(r['gift_url'], quote=True)}\">Открыть NFT</a>"
        )
    text = f"📋 <b>МОИ СДЕЛКИ · {page + 1}/{pages}</b>\n━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(cards)
    navigation = []
    if page > 0:
        navigation.append(button("◀️ Назад", f"mydeals:{page - 1}"))
    if page + 1 < pages:
        navigation.append(button("Дальше ▶️", f"mydeals:{page + 1}"))
    rows = [navigation] if navigation else []
    rows.extend([[button("↩️ В профиль", "menu:profile")], [button("🏠 Главное меню", "menu:home")]])
    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb(rows), disable_web_page_preview=True)


@dp.callback_query(F.data == "menu:deals")
async def my_deals(callback: CallbackQuery):
    await send_user_deals(callback.message.chat.id, callback.from_user.id, 0)
    await callback.answer()


@dp.callback_query(F.data.startswith("mydeals:"))
async def my_deals_page(callback: CallbackQuery):
    raw_page = callback.data.split(":", 1)[1]
    if not raw_page.isdigit():
        await callback.answer("Неверная страница.", show_alert=True)
        return
    await send_user_deals(callback.message.chat.id, callback.from_user.id, int(raw_page))
    await callback.answer()


@dp.callback_query(F.data.startswith("accept:"))
async def accept_offer(callback: CallbackQuery):
    offer_id = int(callback.data.split(":")[1])
    photo_card = callback.data.endswith(":photo")
    await ensure_user(callback.from_user)
    insufficient = False
    invalid_reason = None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT * FROM offers WHERE id=?", (offer_id,))
        offer = await cur.fetchone()
        if not offer or offer["status"] not in ("draft", "open") or offer["buyer_id"] == callback.from_user.id:
            await db.rollback()
            await callback.answer("Оффер уже недоступен или это твой собственный оффер.", show_alert=True)
            return
        if offer["business_connection_id"]:
            await db.rollback()
            await callback.answer("Этот оффер доступен только в исходном личном чате.", show_alert=True)
            return
        if offer["receiver_username"] != GIFT_CUSTODY_USERNAME:
            await db.rollback()
            await callback.answer(f"Этот оффер устарел. Создай новый: NFT отправляется на @{GIFT_CUSTODY_USERNAME}.", show_alert=True)
            return
        if offer["status"] == "draft" and OFFERS_PAUSED:
            await db.rollback()
            await callback.answer("⏸ Создание новых офферов временно приостановлено.", show_alert=True)
            return
        try:
            created_at = datetime.fromisoformat(offer["created_at"])
        except (TypeError, ValueError):
            created_at = now_local() - timedelta(days=3650)
        if now_local() - created_at > timedelta(hours=OFFER_TTL_HOURS):
            await db.execute("UPDATE offers SET status='expired',updated_at=? WHERE id=? AND status IN ('draft','open')", (now_local().isoformat(), offer_id))
            await db.commit()
            invalid_reason = "Срок действия оффера истёк."
        else:
            cur = await db.execute("SELECT balance,blocked FROM users WHERE user_id=?", (offer["buyer_id"],))
            account = await cur.fetchone()
            if not account or account["blocked"]:
                await db.rollback()
                await callback.answer("Покупатель недоступен — сделка не принята.", show_alert=True)
                return
            # Never accept an offer without the full available balance, including legacy admin offers.
            use_virtual = False
            if account["balance"] < offer["amount"] and not use_virtual:
                await db.rollback()
                insufficient = True
            else:
                now = now_local().isoformat()
                cur = await db.execute(
                    "UPDATE offers SET seller_id=?,seller_username=?,status='awaiting_gift',virtual_reserve=?,updated_at=? WHERE id=? AND status=?",
                    (callback.from_user.id, callback.from_user.username, int(use_virtual), now, offer_id, offer["status"]),
                )
                if cur.rowcount != 1:
                    await db.rollback()
                    await callback.answer("Оффер уже обработан.", show_alert=True)
                    return
                if use_virtual:
                    await db.execute("UPDATE users SET reserved=reserved+? WHERE user_id=?", (offer["amount"], offer["buyer_id"]))
                    await ledger_entry(db, offer["buyer_id"], 0, offer["amount"], "virtual_offer_reserve", offer_id, callback.from_user.id,
                                       "Unbacked virtual reserve; not real Stars")
                else:
                    await db.execute("UPDATE users SET balance=balance-?,reserved=reserved+? WHERE user_id=?",
                                     (offer["amount"], offer["amount"], offer["buyer_id"]))
                    await ledger_entry(db, offer["buyer_id"], -offer["amount"], offer["amount"], "offer_reserve", offer_id, callback.from_user.id)
                await db.commit()
    if invalid_reason:
        await callback.answer(invalid_reason, show_alert=True)
        expired_text = f"⌛ <b>ОФФЕР №{offer_id} ИСТЁК</b>\n\nСрок действия предложения закончился. Создай новый оффер, если он всё ещё актуален."
        try:
            if callback.inline_message_id and photo_card:
                await bot.edit_message_caption(caption=expired_text, inline_message_id=callback.inline_message_id, parse_mode="HTML", reply_markup=None)
            elif callback.inline_message_id:
                await bot.edit_message_text(text=expired_text, inline_message_id=callback.inline_message_id, parse_mode="HTML", reply_markup=None)
            elif callback.message:
                await callback.message.edit_text(expired_text, parse_mode="HTML", reply_markup=None)
        except Exception:
            pass
        try:
            await bot.send_message(offer["buyer_id"], f"⌛ Оффер №{offer_id} истёк и больше не принимает продавцов.")
        except Exception:
            pass
        return
    if insufficient:
        await callback.answer("Недостаточно средств — принятие не сохранено, резерв не создан.", show_alert=True)
        if callback.message:
            await callback.message.answer(f"⚠️ Оффер №{offer_id} пока не принят: у покупателя недостаточно доступных средств.")
        try:
            await bot.send_message(offer["buyer_id"],
                f"⚠️ Для оффера №{offer_id} не хватило доступных ⭐-единиц ({offer['amount']} нужно). Пополни баланс и попроси продавца попробовать снова.",
                reply_markup=kb([[button("💰 Пополнить баланс", "menu:wallet", "primary")]]))
        except Exception:
            pass
        await admin_notify(f"⚠️ Оффер №{offer_id} не принят: покупателю не хватило баланса для резерва.")
        return

    seller_text = (
        f"✅ <b>ОФФЕР №{offer_id} ПРИНЯТ</b>\n\n"
        f"Покупатель зарезервировал <b>{offer['amount']} ⭐-единиц</b> во внутреннем балансе.\n\n"
        f"<b>Дальше:</b> отправь NFT на @{html.escape(offer['receiver_username'])}, затем пришли в бота ссылку на передачу. Фото не требуется.\n"
        "Администратор проверит ссылку; выплату продавцу отправляют вручную. Резерв — внутренний учёт, не блокировка Stars в Telegram."
    )
    if callback.inline_message_id:
        try:
            me = await bot.get_me()
            private_link = f"https://t.me/{me.username}?start=s_{offer_id}"
            accepted_markup = kb([[InlineKeyboardButton(text="✅ Подтвердить передачу в боте", url=private_link, style="primary")]])
            if photo_card:
                await bot.edit_message_caption(
                    caption=seller_text, inline_message_id=callback.inline_message_id, parse_mode="HTML",
                    reply_markup=accepted_markup)
            else:
                await bot.edit_message_text(
                    text=seller_text, inline_message_id=callback.inline_message_id, parse_mode="HTML",
                    reply_markup=accepted_markup)
        except Exception:
            logging.exception("Could not update inline offer message %s after acceptance", offer_id)
            await callback.answer("Принято, но карточку в чате обновить не удалось. Открой бота для продолжения.", show_alert=True)
    elif callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(seller_text,
            reply_markup=kb([[button("✅ Подтвердить передачу", f"sent:{offer_id}", "success")], [button("🆘 Проблема", f"dispute:{offer_id}", "danger")]]), parse_mode="HTML")
    reputation = await row_one("SELECT AVG(score) AS avg_score,COUNT(*) AS count FROM ratings WHERE target_user_id=?", (callback.from_user.id,))
    reputation_text = f" · рейтинг {reputation['avg_score']:.1f}/5 ({reputation['count']})" if reputation and reputation["count"] else " · пока без рейтинга"
    buyer_notice = f"✅ Продавец @{html.escape(callback.from_user.username or str(callback.from_user.id))} принял оффер №{offer_id}{reputation_text}. {offer['amount']} ⭐-единиц зарезервированы."
    try:
        await bot.send_message(offer["buyer_id"], buyer_notice, parse_mode="HTML", reply_markup=participant_dispute_keyboard(offer_id))
    except Exception:
        logging.info("Could not send buyer notification for offer %s", offer_id)
    await admin_notify(f"🔒 Оффер №{offer_id} принят; сумма {offer['amount']} ⭐; buyer_id={offer['buyer_id']}.")
    await callback.answer("Принято: средства зарезервированы")


@dp.callback_query(F.data.startswith("reject:"))
async def reject_offer(callback: CallbackQuery):
    offer_id = int(callback.data.split(":")[1])
    photo_card = callback.data.endswith(":photo")
    await ensure_user(callback.from_user)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT * FROM offers WHERE id=?", (offer_id,))
        offer = await cur.fetchone()
        if not offer or offer["status"] not in ("draft", "open") or offer["buyer_id"] == callback.from_user.id:
            await db.rollback()
            await callback.answer("Оффер уже обработан или это твой собственный оффер.", show_alert=True)
            return
        if offer["business_connection_id"]:
            await db.rollback()
            await callback.answer("Этот оффер доступен только в исходном личном чате.", show_alert=True)
            return
        cur = await db.execute("UPDATE offers SET status='rejected',rejected_by=?,updated_at=? WHERE id=? AND status=?", (callback.from_user.id, now_local().isoformat(), offer_id, offer["status"]))
        if cur.rowcount != 1:
            await db.rollback()
            await callback.answer("Оффер уже обработан.", show_alert=True)
            return
        await db.commit()
    rejected_text = (
        f"❌ <b>ОФФЕР №{offer_id} ОТКЛОНЁН</b>\n\n"
        f"Продавец @{html.escape(callback.from_user.username or str(callback.from_user.id))} отклонил предложение.\n"
        "Ничего не зарезервировано. Покупатель может создать новый оффер."
    )
    try:
        if callback.inline_message_id and photo_card:
            await bot.edit_message_caption(caption=rejected_text, inline_message_id=callback.inline_message_id, parse_mode="HTML", reply_markup=None)
        elif callback.inline_message_id:
            await bot.edit_message_text(text=rejected_text, inline_message_id=callback.inline_message_id, parse_mode="HTML", reply_markup=None)
        elif callback.message:
            await callback.message.edit_text(rejected_text, parse_mode="HTML", reply_markup=None)
    except Exception:
        logging.exception("Could not update rejected offer message %s", offer_id)
    try:
        await bot.send_message(offer["buyer_id"], f"❌ Продавец отклонил оффер №{offer_id}. Средства не резервировались.")
    except Exception:
        pass
    await callback.answer("Оффер отклонён")


@dp.callback_query(F.data.startswith("sent:"))
async def seller_sent(callback: CallbackQuery, state: FSMContext):
    offer_id = int(callback.data.split(":")[1])
    row = await row_one("SELECT * FROM offers WHERE id=?", (offer_id,))
    if not row or row["seller_id"] != callback.from_user.id or row["status"] != "awaiting_gift":
        await callback.answer("Кнопка недоступна.", show_alert=True)
        return
    if row["receiver_username"] != GIFT_CUSTODY_USERNAME:
        await callback.answer(f"Сделка создана по старым правилам. Создайте новый оффер на @{GIFT_CUSTODY_USERNAME}.", show_alert=True)
        return
    await state.update_data(offer_id=offer_id)
    await state.set_state(PaymentForm.seller_gift_link)
    await callback.message.answer(
        f"✅ Отправь NFT на @{html.escape(row['receiver_username'])}, затем пришли сюда ссылку на переданный подарок. "
        "Скриншот не нужен."
    )
    await callback.answer()


@dp.message(PaymentForm.seller_gift_link)
async def seller_link_received(message: Message, state: FSMContext):
    proof = (message.text or "").strip()
    if not re.match(r"^https://t\.me/", proof):
        await message.answer("Пришли ссылку на переданный NFT в формате https://t.me/...")
        return
    data = await state.get_data()
    offer_id = data.get("offer_id")
    row = await row_one("SELECT * FROM offers WHERE id=?", (offer_id,)) if offer_id else None
    if not row or row["seller_id"] != message.from_user.id or row["status"] != "awaiting_gift":
        await state.clear()
        await message.answer("Статус сделки уже изменился. Напиши в поддержку.")
        return
    if row["receiver_username"] != GIFT_CUSTODY_USERNAME:
        await state.clear()
        await message.answer(f"Эта сделка создана по старым правилам. Передача не подтверждена. Обратись к покупателю для нового оффера на @{GIFT_CUSTODY_USERNAME}.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE offers SET proof_url=?,status='review_pending',updated_at=? WHERE id=? AND seller_id=? AND status='awaiting_gift'",
            (proof, now_local().isoformat(), offer_id, message.from_user.id),
        )
        if cur.rowcount != 1:
            await db.rollback()
            await state.clear()
            await message.answer("Статус сделки уже изменился. Ссылка не принята — напиши в поддержку.")
            return
        await db.commit()
    await state.clear()
    await message.answer("📨 Ссылка получена и отправлена администратору на проверку.")
    admin_text = (
        f"🔎 <b>ПРОВЕРКА NFT · ОФФЕР #{offer_id}</b>\n\n"
        f"Покупатель: @{html.escape(row['buyer_username'] or str(row['buyer_id']))} · <code>{row['buyer_id']}</code>\n"
        f"Продавец: @{html.escape(row['seller_username'] or str(row['seller_id']))} · <code>{row['seller_id']}</code>\n"
        f"Сумма: <b>{row['amount']} ⭐</b>\nКуда отправлено: @{html.escape(row['receiver_username'])}\n"
        f"Оффер: {html.escape(row['gift_url'])}\nСсылка на передачу: {html.escape(proof)}\n\nСтатус: ожидает проверки"
    )
    await admin_notify(admin_text, admin_offer_keyboard(offer_id, "review"))


@dp.callback_query(F.data.startswith("dispute:"))
async def open_dispute(callback: CallbackQuery, state: FSMContext):
    offer_id = int(callback.data.split(":")[1])
    row = await row_one("SELECT * FROM offers WHERE id=?", (offer_id,))
    allowed_statuses = {"awaiting_gift", "review_pending", "awaiting_payout"}
    if not row or callback.from_user.id not in (row["buyer_id"], row["seller_id"]) or row["status"] not in allowed_statuses:
        await callback.answer("Спор по этой сделке открыть нельзя.", show_alert=True)
        return
    await state.update_data(dispute_offer_id=offer_id)
    await state.set_state(PaymentForm.dispute_reason)
    await callback.message.answer("🆘 Кратко опиши проблему. Резерв останется удержанным до решения администратора.")
    await callback.answer()


@dp.message(PaymentForm.dispute_reason)
async def save_dispute(message: Message, state: FSMContext):
    data = await state.get_data()
    offer_id = data.get("dispute_offer_id")
    reason = (message.text or "").strip()
    if len(reason) < 5:
        await message.answer("Опиши проблему чуть подробнее (минимум 5 символов).")
        return
    row = await row_one("SELECT * FROM offers WHERE id=?", (offer_id,))
    if not row or message.from_user.id not in (row["buyer_id"], row["seller_id"]) or row["status"] not in ("awaiting_gift", "review_pending", "awaiting_payout"):
        await state.clear()
        await message.answer("Статус сделки уже изменился. Напиши в поддержку @walooon.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE offers SET status='dispute',dispute_reason=?,updated_at=? WHERE id=?", (reason, now_local().isoformat(), offer_id))
        await db.commit()
    await state.clear()
    await message.answer(f"🆘 Спор по офферу #{offer_id} отправлен администратору. Резерв пока не возвращён.")
    admin_text = (
        f"🆘 <b>СПОР · ОФФЕР #{offer_id}</b>\n\n"
        f"Покупатель: @{html.escape(row['buyer_username'] or str(row['buyer_id']))} · <code>{row['buyer_id']}</code>\n"
        f"Продавец: @{html.escape(row['seller_username'] or str(row['seller_id']))} · <code>{row['seller_id']}</code>\n"
        f"Сумма: <b>{row['amount']} ⭐</b> · резерв удержан\n"
        f"Причина от user_id {message.from_user.id}: {html.escape(reason)}\n"
        f"NFT: {html.escape(row['gift_url'])}\nСсылка продавца: {html.escape(row['proof_url'] or 'пока не получена')}"
    )
    await admin_notify(admin_text, admin_dispute_keyboard(offer_id))
    for uid in (row["buyer_id"], row["seller_id"]):
        if uid != message.from_user.id:
            try:
                await bot.send_message(uid, f"🆘 Открыт спор по сделке #{offer_id}. Администратор проверит её; резерв остаётся удержанным до решения.")
            except Exception:
                pass


@dp.callback_query(F.data.startswith("giftok:"))
async def admin_gift_ok(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    offer_id = int(callback.data.split(":")[1])
    row = await row_one("SELECT * FROM offers WHERE id=?", (offer_id,))
    if not row or row["status"] != "review_pending":
        await callback.answer("Уже обработано.", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("UPDATE offers SET status='awaiting_payout',updated_at=? WHERE id=? AND status='review_pending'", (now_local().isoformat(), offer_id))
        if cur.rowcount != 1:
            await db.rollback()
            await callback.answer("Уже обработано.", show_alert=True)
            return
        await log_admin_action(db, callback.from_user.id, row["buyer_id"], "gift_verified", f"offer_id={offer_id}")
        await db.commit()
    await callback.message.edit_reply_markup(reply_markup=admin_offer_keyboard(offer_id, "payout"))
    seller_notice = f"✅ Подарок по офферу #{offer_id} проверен. Администратор вручную отправит {row['amount']} ⭐."
    buyer_notice = f"✅ Подарок по офферу #{offer_id} подтверждён. Администратор завершает выплату."
    await notify_offer_chat(row, seller_notice)
    for uid, text in ((row["seller_id"], seller_notice), (row["buyer_id"], buyer_notice)):
        try:
            await bot.send_message(uid, text)
        except Exception:
            logging.info("Could not notify user %s about gift verification", uid)
    await callback.answer("Подтверждено; сделка в списке невыплаченных")


@dp.callback_query(F.data.startswith("giftno:"))
async def admin_gift_no(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    offer_id = int(callback.data.split(":")[1])
    row = await row_one("SELECT * FROM offers WHERE id=?", (offer_id,))
    if not row or row["status"] != "review_pending":
        await callback.answer("Уже обработано.", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("UPDATE offers SET status='rejected',updated_at=? WHERE id=? AND status='review_pending'", (now_local().isoformat(), offer_id))
        if cur.rowcount != 1:
            await db.rollback()
            await callback.answer("Уже обработано.", show_alert=True)
            return
        if row["virtual_reserve"]:
            await db.execute("UPDATE users SET reserved=MAX(0,reserved-?) WHERE user_id=?", (row["amount"], row["buyer_id"]))
            await ledger_entry(db, row["buyer_id"], 0, -row["amount"], "virtual_reserve_released", offer_id, callback.from_user.id, "Unbacked virtual reserve released")
        else:
            await db.execute("UPDATE users SET balance=balance+?,reserved=MAX(0,reserved-?) WHERE user_id=?", (row["amount"], row["amount"], row["buyer_id"]))
            await ledger_entry(db, row["buyer_id"], row["amount"], -row["amount"], "offer_reserve_released", offer_id, callback.from_user.id, "Gift verification rejected")
        await log_admin_action(db, callback.from_user.id, row["buyer_id"], "gift_rejected", f"offer_id={offer_id}")
        await db.commit()
    await callback.message.edit_reply_markup(reply_markup=None)
    seller_notice = f"❌ Сделка #{offer_id} отклонена после проверки. Обратись в поддержку, если считаешь это ошибкой."
    await notify_offer_chat(row, seller_notice)
    refund_notice = (f"Сделка #{offer_id} отклонена; доступный баланс покупателя не менялся." if row["virtual_reserve"] else f"Сделка #{offer_id} отклонена; резерв {row['amount']} ⭐ возвращён на доступный баланс.")
    for uid, text in ((row["seller_id"], seller_notice), (row["buyer_id"], refund_notice)):
        try:
            await bot.send_message(uid, text)
        except Exception:
            logging.info("Could not notify user %s about rejected gift", uid)
    await callback.answer("Отклонено, резерв возвращён")


@dp.callback_query(F.data.startswith("disputeok:"))
async def admin_resolve_dispute_payout(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    offer_id = int(callback.data.split(":")[1])
    row = await row_one("SELECT * FROM offers WHERE id=?", (offer_id,))
    if not row or row["status"] != "dispute":
        await callback.answer("Спор уже обработан.", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("UPDATE offers SET status='awaiting_payout',updated_at=? WHERE id=? AND status='dispute'", (now_local().isoformat(), offer_id))
        if cur.rowcount != 1:
            await db.rollback()
            await callback.answer("Спор уже обработан.", show_alert=True)
            return
        await log_admin_action(db, callback.from_user.id, row["buyer_id"], "dispute_resolved_payout", f"offer_id={offer_id}")
        await db.commit()
    await callback.message.edit_reply_markup(reply_markup=admin_offer_keyboard(offer_id, "payout"))
    seller_notice = f"✅ Администратор разрешил спор по сделке #{offer_id} в пользу выплаты. Stars будут отправлены вручную."
    buyer_notice = f"Решение по спору #{offer_id}: подарок подтверждён, сделка перешла в очередь выплаты."
    await bot.send_message(row["seller_id"], seller_notice)
    await bot.send_message(row["buyer_id"], buyer_notice)
    await callback.answer("Переведено в очередь выплаты")


@dp.callback_query(F.data.startswith("disputerefund:"))
async def admin_resolve_dispute_refund(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    offer_id = int(callback.data.split(":")[1])
    row = await row_one("SELECT * FROM offers WHERE id=?", (offer_id,))
    if not row or row["status"] != "dispute":
        await callback.answer("Спор уже обработан.", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("UPDATE offers SET status='refunded',updated_at=? WHERE id=? AND status='dispute'", (now_local().isoformat(), offer_id))
        if cur.rowcount != 1:
            await db.rollback()
            await callback.answer("Спор уже обработан.", show_alert=True)
            return
        if row["virtual_reserve"]:
            await db.execute("UPDATE users SET reserved=MAX(0,reserved-?) WHERE user_id=?", (row["amount"], row["buyer_id"]))
            await ledger_entry(db, row["buyer_id"], 0, -row["amount"], "virtual_reserve_released", offer_id, callback.from_user.id, "Dispute closed; virtual reserve released")
        else:
            await db.execute("UPDATE users SET balance=balance+?,reserved=MAX(0,reserved-?) WHERE user_id=?", (row["amount"], row["amount"], row["buyer_id"]))
            await ledger_entry(db, row["buyer_id"], row["amount"], -row["amount"], "dispute_refund", offer_id, callback.from_user.id, row["dispute_reason"])
        await log_admin_action(db, callback.from_user.id, row["buyer_id"], "dispute_refund", f"offer_id={offer_id}")
        await db.commit()
    await callback.message.edit_reply_markup(reply_markup=None)
    refund_notice = (f"↩️ Спор по сделке #{offer_id} закрыт; доступный баланс покупателя не менялся." if row["virtual_reserve"] else f"↩️ Спор по сделке #{offer_id} решён: резерв {row['amount']} ⭐ возвращён на доступный баланс.")
    await bot.send_message(row["buyer_id"], refund_notice)
    await bot.send_message(row["seller_id"], f"Спор по сделке #{offer_id} закрыт администратором без выплаты.")
    await callback.answer("Резерв возвращён покупателю")


@dp.callback_query(F.data.startswith("paid:"))
async def admin_mark_paid(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    offer_id = int(callback.data.split(":")[1])
    row = await row_one("SELECT * FROM offers WHERE id=?", (offer_id,))
    if not row or row["status"] != "awaiting_payout":
        await callback.answer("Эта сделка не ожидает выплаты.", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("UPDATE offers SET status='paid',updated_at=? WHERE id=? AND status='awaiting_payout'", (now_local().isoformat(), offer_id))
        if cur.rowcount != 1:
            await db.rollback()
            await callback.answer("Выплата уже отмечена.", show_alert=True)
            return
        await db.execute("UPDATE users SET reserved=MAX(0,reserved-?) WHERE user_id=?", (row["amount"], row["buyer_id"]))
        await ledger_entry(db, row["buyer_id"], 0, -row["amount"], "seller_paid", offer_id, callback.from_user.id)
        await log_admin_action(db, callback.from_user.id, row["seller_id"], "payout_marked_paid", f"offer_id={offer_id}; amount={row['amount']}")
        await db.commit()
    await callback.message.edit_reply_markup(reply_markup=None)
    await notify_offer_chat(row, f"✅ Администратор отметил выплату по сделке #{offer_id} как выполненную. Проверь получение.")
    for uid in (row["seller_id"], row["buyer_id"]):
        try:
            await bot.send_message(uid, f"✅ Выплата по сделке #{offer_id} отмечена как выполненная.")
        except Exception:
            pass
    try:
        await bot.send_message(row["buyer_id"], f"⭐ Оцени продавца по сделке #{offer_id}:", reply_markup=rating_keyboard(offer_id))
    except Exception:
        pass
    await callback.answer("Отмечено выплаченным")


@dp.callback_query(F.data.startswith("rate:"))
async def rate_seller(callback: CallbackQuery):
    _, raw_offer_id, raw_score = callback.data.split(":")
    offer_id, score = int(raw_offer_id), int(raw_score)
    row = await row_one("SELECT * FROM offers WHERE id=?", (offer_id,))
    if not row or row["status"] != "paid" or row["buyer_id"] != callback.from_user.id or not 1 <= score <= 5:
        await callback.answer("Оценка недоступна.", show_alert=True)
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO ratings(offer_id,rater_id,target_user_id,score,created_at) VALUES(?,?,?,?,?)",
                             (offer_id, callback.from_user.id, row["seller_id"], score, now_local().isoformat()))
            await db.commit()
    except aiosqlite.IntegrityError:
        await callback.answer("Ты уже оценил эту сделку.", show_alert=True)
        return
    summary = await row_one("SELECT AVG(score) AS avg_score,COUNT(*) AS count FROM ratings WHERE target_user_id=?", (row["seller_id"],))
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Спасибо за оценку!")
    await admin_notify(f"⭐ Оффер #{offer_id}: продавец получил оценку {score}/5; рейтинг {summary['avg_score']:.1f}/5 по {summary['count']} сделкам.")


@dp.callback_query(F.data.startswith("topup:"))
async def topup_start(callback: CallbackQuery, state: FSMContext):
    method = callback.data.split(":")[1]
    if method == "stars":
        await state.set_state(PaymentForm.stars_amount)
        await state.update_data(purpose="topup")
        await callback.message.answer("⭐ Сколько внутренних ⭐-единиц пополнить? Введи целое число, минимум 1.")
    else:
        await state.set_state(PaymentForm.stars_amount)
        await state.update_data(purpose="topup_crypto")
        await callback.message.answer("💎 Сколько ⭐-единиц пополнить? Криптосумма будет рассчитана по курсу из настроек.")
    await callback.answer()


@dp.message(PaymentForm.stars_amount)
async def amount_for_payment(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not value.isdigit() or int(value) < 1:
        await message.answer("Введи положительное целое число.")
        return
    data = await state.get_data()
    purpose = data.get("purpose", "topup")
    if purpose == "topup_crypto":
        await state.clear()
        await create_crypto_payment(message.from_user, "topup", int(value), message.chat.id)
        return
    payment_id = await create_manual_payment(message.from_user, "topup", int(value))
    await state.update_data(payment_id=payment_id)
    await state.set_state(PaymentForm.operation_id)
    await message.answer(f"Переведи сумму в Stars на @{html.escape(STAR_RECEIVE_USERNAME)}. После перевода пришли ID операции. Заявка #{payment_id} попадёт администратору на проверку.", parse_mode="HTML")


async def create_manual_payment(user, purpose, amount):
    now = now_local().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT INTO payments(user_id,username,method,purpose,amount_units,status,created_at,updated_at) VALUES(?,?, 'stars_username',?,?, 'awaiting_operation_id',?,?)",
            (user.id, user.username, purpose, amount, now, now))
        payment_id = cur.lastrowid
        await db.commit()
    return payment_id


@dp.message(PaymentForm.operation_id)
async def operation_id_received(message: Message, state: FSMContext):
    operation_id = (message.text or "").strip()
    if operation_id.lower() in {"/cancel", "cancel", "отмена"}:
        data = await state.get_data()
        if data.get("payment_id"):
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE payments SET status='cancelled',updated_at=? WHERE id=? AND status='awaiting_operation_id'", (now_local().isoformat(), data["payment_id"]))
                await db.commit()
        await state.clear()
        await message.answer("Заявка на оплату отменена. Если ты уже перевёл Stars, сообщи поддержке.")
        return
    if len(operation_id) < 3 or len(operation_id) > 128:
        await message.answer("Пришли корректный ID операции.")
        return
    data = await state.get_data()
    payment_id = data["payment_id"]
    payment = await row_one("SELECT * FROM payments WHERE id=?", (payment_id,))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE payments SET operation_id=?,status='pending_review',updated_at=? WHERE id=?", (operation_id, now_local().isoformat(), payment_id))
        await db.commit()
    await state.clear()
    await message.answer("📨 Заявка отправлена на проверку. Баланс или подписка обновятся после подтверждения платежа.")
    text = (f"💳 <b>ПРОВЕРКА ОПЛАТЫ #{payment_id}</b>\n\n"
            f"Пользователь: @{html.escape(message.from_user.username or str(message.from_user.id))} · <code>{message.from_user.id}</code>\n"
            f"Способ: Stars на @{html.escape(STAR_RECEIVE_USERNAME)}\nСумма: {payment['amount_units']} ⭐-единиц\n"
            f"ID операции: <code>{html.escape(operation_id)}</code>\nНазначение: {payment['purpose']}")
    await admin_notify(text, admin_payment_keyboard(payment_id))


@dp.callback_query(F.data.startswith("paycancel:"))
async def cancel_subscription_payment(callback: CallbackQuery, state: FSMContext):
    payment_id = int(callback.data.split(":", 1)[1])
    payment = await row_one("SELECT user_id,status FROM payments WHERE id=?", (payment_id,))
    if not payment or payment["user_id"] != callback.from_user.id or payment["status"] != "awaiting_operation_id":
        await callback.answer("Заявку нельзя отменить этим действием.", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE payments SET status='cancelled',updated_at=? WHERE id=? AND status='awaiting_operation_id'", (now_local().isoformat(), payment_id))
        await db.commit()
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Заявка отменена")
    await callback.message.answer("Заявка отменена. Если перевод уже отправлен, свяжись с поддержкой.")


@dp.callback_query(F.data.startswith("paystars:"))
async def premium_stars(callback: CallbackQuery, state: FSMContext):
    purpose = callback.data.split(":", 1)[1]
    if purpose.startswith("sub:"):
        code = purpose.split(":", 1)[1]
        plan = await row_one("SELECT * FROM subscription_plans WHERE code=? AND enabled=1 AND code!='free'", (code,))
        if not plan:
            await callback.answer("Этот тариф недоступен.", show_alert=True)
            return
        allowed, reason = await can_purchase_plan(callback.from_user.id, code)
        if not allowed:
            await callback.answer(reason, show_alert=True)
            return
        payment_id = await create_manual_payment(callback.from_user, purpose, plan["price_units"])
        await state.update_data(payment_id=payment_id)
        await state.set_state(PaymentForm.operation_id)
        await callback.message.answer(
            f"⭐ Для тарифа «{html.escape(plan['name'])}» переведи {plan['price_units']} ⭐ на @{html.escape(STAR_RECEIVE_USERNAME)}.\n"
            f"Затем пришли ID операции. Доступ включится после проверки платежа. Заявка №{payment_id}.",
            parse_mode="HTML", reply_markup=kb([[button("✖️ Отменить заявку", f"paycancel:{payment_id}", "danger")]]))
        await callback.answer()
        return
    if purpose != "premium":
        await callback.answer("Неизвестный платёж.", show_alert=True)
        return
    payment_id = await create_manual_payment(callback.from_user, "premium", PREMIUM_PRICE_STARS)
    await state.update_data(payment_id=payment_id)
    await state.set_state(PaymentForm.operation_id)
    await callback.message.answer(f"⭐ Для подписки на {PREMIUM_DAYS} дней отправь {PREMIUM_PRICE_STARS} ⭐ на @{html.escape(STAR_RECEIVE_USERNAME)}, затем пришли ID операции.", parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("paycrypto:"))
async def premium_crypto(callback: CallbackQuery):
    purpose = callback.data.split(":", 1)[1]
    if purpose.startswith("sub:"):
        code = purpose.split(":", 1)[1]
        plan = await row_one("SELECT * FROM subscription_plans WHERE code=? AND enabled=1 AND code!='free'", (code,))
        if not plan:
            await callback.answer("Этот тариф недоступен.", show_alert=True)
            return
        allowed, reason = await can_purchase_plan(callback.from_user.id, code)
        if not allowed:
            await callback.answer(reason, show_alert=True)
            return
        await create_crypto_payment(callback.from_user, purpose, plan["price_units"], callback.message.chat.id)
    elif purpose == "premium":
        await create_crypto_payment(callback.from_user, "premium", PREMIUM_PRICE_STARS, callback.message.chat.id)
    elif purpose == "topup":
        await callback.message.answer("Для пополнения сначала выбери сумму через «Пополнить Crypto Pay» в разделе баланса.")
    await callback.answer()


@dp.callback_query(F.data.startswith("payok:"))
async def payment_approve(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    payment_id = int(callback.data.split(":")[1])
    payment = await row_one("SELECT * FROM payments WHERE id=?", (payment_id,))
    if not payment or payment["status"] != "pending_review":
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return
    activation_message = "✅ Оплата подтверждена. Доступ обновлён."
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("UPDATE payments SET status='paid',updated_at=? WHERE id=? AND status='pending_review'", (now_local().isoformat(), payment_id))
        if cur.rowcount != 1:
            await db.rollback()
            await callback.answer("Заявка уже обработана.", show_alert=True)
            return
        if payment["purpose"] == "topup":
            await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (payment["amount_units"], payment["user_id"]))
            await ledger_entry(db, payment["user_id"], payment["amount_units"], 0, "stars_username_topup", payment_id, callback.from_user.id)
        elif payment["purpose"].startswith("sub:"):
            plan_code = payment["purpose"].split(":", 1)[1]
            plan, until = await activate_subscription(db, payment["user_id"], plan_code)
            activation_message = f"✅ Тариф «{plan['name']}» активирован до {until[:10]}."
        elif payment["purpose"] == "premium":
            plan, until = await activate_subscription(db, payment["user_id"], "plus", PREMIUM_DAYS)
            activation_message = f"✅ Тариф «{plan['name']}» активирован до {until[:10]}."
        await log_admin_action(db, callback.from_user.id, payment["user_id"], "payment_approved", f"payment_id={payment_id}")
        await db.commit()
    await callback.message.edit_reply_markup(reply_markup=None)
    await bot.send_message(payment["user_id"], activation_message)
    await callback.answer("Оплата одобрена")


@dp.callback_query(F.data.startswith("payno:"))
async def payment_reject(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    payment_id = int(callback.data.split(":")[1])
    payment = await row_one("SELECT * FROM payments WHERE id=?", (payment_id,))
    if not payment or payment["status"] != "pending_review":
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("UPDATE payments SET status='rejected',updated_at=? WHERE id=? AND status='pending_review'", (now_local().isoformat(), payment_id))
        if cur.rowcount != 1:
            await db.rollback()
            await callback.answer("Заявка уже обработана.", show_alert=True)
            return
        await log_admin_action(db, callback.from_user.id, payment["user_id"], "payment_rejected", f"payment_id={payment_id}")
        await db.commit()
    await callback.message.edit_reply_markup(reply_markup=None)
    await bot.send_message(payment["user_id"], f"❌ Оплата #{payment_id} не подтверждена. Свяжись с поддержкой, если это ошибка.")
    await callback.answer("Отклонено")


@dp.callback_query(F.data.startswith("note:"))
async def note_start(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, entity_type, entity_id = callback.data.split(":")
    await state.update_data(entity_type=entity_type, entity_id=int(entity_id))
    await state.set_state(PaymentForm.admin_note)
    await callback.message.answer(f"📝 Напиши примечание для {entity_type} #{entity_id}.")
    await callback.answer()


@dp.message(PaymentForm.admin_note)
async def save_admin_note(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    note = (message.text or "").strip()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO admin_notes(admin_id,entity_type,entity_id,note,created_at) VALUES(?,?,?,?,?)",
                         (message.from_user.id, data["entity_type"], data["entity_id"], note, now_local().isoformat()))
        await db.commit()
    await state.clear()
    await message.answer("✅ Примечание сохранено.")


@dp.callback_query(F.data == "menu:admin")
async def admin_menu_callback(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await send_admin_panel(callback.message.chat.id)
    await callback.answer()


async def send_admin_settings(chat_id: int):
    free = await row_one("SELECT parser_per_day FROM subscription_plans WHERE code='free'")
    await bot.send_message(
        chat_id,
        "⚙️ <b>НАСТРОЙКИ СЕРВИСА</b>\n\n"
        f"Новые офферы: {'⏸ на паузе' if OFFERS_PAUSED else '🟢 принимаются'}\n"
        f"Срок оффера: {OFFER_TTL_HOURS} ч.\n"
        f"Бесплатный парсер: {free['parser_per_day']} запр./день\n"
        f"Crypto Pay: {'подключён' if crypto_pay_enabled() else 'не подключён'} · {USDT_PER_STAR} USDT/⭐\n\n"
        "Нажми кнопку, чтобы изменить нужную настройку.",
        reply_markup=kb([
            [button("🧩 Тарифы и квота парсера", "admin:plans", "primary")],
            [button("⏱ Срок оффера", "cfg:ttl")],
            [button("⏸ Пауза / запуск", "cfg:pause", "danger" if not OFFERS_PAUSED else "success")],
            [button("💎 Crypto Pay", "cfg:rate", "primary")],
            [button("💰 Пополнение баланса", "cfg:bonus", "primary")],
            [button("↩️ Назад", "menu:admin")],
        ]), parse_mode="HTML")


async def get_group_bonus_settings():
    rows = await rows_all("SELECT key,value FROM app_settings WHERE key IN ('group_bonus_amount','group_bonus_period_hours','group_bonus_pool','group_bonus_enabled')")
    values = {row["key"]: row["value"] for row in rows}
    return {
        "amount": int(values.get("group_bonus_amount", "0")),
        "period": int(values.get("group_bonus_period_hours", "24")),
        "pool": int(values.get("group_bonus_pool", "0")),
        "enabled": values.get("group_bonus_enabled", "0") == "1",
    }


async def send_admin_group_bonus_settings(chat_id: int, client=None):
    client = client or bot
    settings = await get_group_bonus_settings()
    bot_ready = bool(group_bot and BONUS_GROUP_ID)
    status = "🟢 включён" if settings["enabled"] and bot_ready else ("⏸ выключен" if not settings["enabled"] else "⚠️ не настроен токен/ID группы")
    text = (
        "💰 <b>ПОПОЛНЕНИЕ БАЛАНСА · АКЦИЯ</b>\n\n"
        f"Статус: {status}\n"
        f"Начисление за нажатие: <b>{settings['amount']:,} ⭐</b>\n"
        f"Интервал: <b>{settings['period']} ч.</b>\n"
        f"Резерв кампании: <b>{settings['pool']:,} ⭐</b>\n"
        f"Бот настроен: {'да' if bot_ready else 'нет — задайте GROUP_BOT_TOKEN и BONUS_GROUP_ID'}\n\n"
        "Резерв пополняется переводом доступного баланса администратора. Каждое начисление уменьшает резерв и проходит через журнал баланса."
    )
    await client.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb([
        [button("✏️ Сумма пополнения", "bonus:amount"), button("⏱ Интервал", "bonus:period")],
        [button("➕ Пополнить резерв", "bonus:fund", "success")],
        [button("📣 Создать / обновить кнопку в группе", "bonus:publish")],
        [button("▶️ Включить" if not settings["enabled"] else "⏸ Выключить", "bonus:toggle", "primary")],
        [button("↩️ Меню", "groupadmin:home" if client == group_bot else "admin:settings")],
    ]))


@dp.callback_query(F.data.startswith("bonus:"))
async def admin_group_bonus_action(callback: CallbackQuery, state: FSMContext):
    if not callback.message or callback.message.chat.type != "private" or not await is_admin(callback.from_user.id):
        await callback.answer("Доступно только администратору в личном чате с ботом.", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    prompts = {
        "amount": (AdminForm.group_bonus_amount, "Введи сумму пополнения в ⭐-единицах от 1 до 1 000 000 000."),
        "period": (AdminForm.group_bonus_period, "Введи интервал между получениями в часах: от 1 до 8760."),
        "fund": (AdminForm.group_bonus_fund, "Сколько ⭐ перевести из твоего доступного баланса в резерв кампании? Введи целое число от 1 до 1 000 000 000."),
    }
    if action == "publish":
        settings = await get_group_bonus_settings()
        if not group_bot or not BONUS_GROUP_ID:
            await callback.answer("Сначала задайте GROUP_BOT_TOKEN и BONUS_GROUP_ID и перезапустите приложение.", show_alert=True)
            return
        if not settings["enabled"]:
            await callback.answer("Сначала настройте сумму и резерв и включите акцию.", show_alert=True)
            return
        post_text = (
            "💰 <b>ПОПОЛНЕНИЕ БАЛАНСА УЧАСТНИКАМ</b>\n\n"
            f"Нажми кнопку ниже, чтобы получить <b>{settings['amount']:,} ⭐-единиц</b> из резерва акции на внутренний баланс. Это не Telegram Stars.\n"
            f"Повторное получение доступно раз в {settings['period']} ч., пока есть средства в резерве."
        )
        existing = await row_one("SELECT value FROM app_settings WHERE key='group_bonus_message_id'")
        message_id = int(existing["value"]) if existing and str(existing["value"]).isdigit() else 0
        published = None
        if message_id:
            try:
                await group_bot.edit_message_text(post_text, chat_id=BONUS_GROUP_ID, message_id=message_id,
                                                  parse_mode="HTML", reply_markup=group_bonus_markup())
                published = message_id
            except Exception:
                logging.info("Stored group bonus post not editable; creating a new one")
        if published is None:
            try:
                sent = await group_bot.send_message(BONUS_GROUP_ID, post_text, parse_mode="HTML", reply_markup=group_bonus_markup())
                published = sent.message_id
            except Exception:
                logging.exception("Could not publish group bonus button")
                await callback.answer("Не удалось отправить кнопку. Проверь, что групповой бот добавлен и может писать в группу.", show_alert=True)
                return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('group_bonus_message_id',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (str(published), now_local().isoformat()))
            await log_admin_action(db, callback.from_user.id, None, "group_bonus_button_published", f"group_id={BONUS_GROUP_ID};message_id={published}")
            await db.commit()
        await callback.answer("Кнопка опубликована/обновлена в группе")
        await send_admin_group_bonus_settings(callback.message.chat.id, callback.bot)
        return
    if action == "toggle":
        settings = await get_group_bonus_settings()
        if not settings["enabled"] and not group_bot:
            await callback.answer("Сначала задайте GROUP_BOT_TOKEN и перезапустите приложение.", show_alert=True)
            return
        if not settings["enabled"] and not BONUS_GROUP_ID:
            await callback.answer("Сначала задайте BONUS_GROUP_ID и перезапустите приложение.", show_alert=True)
            return
        if not settings["enabled"] and settings["amount"] < 1:
            await callback.answer("Сначала задайте сумму пополнения.", show_alert=True)
            return
        if not settings["enabled"] and settings["pool"] < settings["amount"]:
            await callback.answer("Сначала пополните резерв минимум на одно начисление.", show_alert=True)
            return
        enabled = not settings["enabled"]
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('group_bonus_enabled',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", ("1" if enabled else "0", now_local().isoformat()))
            await log_admin_action(db, callback.from_user.id, None, "group_bonus_toggle", f"enabled={int(enabled)}")
            await db.commit()
        if not enabled and group_bot and BONUS_GROUP_ID:
            published_row = await row_one("SELECT value FROM app_settings WHERE key='group_bonus_message_id'")
            if published_row and str(published_row["value"]).isdigit() and int(published_row["value"]) > 0:
                try:
                    await group_bot.edit_message_reply_markup(chat_id=BONUS_GROUP_ID, message_id=int(published_row["value"]), reply_markup=None)
                except Exception:
                    logging.info("Could not remove the paused group bonus button")
        await callback.answer("Пополнение по акции включено" if enabled else "Пополнение по акции выключено", show_alert=True)
        await send_admin_group_bonus_settings(callback.message.chat.id, callback.bot)
        return
    if action not in prompts:
        await callback.answer("Неизвестное действие", show_alert=True)
        return
    form_state, prompt = prompts[action]
    await state.set_state(form_state)
    await callback.message.answer(prompt + " Для отмены отправь /cancel.")
    await callback.answer()


async def parse_bonus_admin_input(message: Message, state: FSMContext, key: str, title: str, minimum: int, maximum: int):
    raw = (message.text or "").strip()
    if raw.lower() in {"/cancel", "cancel", "отмена"}:
        await state.clear()
        await message.answer("Изменение отменено.")
        await send_admin_group_bonus_settings(message.chat.id, message.bot)
        return None
    if not raw.isdigit() or not minimum <= int(raw) <= maximum:
        await message.answer(f"Введи целое число от {minimum:,} до {maximum:,} или /cancel.")
        return None
    value = int(raw)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (key, str(value), now_local().isoformat()))
        await log_admin_action(db, message.from_user.id, None, "set_group_bonus_setting", f"{key}={value}")
        await db.commit()
    await state.clear()
    await message.answer(f"✅ {title}: <b>{value:,}</b>.", parse_mode="HTML")
    await send_admin_group_bonus_settings(message.chat.id, message.bot)
    return value


@dp.message(AdminForm.group_bonus_amount)
async def save_group_bonus_amount(message: Message, state: FSMContext):
    if message.chat.type != "private" or not await is_admin(message.from_user.id):
        await state.clear(); return
    await parse_bonus_admin_input(message, state, "group_bonus_amount", "Сумма пополнения в ⭐-единицах", 1, 1_000_000_000)


@dp.message(AdminForm.group_bonus_period)
async def save_group_bonus_period(message: Message, state: FSMContext):
    if message.chat.type != "private" or not await is_admin(message.from_user.id):
        await state.clear(); return
    await parse_bonus_admin_input(message, state, "group_bonus_period_hours", "Интервал получения, часов", 1, 8760)


@dp.message(AdminForm.group_bonus_fund)
async def fund_group_bonus_pool(message: Message, state: FSMContext):
    if message.chat.type != "private" or not await is_admin(message.from_user.id):
        await state.clear(); return
    raw = (message.text or "").strip()
    if raw.lower() in {"/cancel", "cancel", "отмена"}:
        await state.clear(); await message.answer("Пополнение резерва отменено.")
        await send_admin_group_bonus_settings(message.chat.id, message.bot); return
    if not raw.isdigit() or not 1 <= int(raw) <= 1_000_000_000:
        await message.answer("Введи сумму от 1 до 1 000 000 000 ⭐ или /cancel."); return
    amount = int(raw)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        account = await (await db.execute("SELECT balance FROM users WHERE user_id=?", (message.from_user.id,))).fetchone()
        if not account or account["balance"] < amount:
            await db.rollback()
            await message.answer("Недостаточно доступного баланса для перевода в резерв.")
            return
        pool_row = await (await db.execute("SELECT value FROM app_settings WHERE key='group_bonus_pool'")).fetchone()
        pool = int(pool_row["value"] if pool_row else 0)
        await db.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, message.from_user.id))
        await db.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('group_bonus_pool',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (str(pool + amount), now_local().isoformat()))
        await ledger_entry(db, message.from_user.id, -amount, 0, "group_bonus_pool_funding", None, message.from_user.id, f"Promo pool funding: {amount}")
        await log_admin_action(db, message.from_user.id, None, "fund_group_bonus_pool", f"amount={amount};pool={pool + amount}")
        await db.commit()
    await state.clear()
    await message.answer(f"✅ В резерв переведено {amount:,} ⭐.")
    await send_admin_group_bonus_settings(message.chat.id, message.bot)


async def send_admin_panel(chat_id):
    moscow_now = now_moscow()
    cutoff = (moscow_now - timedelta(hours=24)).isoformat()
    end = moscow_now.isoformat()
    counts = await row_one(
        "SELECT COUNT(*) AS all_count, "
        "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS sent, "
        "SUM(CASE WHEN status IN ('awaiting_gift','review_pending','dispute','awaiting_payout','paid','refunded') THEN 1 ELSE 0 END) AS accepted, "
        "SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected "
        "FROM offers WHERE status!='draft' AND created_at>=? AND created_at<=?", (cutoff, end))
    payments = await row_one("SELECT COUNT(*) AS n FROM payments WHERE status='pending_review'")
    payouts = await row_one("SELECT COUNT(*) AS n FROM offers WHERE status='awaiting_payout'")
    await bot.send_message(
        chat_id,
        "🛠 <b>АДМИН-ПАНЕЛЬ</b>\n"
        f"{'⏸ Сервис на паузе' if OFFERS_PAUSED else '🟢 Сервис работает'}\n"
        f"📊 За 24 часа МСК: {counts['all_count'] or 0} офферов · 📨 ждут ответа {counts['sent'] or 0} · ✅ приняты {counts['accepted'] or 0} · ❌ отклонены {counts['rejected'] or 0}\n"
        f"💳 Пополнения к проверке: {payments['n']} · 💸 выплаты по офферам: {payouts['n']}\n\n"
        "Выбери раздел:",
        reply_markup=kb([
            [button("💸 Выплата по офферу", "admin:payouts", "primary")],
            [button("💰 Баланс / пополнения", "admin:payments", "primary")],
            [button("📊 Оферы · 24 ч МСК", "admin:offers", "primary")],
            [button("⚙️ Ещё", "admin:more")],
            [button("👥 Администраторы", "admin:admins")],
        ]), parse_mode="HTML",
    )


@dp.callback_query(F.data == "admin:admins")
async def admin_admins_info(callback: CallbackQuery):
    if callback.from_user.id not in ROOT_ADMIN_IDS or callback.message.chat.type != "private":
        await callback.answer("Только главный администратор в личном чате.", show_alert=True)
        return
    await callback.message.answer(
        "👥 Управление администраторами\n\n"
        "/admins — список\n"
        "/addadmin 123456789 — добавить по Telegram ID\n"
        "/deladmin 123456789 — удалить\n\n"
        "Назначенный администратор получает полный доступ к панели, балансам и сделкам. "
        "Управлять администраторами могут только ID из ADMIN_IDS в настройках."
    )
    await callback.answer()


@dp.message(Command("admins", "addadmin", "deladmin"))
async def manage_admins(message: Message):
    if message.chat.type != "private" or message.from_user.id not in ROOT_ADMIN_IDS:
        await message.answer("Только главный администратор в личном чате может управлять доступом.")
        return
    parts = (message.text or "").split()
    command = parts[0].split("@", 1)[0].lower()
    if command == "/admins":
        roots = ", ".join(str(uid) for uid in sorted(ROOT_ADMIN_IDS))
        rows = await rows_all("SELECT user_id,added_by FROM delegated_admins ORDER BY user_id")
        delegated = "\n".join(f"• {row['user_id']} (добавил {row['added_by']})" for row in rows if row['user_id'] not in ROOT_ADMIN_IDS) or "нет"
        await message.answer(f"👥 Главные администраторы (ADMIN_IDS): {roots}\n\nНазначенные через бота:\n{delegated}")
        return
    if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdigit() or not 1 <= int(parts[1]) <= 2**63 - 1:
        await message.answer(f"Формат: {command} 123456789 — числовой Telegram ID.")
        return
    target = int(parts[1])
    if target in ROOT_ADMIN_IDS:
        await message.answer("Этот ID задан в ADMIN_IDS. Изменить его можно только в настройках запуска.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        if command == "/addadmin":
            result = await db.execute("INSERT OR IGNORE INTO delegated_admins(user_id,added_by,added_at) VALUES(?,?,?)", (target, message.from_user.id, now_local().isoformat()))
            action = "add_admin"
        else:
            result = await db.execute("DELETE FROM delegated_admins WHERE user_id=?", (target,))
            action = "remove_admin"
        changed = result.rowcount == 1
        if changed:
            await log_admin_action(db, message.from_user.id, target, action, "delegated admin")
        await db.commit()
        if changed:
            if command == "/addadmin":
                ADMIN_IDS.add(target)
            else:
                ADMIN_IDS.discard(target)
    if not changed:
        await message.answer("Этот ID уже назначен администратором." if command == "/addadmin" else "Этот ID не является назначенным администратором.")
        return
    await message.answer(f"{'✅ Добавлен' if command == '/addadmin' else '✅ Удалён'} администратор {target}.")
    try:
        await bot.send_message(target, "Тебе предоставлен доступ администратора. Открой /admin." if command == "/addadmin" else "Твой доступ администратора отозван.")
    except Exception:
        logging.info("Could not notify admin %s about access change", target)


@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    await send_admin_panel(message.chat.id)


@dp.callback_query(F.data.startswith("remind:"))
async def admin_remind_offer(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    offer_id = int(callback.data.split(":")[1])
    row = await row_one("SELECT * FROM offers WHERE id=? AND status='awaiting_gift'", (offer_id,))
    if not row:
        await callback.answer("Сделка уже изменила статус.", show_alert=True)
        return
    if row["receiver_username"] != GIFT_CUSTODY_USERNAME:
        await callback.answer("Эта старая сделка приостановлена: сначала создайте новый оффер на фиксированный адрес.", show_alert=True)
        return
    for uid, text in (
        (row["buyer_id"], f"🔔 Напоминание по офферу #{offer_id}: подарок ещё не отмечен как отправленный. Если есть проблема, открой спор в боте."),
        (row["seller_id"], f"🔔 Напоминание по офферу #{offer_id}: отправь NFT на @{row['receiver_username']} и пришли ссылку на передачу либо открой спор."),
    ):
        try:
            await bot.send_message(uid, text, reply_markup=participant_dispute_keyboard(offer_id))
        except Exception:
            pass
    async with aiosqlite.connect(DB_PATH) as db:
        await log_admin_action(db, callback.from_user.id, row["buyer_id"], "remind_offer", f"offer_id={offer_id}")
        await db.commit()
    await callback.answer("Напоминания отправлены")


@dp.callback_query(F.data.startswith("admin:"))
async def admin_list(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    kind = callback.data.split(":")[1]
    if kind == "more":
        await callback.message.answer(
            "⚙️ <b>ДОПОЛНИТЕЛЬНО</b>", parse_mode="HTML", reply_markup=kb([
                [button("👥 Пользователи / баланс", "admin:user")],
                [button("📥 Передачи и споры", "admin:queues")],
                [button("⚙️ Настройки", "admin:settings"), button("📈 Отчёты", "admin:reports")],
                [button("↩️ Главное меню", "menu:admin")],
            ]))
    elif kind == "offers":
        await send_admin_offer_categories(callback.message.chat.id)
    elif kind == "queues":
        await callback.message.answer(
            "📥 <b>ОЧЕРЕДИ</b>\nВыбери, что нужно обработать:",
            parse_mode="HTML", reply_markup=kb([
                [button("💳 Оплаты", "admin:payments"), button("🎁 Передачи на проверке", "admin:reviews")],
                [button("📦 Ждут передачи", "admin:waiting"), button("💸 К выплате", "admin:payouts")],
                [button("🆘 Споры", "admin:disputes")],
                [button("↩️ Админ-панель", "menu:admin")],
            ]),
        )
    elif kind == "reports":
        await callback.message.answer(
            "📊 <b>ОТЧЁТЫ</b>\nВыбери нужный отчёт:",
            parse_mode="HTML", reply_markup=kb([
                [button("📈 Сводка и финансы", "admin:overview")],
                [button("📚 История сделок", "admin:history"), button("🧾 Действия админов", "admin:actions")],
                [button("📥 Скачать CSV", "admin:export")],
                [button("↩️ Админ-панель", "menu:admin")],
            ]),
        )
    elif kind == "overview":
        today = now_local().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        users = await row_one("SELECT COUNT(*) AS total, SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END) AS new_today FROM users", (today,))
        offers = await row_one("SELECT COUNT(*) AS total, SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END) AS today FROM offers", (today,))
        active = await row_one("SELECT COUNT(*) AS n FROM offers WHERE status IN ('open','awaiting_gift','review_pending','dispute','awaiting_payout')")
        payments_today = await row_one("SELECT COUNT(*) AS n FROM payments WHERE created_at>=?", (today,))
        balances = await row_one("SELECT COALESCE(SUM(balance),0) AS available, COALESCE(SUM(reserved),0) AS reserved FROM users")
        await callback.message.answer(
            "📊 <b>Сводка сервиса</b>\n\n"
            f"👥 Пользователи: {users['total'] or 0} · новых сегодня: {users['new_today'] or 0}\n"
            f"🎁 Офферы: {offers['total'] or 0} · сегодня: {offers['today'] or 0} · активных: {active['n']}\n"
            f"💳 Платёжных заявок сегодня: {payments_today['n']}\n"
            f"💰 Внутренний баланс: доступно {balances['available']} ⭐ · в резерве {balances['reserved']} ⭐\n\n"
            "Баланс и резерв — внутренние учётные единицы, не фактический баланс Stars в Telegram. Выплаты продавцам подтверждаются вручную.",
            reply_markup=kb([[button("🔄 Обновить", "admin:overview")], [button("↩️ Отчёты", "admin:reports")]]),
            parse_mode="HTML")
    elif kind == "export":
        await callback.answer("Готовлю архив отчётов…")
        await send_admin_export(callback.message.chat.id, callback.from_user.id)
        return
    elif kind == "payments":
        await send_admin_payment_list(callback.message.chat.id, 0)
    elif kind == "waiting":
        items = await rows_all("SELECT * FROM offers WHERE status='awaiting_gift' ORDER BY id LIMIT 20")
        if not items:
            await callback.message.answer("✅ Нет сделок, ожидающих передачи.", reply_markup=kb([[button("↩️ Очереди", "admin:queues")]]))
        for o in items:
            await callback.message.answer(
                f"📦 Оффер #{o['id']} · {o['amount']} ⭐ · {'⚠️ виртуальный резерв' if o['virtual_reserve'] else '✅ внутренний резерв'}\n"
                f"Покупатель @{html.escape(o['buyer_username'] or str(o['buyer_id']))} · продавец @{html.escape(o['seller_username'] or str(o['seller_id']))}\n"
                f"NFT: {html.escape(o['nft_title'] or 'подарок')} #{html.escape(o['nft_number'] or '—')}\nСсылка: {html.escape(o['gift_url'])}",
                reply_markup=admin_waiting_keyboard(o['id']), parse_mode="HTML", disable_web_page_preview=True)
    elif kind == "reviews":
        items = await rows_all("SELECT * FROM offers WHERE status='review_pending' ORDER BY id LIMIT 10")
        if not items:
            await callback.message.answer("✅ Нет передач для проверки.", reply_markup=kb([[button("↩️ Очереди", "admin:queues")]]))
        for o in items:
            virtual_tag = "\n⚠️ Проверь резерв перед выплатой." if o["virtual_reserve"] else ""
            review_text = (
                f"🔎 Оффер #{o['id']} · {o['amount']} ⭐{virtual_tag}\n"
                f"Покупатель @{html.escape(o['buyer_username'] or str(o['buyer_id']))} · продавец @{html.escape(o['seller_username'] or str(o['seller_id']))}\n"
                f"NFT: {html.escape(o['nft_title'] or 'подарок')} #{html.escape(o['nft_number'] or '—')}\n"
                f"Ссылка: {html.escape(o['gift_url'])}\nПереданный подарок: {html.escape(o['proof_url'] or '—')}"
            )
            await callback.message.answer(review_text, reply_markup=admin_offer_keyboard(o['id'], "review"),
                parse_mode="HTML", disable_web_page_preview=True)
    elif kind == "payouts":
        await send_admin_offer_list(callback.message.chat.id, "payout", 0)
    elif kind == "disputes":
        items = await rows_all("SELECT * FROM offers WHERE status='dispute' ORDER BY id LIMIT 20")
        if not items:
            await callback.message.answer("✅ Активных споров нет.", reply_markup=kb([[button("↩️ Очереди", "admin:queues")]]))
        for o in items:
            await callback.message.answer(
                f"🆘 Спор #{o['id']} · {o['amount']} ⭐ · {'⚠️ виртуальный резерв' if o['virtual_reserve'] else 'резерв удержан'}\n"
                f"Покупатель @{html.escape(o['buyer_username'] or str(o['buyer_id']))} · продавец @{html.escape(o['seller_username'] or str(o['seller_id']))}\n"
                f"Причина: {html.escape(o['dispute_reason'] or 'не указана')}\nПодарок: {html.escape(o['proof_url'] or o['gift_url'])}",
                reply_markup=admin_dispute_keyboard(o['id']), parse_mode="HTML", disable_web_page_preview=True)
    elif kind == "history":
        items = await rows_all("SELECT id,buyer_id,buyer_username,seller_id,seller_username,rejected_by,nft_title,nft_number,amount,status FROM offers ORDER BY id DESC LIMIT 20")
        if not items:
            await callback.message.answer("История сделок пока пуста.", reply_markup=kb([[button("↩️ Отчёты", "admin:reports")]]))
        else:
            lines = [f"#{o['id']} · {html.escape(o['nft_title'] or 'NFT-подарок')} #{html.escape(o['nft_number'] or '—')} · {o['amount']} ⭐ · <b>{html.escape(o['status'])}</b> · @{html.escape(o['buyer_username'] or str(o['buyer_id']))} → @{html.escape(o['seller_username'] or 'ожидает')}" + (f" · отклонил user_id {o['rejected_by']}" if o['rejected_by'] else "") for o in items]
            await callback.message.answer("📚 <b>Последние 20 сделок</b>\n\n" + "\n".join(lines), parse_mode="HTML", reply_markup=kb([[button("↩️ Отчёты", "admin:reports")]]))
    elif kind == "actions":
        items = await rows_all("SELECT * FROM admin_actions ORDER BY id DESC LIMIT 30")
        if not items:
            await callback.message.answer("Журнал действий пока пуст.", reply_markup=kb([[button("↩️ Отчёты", "admin:reports")]]))
        else:
            lines = [f"{x['created_at'][:16]} · admin <code>{x['admin_id']}</code> → user <code>{x['target_user_id'] or '—'}</code> · {html.escape(x['action'])} · {html.escape(x['details'] or '')}" for x in items]
            await callback.message.answer("🧾 <b>Последние 30 действий</b>\n\n" + "\n".join(lines), parse_mode="HTML", reply_markup=kb([[button("↩️ Отчёты", "admin:reports")]]))
    elif kind == "plans":
        plans = await rows_all("SELECT * FROM subscription_plans ORDER BY sort_order")
        lines = ["🧾 <b>ТАРИФЫ И КВОТЫ ПАРСЕРА</b>"]
        buttons = []
        for plan in plans:
            cost = "бесплатно" if plan["code"] == "free" else f"{plan['price_units']} ⭐ / {plan['duration_days']} дн."
            lines.append(f"<b>{html.escape(plan['name'])}</b> · {cost}\n🧩 Запросов парсера: {plan['parser_per_day']}/день")
            buttons.append([button(f"✏️ Изменить «{plan['name']}»", f"planedit:{plan['code']}")])
        lines.append("\nИзменения сохраняются в базе и отражаются в меню подписок. Тарифицируется квота парсера.")
        buttons.append([button("↩️ Настройки", "admin:settings")])
        await callback.message.answer("\n\n".join(lines), parse_mode="HTML", reply_markup=kb(buttons))
    elif kind == "settings":
        await send_admin_settings(callback.message.chat.id)
    elif kind == "user":
        await state.set_state(AdminForm.lookup_id)
        await callback.message.answer(
            "Отправь ID/@username через запятую или пробелами. Большие списки загружай файлом CSV/TXT — жёсткого лимита по числу пользователей нет.\n"
            "Пример: <code>123456789, @user_one, 987654321</code>\n\n"
            "Дальше можно открывать профили, выбирать пользователей по страницам или выделить всех и применить одинаковое действие.",
            parse_mode="HTML",
        )
    await callback.answer()




OFFER_STATUS_LABELS = {
    "draft": "Черновик", "open": "Отправлен · ждёт ответа", "awaiting_gift": "Принят · ждём NFT",
    "review_pending": "Передача на проверке", "dispute": "Спор", "awaiting_payout": "Ожидает выплаты",
    "paid": "Выплачен", "refunded": "Возврат", "rejected": "Отклонён", "expired": "Истёк",
}
OFFER_LIST_FILTERS = {
    "sent": ("Отправлены / ждут ответа", ("open",)),
    "accepted": ("Принятые", ("awaiting_gift", "review_pending", "dispute", "awaiting_payout", "paid", "refunded")),
    "rejected": ("Отклонённые", ("rejected",)),
    "payout": ("Выплата по офферу", ("awaiting_payout",)),
    "all": ("Все офферы", ()),
}


def short_button_text(text: str, limit: int = 58) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def format_offer_person(username, user_id):
    if not user_id:
        return "не назначен"
    label = f"@{html.escape(username)}" if username else "без username"
    return f"{label} · <code>{user_id}</code>"


async def send_admin_offer_categories(chat_id: int, edit_message=None):
    moscow_now = now_moscow()
    cutoff, end = (moscow_now - timedelta(hours=24)).isoformat(), moscow_now.isoformat()
    counts = await row_one(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS sent, "
        "SUM(CASE WHEN status IN ('awaiting_gift','review_pending','dispute','awaiting_payout','paid','refunded') THEN 1 ELSE 0 END) AS accepted, "
        "SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected "
        "FROM offers WHERE status!='draft' AND created_at>=? AND created_at<=?", (cutoff, end))
    text = (
        "📊 <b>ОФФЕРЫ</b> · за последние 24 часа по МСК\n"
        f"Всего: <b>{counts['total'] or 0}</b> · ждут ответа: {counts['sent'] or 0} · приняты: {counts['accepted'] or 0} · отклонены: {counts['rejected'] or 0}\n"
        "Нажми категорию — откроется список; выбери оффер, чтобы увидеть полные данные и время."
    )
    markup = kb([
        [button(f"📨 Отправлены · {counts['sent'] or 0}", "offerlist:sent:0")],
        [button(f"✅ Приняты · {counts['accepted'] or 0}", "offerlist:accepted:0")],
        [button(f"❌ Отклонены · {counts['rejected'] or 0}", "offerlist:rejected:0")],
        [button("📚 Все офферы", "offerlist:all:0")],
        [button("↩️ Главное меню", "menu:admin")],
    ])
    if edit_message:
        await edit_message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


async def send_admin_offer_list(chat_id: int, category: str, page: int, edit_message=None):
    page_size = 10
    page = max(0, page)
    cutoff, end = (now_moscow() - timedelta(hours=24)).isoformat(), now_moscow().isoformat()
    if category not in OFFER_LIST_FILTERS:
        category = "all"
    title, statuses = OFFER_LIST_FILTERS[category]
    params = []
    conditions = []
    if statuses:
        conditions.append("status IN (" + ",".join("?" for _ in statuses) + ")")
        params.extend(statuses)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    total_row = await row_one("SELECT COUNT(*) AS n FROM offers" + where, tuple(params))
    total = total_row["n"] if total_row else 0
    since_conditions = conditions + ["created_at>=?", "created_at<=?"]
    since_params = params + [cutoff, end]
    recent_row = await row_one("SELECT COUNT(*) AS n FROM offers WHERE " + " AND ".join(since_conditions), tuple(since_params))
    recent = recent_row["n"] if recent_row else 0
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages - 1)
    items = await rows_all(
        "SELECT id,buyer_id,buyer_username,seller_id,seller_username,nft_title,nft_number,amount,status,created_at FROM offers" + where +
        " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", tuple(params + [page_size, page * page_size]))
    lines = [f"📋 <b>{html.escape(title)}</b> · {total} всего · {recent} за 24 ч. МСК · страница {page + 1}/{pages}"]
    buttons = []
    for offer in items:
        who = offer["buyer_username"] or str(offer["buyer_id"])
        nft = offer["nft_title"] or "NFT"
        time_label = format_moscow(offer["created_at"])[0:16]
        lines.append(f"#{offer['id']} · {html.escape(nft)} · {offer['amount']} ⭐ · {html.escape(who)} · {time_label}")
        buttons.append([button(short_button_text(f"#{offer['id']} · {nft} · {offer['amount']}⭐ · {time_label}"), f"offerinfo:{offer['id']}:{category}:{page}")])
    if not items:
        lines.append("В этой категории пока нет офферов.")
    nav = []
    if page > 0:
        nav.append(button("◀️ Назад", f"offerlist:{category}:{page - 1}"))
    if page + 1 < pages:
        nav.append(button("Дальше ▶️", f"offerlist:{category}:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.extend([
        [button("✅ Принятые", "offerlist:accepted:0"), button("❌ Отклонённые", "offerlist:rejected:0")],
        [button("📚 Все", "offerlist:all:0"), button("↩️ Категории", "offerlist:menu:0")],
    ])
    text, markup = "\n".join(lines), kb(buttons)
    if edit_message:
        await edit_message.edit_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
    else:
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


async def send_admin_offer_detail(chat_id: int, offer_id: int, category: str, page: int, edit_message=None):
    offer = await row_one("SELECT * FROM offers WHERE id=?", (offer_id,))
    if not offer:
        await bot.send_message(chat_id, "Оффер не найден.", reply_markup=kb([[button("↩️ К списку", f"offerlist:{category}:0")]]))
        return
    status = OFFER_STATUS_LABELS.get(offer["status"], offer["status"])
    text = (
        f"🎁 <b>ОФФЕР №{offer_id}</b>\n"
        f"Статус: <b>{html.escape(status)}</b>\n"
        f"Создан: <b>{format_moscow(offer['created_at'])}</b>\n"
        f"Обновлён: <b>{format_moscow(offer['updated_at'])}</b>\n\n"
        f"Покупатель / создатель: {format_offer_person(offer['buyer_username'], offer['buyer_id'])}\n"
        f"Продавец: {format_offer_person(offer['seller_username'], offer['seller_id'])}\n"
        f"Отклонил: <code>{offer['rejected_by'] or '—'}</code>\n"
        f"Сумма: <b>{offer['amount']} ⭐-единиц</b>\n"
        f"NFT: {html.escape(offer['nft_title'] or 'NFT-подарок')} #{html.escape(offer['nft_number'] or '—')}\n"
        f"Ссылка NFT: {html.escape(offer['gift_url'])}\n"
        f"Адрес передачи: @{html.escape(offer['receiver_username'])}\n"
        f"Ссылка передачи: {html.escape(offer['proof_url'] or '—')}\n"
        f"Внутренний резерв: {'создан' if not offer['virtual_reserve'] else 'нужна ручная проверка'}\n"
        f"Причина спора: {html.escape(offer['dispute_reason'] or '—')}"
    )
    rows = []
    if offer["status"] == "review_pending":
        rows.extend(admin_offer_keyboard(offer_id, "review").inline_keyboard)
    elif offer["status"] == "awaiting_payout":
        rows.extend(admin_offer_keyboard(offer_id, "payout").inline_keyboard)
    elif offer["status"] == "dispute":
        rows.extend(admin_dispute_keyboard(offer_id).inline_keyboard)
    elif offer["status"] == "awaiting_gift":
        rows.extend(admin_waiting_keyboard(offer_id).inline_keyboard)
    rows.append([button("↩️ К списку", f"offerlist:{category}:{page}")])
    if edit_message:
        await edit_message.edit_text(text, parse_mode="HTML", reply_markup=kb(rows), disable_web_page_preview=True)
    else:
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb(rows), disable_web_page_preview=True)


@dp.callback_query(F.data.startswith("offerlist:"))
async def admin_offer_list_callback(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True); return
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная страница", show_alert=True); return
    if parts[1] == "menu":
        await send_admin_offer_categories(callback.message.chat.id, edit_message=callback.message)
    else:
        await send_admin_offer_list(callback.message.chat.id, parts[1], int(parts[2]), edit_message=callback.message)
    await callback.answer()


@dp.callback_query(F.data.startswith("offerinfo:"))
async def admin_offer_detail_callback(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True); return
    parts = callback.data.split(":")
    if len(parts) != 4 or not parts[1].isdigit() or not parts[3].isdigit():
        await callback.answer("Некорректный оффер", show_alert=True); return
    await send_admin_offer_detail(callback.message.chat.id, int(parts[1]), parts[2], int(parts[3]), edit_message=callback.message)
    await callback.answer()


async def send_admin_payment_list(chat_id: int, page: int = 0, edit_message=None):
    page_size = 10
    total_row = await row_one("SELECT COUNT(*) AS n FROM payments WHERE status='pending_review'")
    total = total_row["n"] if total_row else 0
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    items = await rows_all("SELECT * FROM payments WHERE status='pending_review' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (page_size, page * page_size))
    lines = [f"💰 <b>БАЛАНС / ПОПОЛНЕНИЯ</b> · к проверке: {total} · страница {page + 1}/{pages}"]
    buttons = []
    for payment in items:
        name = payment["username"] or str(payment["user_id"])
        lines.append(f"#{payment['id']} · {payment['amount_units']} ⭐ · {html.escape(name)} · {format_moscow(payment['created_at'])}")
        buttons.append([button(short_button_text(f"#{payment['id']} · {payment['amount_units']}⭐ · {name}"), f"payinfo:{payment['id']}:{page}")])
    if not items:
        lines.append("Нет пополнений, ожидающих проверки.")
    nav = []
    if page > 0: nav.append(button("◀️ Назад", f"paylist:{page - 1}"))
    if page + 1 < pages: nav.append(button("Далее ▶️", f"paylist:{page + 1}"))
    if nav: buttons.append(nav)
    buttons.extend([
        [button("👤 Найти / изменить баланс", "admin:user")],
        [button("↩️ Главное меню", "menu:admin")],
    ])
    text, markup = "\n".join(lines), kb(buttons)
    if edit_message:
        await edit_message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


@dp.callback_query(F.data.startswith("paylist:"))
async def admin_payment_list_callback(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True); return
    raw = callback.data.split(":", 1)[1]
    if not raw.isdigit():
        await callback.answer("Некорректная страница", show_alert=True); return
    await send_admin_payment_list(callback.message.chat.id, int(raw), edit_message=callback.message)
    await callback.answer()


@dp.callback_query(F.data.startswith("payinfo:"))
async def admin_payment_detail_callback(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True); return
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await callback.answer("Некорректная заявка", show_alert=True); return
    payment_id, page = int(parts[1]), int(parts[2])
    payment = await row_one("SELECT * FROM payments WHERE id=?", (payment_id,))
    if not payment:
        await callback.answer("Заявка не найдена", show_alert=True); return
    text = (
        f"💳 <b>ЗАЯВКА №{payment_id}</b>\n"
        f"Статус: {html.escape(payment['status'])}\n"
        f"Создана: {format_moscow(payment['created_at'])}\n"
        f"Обновлена: {format_moscow(payment['updated_at'])}\n"
        f"Пользователь: {format_offer_person(payment['username'], payment['user_id'])}\n"
        f"Сумма: <b>{payment['amount_units']} ⭐</b>\n"
        f"Тип: {html.escape(payment['purpose'])} · способ: {html.escape(payment['method'])}\n"
        f"ID операции: <code>{html.escape(payment['operation_id'] or '—')}</code>"
    )
    rows = []
    if payment["status"] == "pending_review":
        rows.extend(admin_payment_keyboard(payment_id).inline_keyboard)
    rows.append([button("↩️ К списку пополнений", f"paylist:{page}")])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb(rows))
    await callback.answer()


async def send_admin_export(chat_id: int, admin_id: int):
    tables = {
        "users.csv": ("SELECT user_id,username,balance,reserved,subscription_plan,premium_until,blocked,unlimited_parser,unlimited_balance,created_at FROM users ORDER BY user_id",),
        "offers.csv": ("SELECT id,buyer_id,buyer_username,seller_id,seller_username,rejected_by,receiver_username,gift_url,nft_title,nft_number,amount,status,virtual_reserve,proof_url,created_at,updated_at FROM offers ORDER BY id",),
        "payments.csv": ("SELECT id,user_id,username,method,purpose,amount_units,status,created_at,updated_at FROM payments ORDER BY id",),
        "balance_ledger.csv": ("SELECT id,user_id,delta_available,delta_reserved,event,reference_id,actor_id,note,created_at FROM balance_ledger ORDER BY id",),
        "admin_actions.csv": ("SELECT id,admin_id,target_user_id,action,details,created_at FROM admin_actions ORDER BY id",),
        "subscription_plans.csv": ("SELECT code,name,price_units,duration_days,offers_per_day,parser_per_day,sort_order,enabled FROM subscription_plans ORDER BY sort_order",),
        "parser_usage.csv": ("SELECT user_id,usage_date,count FROM parser_usage ORDER BY usage_date DESC,user_id",),
        "nft_cache.csv": ("SELECT slug,title,gift_number,image_url,attributes_json,updated_at FROM nft_cache ORDER BY updated_at DESC",),
        "group_bonus_claims.csv": ("SELECT id,user_id,group_id,amount,claimed_at FROM group_bonus_claims ORDER BY id",),
    }
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for filename, (query,) in tables.items():
            rows = await rows_all(query)
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            if rows:
                columns = list(rows[0].keys())
            else:
                async with aiosqlite.connect(DB_PATH) as db:
                    cursor = await db.execute(query)
                    columns = [column[0] for column in cursor.description or []]
            writer.writerow(columns)
            for row in rows:
                writer.writerow([row[column] for column in columns])
            bundle.writestr(filename, "\ufeff" + output.getvalue())
    archive.seek(0)
    async with aiosqlite.connect(DB_PATH) as db:
        await log_admin_action(db, admin_id, None, "export_audit_csv", f"bytes={archive.getbuffer().nbytes}")
        await db.commit()
    filename = f"giftoffer_audit_{now_local().strftime('%Y%m%d_%H%M%S')}.zip"
    await bot.send_document(chat_id, BufferedInputFile(archive.getvalue(), filename=filename),
                            caption="📥 Экспорт: пользователи, сделки, платежи, баланс, тарифы, использование и кэш NFT, действия админов. Храни архив конфиденциально.")


CONFIGURATION_FIELDS = {
    "premium_price": ("premium_price_stars", "PREMIUM_PRICE_STARS", "Цена премиума во внутренних ⭐-единицах", 1, 1_000_000),
    "premium_days": ("premium_days", "PREMIUM_DAYS", "Длительность премиума в днях", 1, 3650),
    "ttl": ("offer_ttl_hours", "OFFER_TTL_HOURS", "Срок действия оффера в часах", 1, 720),
}


@dp.callback_query(F.data.startswith("planedit:"))
async def admin_plan_edit(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    code = callback.data.split(":", 1)[1]
    plan = await row_one("SELECT * FROM subscription_plans WHERE code=?", (code,))
    if not plan:
        await callback.answer("Тариф не найден.", show_alert=True)
        return
    await state.update_data(plan_code=code)
    await state.set_state(AdminForm.plan_config)
    if code == "free":
        instruction = "Введи квоту запросов парсера в день, например: 3"
    else:
        instruction = "Введи три числа: цена_⭐ запросов_парсера_в_день дней. Например: 19 15 30"
    await callback.message.answer(f"✏️ Тариф «{html.escape(plan['name'])}». {instruction} Чтобы отменить, отправь /cancel.", parse_mode="HTML")
    await callback.answer()


@dp.message(AdminForm.plan_config)
async def admin_plan_config_save(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if raw.lower() in {"/cancel", "cancel", "отмена"}:
        await state.clear()
        await message.answer("Изменение тарифа отменено.")
        return
    data = await state.get_data()
    code = data["plan_code"]
    expected = 1 if code == "free" else 3
    parts = raw.split()
    if len(parts) != expected or not all(part.isdigit() for part in parts):
        await message.answer("Формат не совпал. Введи только указанные целые числа или /cancel.")
        return
    values = [int(part) for part in parts]
    if code == "free":
        parser = values[0]
        price, days = 0, 0
    else:
        price, parser, days = values
    if parser < 1 or parser > 100000 or (code != "free" and (price < 1 or price > 1000000 or days < 1 or days > 3650)):
        await message.answer("Квота парсера — 1–100000; цена — 1–1000000 ⭐, срок — 1–3650 дней.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE subscription_plans SET price_units=?,duration_days=?,offers_per_day=0,parser_per_day=? WHERE code=?", (price, days, parser, code))
        await log_admin_action(db, message.from_user.id, None, "edit_subscription_plan", f"{code};price={price};parser={parser};days={days}")
        await db.commit()
    await state.clear()
    await message.answer(f"✅ Тариф обновлён. Офферы без дневного ограничения; квота парсера: {parser}/день." + (f" Цена {price} ⭐ / {days} дней." if code != "free" else ""))
    plans = await rows_all("SELECT * FROM subscription_plans ORDER BY sort_order")
    lines = ["🧾 <b>Тарифы и квоты парсера</b>"]
    buttons = []
    for plan in plans:
        cost = "бесплатно" if plan["code"] == "free" else f"{plan['price_units']} ⭐ / {plan['duration_days']} дн."
        lines.append(f"\n<b>{html.escape(plan['name'])}</b> · {cost}\n🧩 Запросов парсера: {plan['parser_per_day']}/день")
        buttons.append([button(f"✏️ {plan['name']}", f"planedit:{plan['code']}")])
    buttons.append([button("↩️ Админ-панель", "menu:admin")])
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb(buttons))


@dp.callback_query(F.data.startswith("cfg:"))
async def admin_config_action(callback: CallbackQuery, state: FSMContext):
    global OFFERS_PAUSED
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    if key == "bonus":
        await send_admin_group_bonus_settings(callback.message.chat.id)
        await callback.answer()
        return
    if key == "rate":
        await callback.message.answer(
            "💎 <b>Crypto Pay</b>\n\n"
            f"Статус: {'готов' if crypto_pay_enabled() else 'не готов — нужны CRYPTOPAY_TOKEN и положительный курс'}\n"
            f"Сейчас: {USDT_PER_STAR} USDT за 1 внутреннюю ⭐-единицу.\n\n"
            "Задать курс по цене пакета: <code>/rate 100 1.50</code>. Это означает 100 внутренних единиц за 1.50 USDT.\n"
            "Отключить оплату: <code>/rate off</code>. Токен задаётся только в Railway Variables.",
            parse_mode="HTML", reply_markup=kb([[button("🔌 Проверить подключение", "cfg:test", "success")], [button("↩️ Настройки", "admin:settings")]]))
        await callback.answer()
        return
    if key == "test":
        if not CRYPTOPAY_TOKEN:
            await callback.answer("Сначала добавь CRYPTOPAY_TOKEN в Railway Variables.", show_alert=True)
            return
        try:
            app_info = await crypto_api("getMe", {})
            app_name = html.escape(str(app_info.get("name") or app_info.get("app_id") or "Crypto Pay"))
            await callback.message.answer(f"✅ Crypto Pay подключён. Приложение: <b>{app_name}</b>.", parse_mode="HTML")
            await callback.answer("Токен принят")
        except Exception:
            logging.exception("Crypto Pay connection test failed")
            await callback.answer("Проверка не прошла. Проверь CRYPTOPAY_TOKEN в Railway Variables.", show_alert=True)
        return
    if key == "pause":
        OFFERS_PAUSED = not OFFERS_PAUSED
        value = "1" if OFFERS_PAUSED else "0"
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('offers_paused',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (value, now_local().isoformat()))
            await log_admin_action(db, callback.from_user.id, None, "pause_offers" if OFFERS_PAUSED else "resume_offers", "")
            await db.commit()
        await callback.answer("⏸ Новые офферы на паузе" if OFFERS_PAUSED else "🟢 Создание новых офферов включено", show_alert=True)
        await send_admin_settings(callback.message.chat.id)
        return
    if key not in CONFIGURATION_FIELDS:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    _, _, title, minimum, maximum = CONFIGURATION_FIELDS[key]
    await state.update_data(config_key=key)
    await state.set_state(AdminForm.config_value)
    await callback.message.answer(f"✏️ {title}. Введи целое число от {minimum} до {maximum}. Для отмены отправь /cancel.")
    await callback.answer()


@dp.message(AdminForm.config_value)
async def save_admin_config_value(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if raw.lower() in {"/cancel", "cancel", "отмена"}:
        await state.clear()
        await message.answer("Изменение отменено.")
        return
    data = await state.get_data()
    config_key = data.get("config_key")
    spec = CONFIGURATION_FIELDS.get(config_key)
    if not spec:
        await state.clear()
        await message.answer("Настройка не найдена. Открой /admin и попробуй снова.")
        return
    setting_key, variable, title, minimum, maximum = spec
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if not minimum <= value <= maximum:
        await message.answer(f"Введи целое число от {minimum} до {maximum}, либо /cancel.")
        return
    globals()[variable] = value
    now = now_local().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (setting_key, str(value), now))
        await log_admin_action(db, message.from_user.id, None, "change_service_setting", f"{setting_key}={value}")
        await db.commit()
    await state.clear()
    await message.answer(f"✅ {title}: <b>{value}</b>. Настройка сохранена.", parse_mode="HTML")
    await send_admin_settings(message.chat.id)


async def send_admin_user_offers(chat_id: int, target_id: int, page: int = 0):
    page_size = 10
    total_row = await row_one("SELECT COUNT(*) AS n FROM offers WHERE buyer_id=? OR seller_id=?", (target_id, target_id))
    total = total_row["n"] if total_row else 0
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    items = await rows_all(
        "SELECT id,buyer_id,seller_id,amount,status,gift_url,nft_title,nft_number,proof_url,created_at "
        "FROM offers WHERE buyer_id=? OR seller_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (target_id, target_id, page_size, page * page_size),
    )
    if not items:
        await bot.send_message(chat_id, "📋 <b>ОФФЕРЫ ПОЛЬЗОВАТЕЛЯ</b>\nПока офферов нет.", parse_mode="HTML")
        return
    labels = {
        "draft": "Черновик", "open": "Ожидает продавца", "awaiting_gift": "Ждём передачу",
        "review_pending": "На проверке", "dispute": "Спор", "awaiting_payout": "Ожидает выплаты",
        "paid": "Завершён", "refunded": "Возврат", "rejected": "Отклонён", "expired": "Истёк",
    }
    for offer in items:
        role = "Покупатель" if offer["buyer_id"] == target_id else "Продавец"
        title = html.escape(offer["nft_title"] or "NFT-подарок")
        number = html.escape(offer["nft_number"] or "—")
        status = html.escape(labels.get(offer["status"], offer["status"]))
        detail = (
            f"🎁 <b>Оффер #{offer['id']}</b> · {role}\n"
            f"{status} · <b>{offer['amount']} ⭐</b>\n"
            f"{title} #{number}\n"
            f"🔗 {html.escape(offer['gift_url'])}"
        )
        if offer["proof_url"]:
            detail += f"\n📦 Передача: {html.escape(offer['proof_url'])}"
        await bot.send_message(chat_id, detail, parse_mode="HTML", disable_web_page_preview=True)
    navigation = []
    if page > 0:
        navigation.append(button("◀️ Назад", f"useroffers:{target_id}:{page - 1}"))
    if page + 1 < pages:
        navigation.append(button("Дальше ▶️", f"useroffers:{target_id}:{page + 1}"))
    rows = [navigation] if navigation else []
    rows.append([button(f"📋 Обновить сделки · {page + 1}/{pages}", f"useroffers:{target_id}:{page}")])
    rows.append([button("↩️ К профилю", f"user:refresh:{target_id}")])
    await bot.send_message(chat_id, f"📚 Показаны офферы {page * page_size + 1}–{min((page + 1) * page_size, total)} из {total}.", reply_markup=kb(rows))


@dp.callback_query(F.data.startswith("useroffers:"))
async def admin_user_offers_page(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, raw_id, raw_page = callback.data.split(":")
    if not raw_id.isdigit() or not raw_page.isdigit():
        await callback.answer("Неверная страница.", show_alert=True)
        return
    await send_admin_user_offers(callback.message.chat.id, int(raw_id), int(raw_page))
    await callback.answer()


async def send_admin_user_card(chat_id: int, target_id: int):
    user = await row_one("SELECT * FROM users WHERE user_id=?", (target_id,))
    if not user:
        await bot.send_message(chat_id, "Пользователь не найден. Он должен хотя бы один раз открыть бота.")
        return
    name = f"@{html.escape(user['username'])}" if user["username"] else "username не задан"
    premium = user["premium_until"] or "нет"
    text = (
        "👤 <b>ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ</b>\n\n"
        f"Имя: {name}\nID: <code>{user['user_id']}</code>\n"
        f"Баланс: {user['balance']} ⭐ · резерв: {user['reserved']} ⭐\n"
        f"Тариф: {html.escape(user['subscription_plan'] or 'free')} · до: {html.escape(premium)}\n"
        f"Статус: {'🚫 заблокирован' if user['blocked'] else '✅ активен'}\n"
        f"Квота парсера: {'♾ без ограничений' if user['unlimited_parser'] else 'по тарифу'}"
    )
    await bot.send_message(chat_id, text, reply_markup=admin_user_keyboard(user), parse_mode="HTML")
    await send_admin_user_offers(chat_id, target_id, 0)


async def render_admin_bulk_selection(state: FSMContext):
    data = await state.get_data()
    user_ids = data.get("bulk_user_ids", [])
    selected = set(data.get("bulk_selected_ids", [])) & set(user_ids)
    if not user_ids:
        return "Пользователи не выбраны.", kb([[button("↩️ Админ-панель", "menu:admin")]])
    page_size = 20
    pages = max(1, (len(user_ids) + page_size - 1) // page_size)
    page = max(0, min(int(data.get("bulk_page", 0)), pages - 1))
    page_ids = user_ids[page * page_size:(page + 1) * page_size]
    placeholders = ",".join("?" for _ in page_ids)
    users = await rows_all(f"SELECT user_id,username,balance,blocked FROM users WHERE user_id IN ({placeholders})", tuple(page_ids))
    user_map = {row["user_id"]: row for row in users}
    lines = [
        f"👥 <b>ПОЛЬЗОВАТЕЛИ · выбрано {len(selected)} из {len(user_ids)} · страница {page + 1}/{pages}</b>",
        "Отметь пользователей на странице или выдели всех; «Профиль» открывает профиль и офферы.",
        "",
    ]
    buttons = []
    for index, user_id in enumerate(page_ids, start=page * page_size + 1):
        user = user_map.get(user_id)
        if not user:
            continue
        display = f"@{user['username']}" if user["username"] else f"ID {user_id}"
        blocked = " · заблокирован" if user["blocked"] else ""
        lines.append(f"{index}. {html.escape(display)} · ID <code>{user_id}</code> · баланс {user['balance']} ⭐{blocked}")
        mark = "☑️" if user_id in selected else "☐"
        buttons.append([
            button(f"{mark} {display} · {user_id}", f"bulk:toggle:{user_id}"),
            button("👤 Профиль", f"user:refresh:{user_id}"),
        ])
    missing = data.get("bulk_missing", [])
    if missing:
        preview = ", ".join(html.escape(item) for item in missing[:10])
        more = f" и ещё {len(missing) - 10}" if len(missing) > 10 else ""
        lines.append(f"\nНе найдены ({len(missing)}): {preview}{more}")
    nav = []
    if page > 0:
        nav.append(button("◀️ Назад", f"bulk:page:{page - 1}"))
    if page + 1 < pages:
        nav.append(button("Далее ▶️", f"bulk:page:{page + 1}"))
    if nav:
        buttons.append(nav)
    all_selected = len(selected) == len(user_ids)
    buttons.append([button("⬜ Снять выделение" if all_selected else "☑️ Выделить всех", "bulk:all")])
    buttons.append([
        button("➕ Начислить выбранным", "bulk:add", "success"),
        button("➖ Списать выбранным", "bulk:remove", "danger"),
    ])
    buttons.append([button("⭐ Выдать Plus выбранным", "bulk:subscription", "primary")])
    buttons.append([button("↩️ Админ-панель", "menu:admin")])
    return "\n".join(lines), kb(buttons)


async def send_admin_bulk_selection(chat_id: int, state: FSMContext):
    text, markup = await render_admin_bulk_selection(state)
    await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")


def parse_admin_user_tokens(raw: str):
    ignored = {"id", "user_id", "telegram_id", "username"}
    return list(dict.fromkeys(token.strip().lstrip("@") for token in re.split(r"[,;\s]+", raw) if token.strip() and token.strip().lower() not in ignored))


async def resolve_bulk_user_tokens(entries):
    numeric = list(dict.fromkeys(int(item) for item in entries if item.isdigit()))
    usernames = list(dict.fromkeys(item.lower() for item in entries if not item.isdigit()))
    found = {}
    for start in range(0, len(numeric), 400):
        chunk = numeric[start:start + 400]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        rows = await rows_all(f"SELECT user_id FROM users WHERE user_id IN ({placeholders})", tuple(chunk))
        found.update({str(row["user_id"]): row["user_id"] for row in rows})
    for start in range(0, len(usernames), 400):
        chunk = usernames[start:start + 400]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        rows = await rows_all(f"SELECT user_id,username FROM users WHERE lower(username) IN ({placeholders})", tuple(chunk))
        found.update({str(row["username"]).lower(): row["user_id"] for row in rows if row["username"]})
    user_ids, missing = [], []
    for item in entries:
        lookup = item if item.isdigit() else item.lower()
        user_id = found.get(lookup)
        if user_id is not None:
            if user_id not in user_ids:
                user_ids.append(user_id)
        else:
            missing.append(item)
    return user_ids, missing


async def finish_admin_bulk_lookup(message: Message, state: FSMContext, raw: str):
    entries = parse_admin_user_tokens(raw)
    if not entries:
        await message.answer("Не нашёл ID или @username в списке.")
        return
    user_ids, missing = await resolve_bulk_user_tokens(entries)
    if not user_ids:
        await message.answer("Не нашёл пользователей. Аккаунт должен хотя бы один раз открыть основного бота.")
        return
    await state.update_data(bulk_user_ids=user_ids, bulk_selected_ids=[], bulk_missing=missing, bulk_page=0)
    await state.set_state(None)
    await send_admin_bulk_selection(message.chat.id, state)


@dp.message(AdminForm.lookup_id, F.document)
async def admin_lookup_users_file(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    name = (message.document.file_name or "").lower()
    if not name.endswith((".csv", ".txt")):
        await message.answer("Загрузи список как .csv или .txt: ID и @username через запятую, пробел или новую строку.")
        return
    buffer = io.BytesIO()
    try:
        await bot.download(message.document.file_id, destination=buffer)
        raw = buffer.getvalue().decode("utf-8-sig")
    except Exception:
        logging.exception("Could not read admin user list upload")
        await message.answer("Не удалось прочитать файл. Сохрани его как UTF-8 CSV/TXT и отправь ещё раз.")
        return
    await finish_admin_bulk_lookup(message, state, raw)


@dp.message(AdminForm.lookup_id)
async def admin_lookup_user(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer("Отправь ID/@username через запятую или загрузи CSV/TXT. Жёсткого лимита по числу записей нет.")
        return
    await finish_admin_bulk_lookup(message, state, value)


@dp.callback_query(F.data.startswith("bulk:"))
async def admin_bulk_action(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    data = await state.get_data()
    user_ids = data.get("bulk_user_ids", [])
    selected = set(data.get("bulk_selected_ids", [])) & set(user_ids)
    action_parts = callback.data.split(":")
    action = action_parts[1] if len(action_parts) > 1 else ""
    if action == "page":
        page = int(action_parts[2]) if len(action_parts) == 3 and action_parts[2].isdigit() else 0
        await state.update_data(bulk_page=page)
        text, markup = await render_admin_bulk_selection(state)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await callback.answer()
        return
    if action == "all":
        selected = set() if len(selected) == len(user_ids) else set(user_ids)
        await state.update_data(bulk_selected_ids=list(selected))
        text, markup = await render_admin_bulk_selection(state)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await callback.answer("Выделение обновлено")
        return
    if action == "toggle":
        target_id = int(action_parts[2]) if len(action_parts) == 3 and action_parts[2].isdigit() else None
        if target_id not in user_ids:
            await callback.answer("Пользователь не входит в этот список.", show_alert=True)
            return
        if target_id in selected:
            selected.remove(target_id)
        else:
            selected.add(target_id)
        await state.update_data(bulk_selected_ids=list(selected))
        text, markup = await render_admin_bulk_selection(state)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await callback.answer()
        return
    if not selected:
        await callback.answer("Сначала выбери пользователей или нажми «Выделить всех».", show_alert=True)
        return
    selected_ids = sorted(selected)
    await state.update_data(bulk_selected_ids=selected_ids)
    if action in {"add", "remove"}:
        await state.update_data(bulk_balance_action=action)
        await state.set_state(AdminForm.bulk_amount)
        verb = "начислить" if action == "add" else "списать"
        await callback.message.answer(f"Сколько ⭐ {verb} каждому из {len(selected_ids)} выбранных пользователей? Введи целое число.")
    elif action == "subscription":
        await state.set_state(AdminForm.bulk_subscription_days)
        await callback.message.answer(f"На сколько дней выдать тариф Plus каждому из {len(selected_ids)} выбранных пользователей?")
    else:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return
    await callback.answer()


@dp.callback_query(F.data.startswith("user:"))
async def admin_user_action(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, action, raw_id = callback.data.split(":")
    target_id = int(raw_id)
    user = await row_one("SELECT * FROM users WHERE user_id=?", (target_id,))
    if not user:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return
    if action in ("add", "remove"):
        await state.update_data(target_id=target_id, balance_action=action)
        await state.set_state(AdminForm.amount)
        await callback.message.answer("Введи целое количество ⭐ для изменения баланса.")
        await callback.answer()
        return
    if action == "grant":
        await state.update_data(target_id=target_id)
        await state.set_state(AdminForm.subscription_days)
        await callback.message.answer("На сколько дней выдать/продлить подписку? Введи число.")
        await callback.answer()
        return
    if action in {"virtual", "virtual_confirm"}:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.answer("Для новых офферов требуется доступный баланс; безлимит распространяется только на запросы парсера.", show_alert=True)
        await send_admin_user_card(callback.message.chat.id, target_id)
        return
    if action == "refresh":
        await send_admin_user_card(callback.message.chat.id, target_id)
        await callback.answer()
        return
    if action == "block":
        column, value = "blocked", 1
        event = "admin_block"
    elif action == "unblock":
        column, value = "blocked", 0
        event = "admin_unblock"
    else:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(f"UPDATE users SET {column}=? WHERE user_id=?", (value, target_id))
        await log_admin_action(db, callback.from_user.id, target_id, event, f"value={value}")
        await db.commit()
    await callback.answer("Изменение сохранено")
    await send_admin_user_card(callback.message.chat.id, target_id)


@dp.message(AdminForm.amount)
async def admin_user_balance_adjust(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw.isascii() or not raw.isdigit() or len(raw) > 19 or not 1 <= int(raw) <= MAX_SQLITE_INTEGER:
        await message.answer(f"Введи целое число от 1 до {MAX_SQLITE_INTEGER} ⭐ (технический предел базы).")
        return
    data = await state.get_data()
    target_id = data["target_id"]
    amount = int(raw) * (1 if data["balance_action"] == "add" else -1)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT balance,reserved FROM users WHERE user_id=?", (target_id,))
        account = await cur.fetchone()
        if not account or not 0 <= account["balance"] + amount <= MAX_SQLITE_INTEGER - account["reserved"]:
            await db.rollback()
            await message.answer("Недостаточно баланса для списания или превышен технический предел базы.")
            return
        await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, target_id))
        await ledger_entry(db, target_id, amount, 0, "admin_adjustment", None, message.from_user.id)
        await log_admin_action(db, message.from_user.id, target_id, "balance_adjustment", f"delta={amount}")
        await db.commit()
    await state.clear()
    await message.answer(f"✅ Баланс изменён на {amount:+d} ⭐.")
    try:
        await bot.send_message(target_id, f"Администратор изменил твой баланс на {amount:+d} ⭐.")
    except Exception:
        pass
    await send_admin_user_card(message.chat.id, target_id)


@dp.message(AdminForm.subscription_days)
async def admin_user_grant_subscription(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        await message.answer("Введи количество дней числом больше нуля.")
        return
    data = await state.get_data()
    target_id = data["target_id"]
    user = await row_one("SELECT premium_until FROM users WHERE user_id=?", (target_id,))
    base = now_local()
    if user and user["premium_until"]:
        try:
            old = datetime.fromisoformat(user["premium_until"])
            if old > base:
                base = old
        except ValueError:
            pass
    until = (base + timedelta(days=int(raw))).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET subscription_plan='plus',premium_until=? WHERE user_id=?", (until, target_id))
        await log_admin_action(db, message.from_user.id, target_id, "grant_subscription", f"days={raw}; until={until}")
        await db.commit()
    await state.clear()
    await message.answer(f"✅ Подписка выдана до {until}.")
    await send_admin_user_card(message.chat.id, target_id)


@dp.message(AdminForm.bulk_amount)
async def admin_bulk_balance_adjust(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if raw.lower() in {"/cancel", "отмена", "cancel"}:
        await state.set_state(None)
        await message.answer("Массовое изменение баланса отменено.")
        await send_admin_bulk_selection(message.chat.id, state)
        return
    if not raw.isascii() or not raw.isdigit() or len(raw) > 19 or not 1 <= int(raw) <= MAX_SQLITE_INTEGER:
        await message.answer(f"Введи целое число от 1 до {MAX_SQLITE_INTEGER} ⭐ (предел базы) или /cancel.")
        return
    data = await state.get_data()
    user_ids = list(dict.fromkeys(data.get("bulk_selected_ids", [])))
    action = data.get("bulk_balance_action")
    if not user_ids or action not in {"add", "remove"}:
        await state.set_state(None)
        await message.answer("Список выбранных пользователей потерян. Начни поиск заново.")
        return
    amount = int(raw) * (1 if action == "add" else -1)
    updated, skipped = [], []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        for user_id in user_ids:
            row = await (await db.execute("SELECT balance,reserved FROM users WHERE user_id=?", (user_id,))).fetchone()
            if not row or not 0 <= row["balance"] + amount <= MAX_SQLITE_INTEGER - row["reserved"]:
                skipped.append(user_id)
                continue
            await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
            await ledger_entry(db, user_id, amount, 0, "admin_bulk_adjustment", None, message.from_user.id,
                               f"Bulk {action}: {abs(amount)} units")
            await log_admin_action(db, message.from_user.id, user_id, "bulk_balance_adjustment", f"delta={amount}")
            updated.append(user_id)
        await db.commit()
    await state.set_state(None)
    verb = "начислено" if action == "add" else "списано"
    summary = f"✅ {amount if amount > 0 else -amount} ⭐ {verb} каждому из {len(updated)} пользователей."
    if skipped:
        summary += f"\n⏭ Пропущено из-за отсутствия пользователя, недостаточного баланса или предела базы: {len(skipped)}."
    await message.answer(summary)
    for user_id in updated:
        try:
            await bot.send_message(user_id, f"Администратор изменил баланс на {amount:+d} ⭐-единиц.")
        except Exception:
            pass
    await send_admin_bulk_selection(message.chat.id, state)


@dp.message(AdminForm.bulk_subscription_days)
async def admin_bulk_grant_subscription(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if raw.lower() in {"/cancel", "отмена", "cancel"}:
        await state.set_state(None)
        await message.answer("Массовая выдача подписки отменена.")
        await send_admin_bulk_selection(message.chat.id, state)
        return
    if not raw.isdigit() or not 1 <= int(raw) <= 3650:
        await message.answer("Введи число дней от 1 до 3650 или /cancel.")
        return
    data = await state.get_data()
    user_ids = list(dict.fromkeys(data.get("bulk_selected_ids", [])))
    if not user_ids:
        await state.set_state(None)
        await message.answer("Список выбранных пользователей потерян. Начни поиск заново.")
        return
    days = int(raw)
    expiries = []
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        for user_id in user_ids:
            plan, until = await activate_subscription(db, user_id, "plus", days)
            await log_admin_action(db, message.from_user.id, user_id, "bulk_grant_subscription", f"days={days};until={until}")
            expiries.append((user_id, until))
        await db.commit()
    await state.set_state(None)
    await message.answer(f"✅ Тариф Plus выдан {len(expiries)} пользователям на {days} дн.")
    for user_id, until in expiries:
        try:
            await bot.send_message(user_id, f"⭐ Администратор выдал/продлил тариф «{plan['name']}» до {until[:10]}.")
        except Exception:
            pass
    await send_admin_bulk_selection(message.chat.id, state)


async def resolve_user_id(target: str):
    target = target.strip()
    if target.isdigit():
        return int(target)
    username = target.lstrip("@").lower()
    row = await row_one("SELECT user_id FROM users WHERE lower(username)=?", (username,))
    return row["user_id"] if row else None


@dp.message(Command("credit"))
async def admin_credit(message: Message):
    if not await is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[2].lstrip("+-").isdigit():
        await message.answer("Формат: /credit <user_id или @username> <сумма>")
        return
    raw_amount = parts[2]
    digits = raw_amount.lstrip("+-")
    if not digits.isascii() or len(digits) > 19 or int(digits) > MAX_SQLITE_INTEGER or raw_amount.count('+') + raw_amount.count('-') > 1:
        await message.answer(f"Сумма превышает технический предел базы: {MAX_SQLITE_INTEGER} ⭐.")
        return
    target_id = await resolve_user_id(parts[1])
    amount = int(raw_amount)
    if not target_id:
        await message.answer("Пользователь не найден. Он должен хотя бы один раз открыть бота.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT balance,reserved FROM users WHERE user_id=?", (target_id,))
        account = await cur.fetchone()
        if not account or not 0 <= account["balance"] + amount <= MAX_SQLITE_INTEGER - account["reserved"]:
            await db.rollback()
            await message.answer("Недостаточно баланса для списания или превышен технический предел базы.")
            return
        await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, target_id))
        await ledger_entry(db, target_id, amount, 0, "admin_adjustment", None, message.from_user.id)
        await log_admin_action(db, message.from_user.id, target_id, "balance_adjustment", f"delta={amount}")
        await db.commit()
    await message.answer(f"✅ Баланс user_id {target_id} изменён на {amount} ⭐-единиц.")
    try:
        await bot.send_message(target_id, f"Администратор изменил твой баланс на {amount} ⭐-единиц.")
    except Exception:
        pass


@dp.message(Command("ledger"))
async def admin_ledger(message: Message):
    if not await is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Формат: /ledger <user_id или @username>")
        return
    target_id = await resolve_user_id(parts[1])
    if not target_id:
        await message.answer("Пользователь не найден.")
        return
    entries = await rows_all("SELECT * FROM balance_ledger WHERE user_id=? ORDER BY id DESC LIMIT 20", (target_id,))
    if not entries:
        await message.answer("Записей баланса пока нет.")
        return
    lines = []
    for item in entries:
        ref = f" · #{item['reference_id']}" if item['reference_id'] is not None else ""
        lines.append(f"{item['created_at'][:16]} · {html.escape(item['event'])}{ref} · доступно {item['delta_available']:+d} · резерв {item['delta_reserved']:+d}")
    await message.answer("📒 <b>Журнал баланса</b> · user_id <code>" + str(target_id) + "</code>\\n\\n" + "\\n".join(lines), parse_mode="HTML")


@dp.message(Command("grant"))
async def admin_grant(message: Message):
    if not await is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[2].isdigit():
        await message.answer("Формат: /grant <user_id или @username> <дни>")
        return
    target_id = await resolve_user_id(parts[1])
    if not target_id:
        await message.answer("Пользователь не найден.")
        return
    row = await row_one("SELECT premium_until FROM users WHERE user_id=?", (target_id,))
    base = now_local()
    if row and row["premium_until"]:
        try:
            old = datetime.fromisoformat(row["premium_until"])
            if old > base:
                base = old
        except ValueError:
            pass
    until = (base + timedelta(days=int(parts[2]))).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET subscription_plan='plus',premium_until=? WHERE user_id=?", (until, target_id))
        await log_admin_action(db, message.from_user.id, target_id, "grant_subscription", f"days={parts[2]}; until={until}")
        await db.commit()
    await message.answer(f"✅ Доступ выдан до {until}.")


@dp.message(Command("block"))
async def admin_block(message: Message):
    await admin_set_block(message, 1)


@dp.message(Command("unblock"))
async def admin_unblock(message: Message):
    await admin_set_block(message, 0)


async def admin_set_block(message, blocked):
    if not await is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Формат: /block <user_id или @username> или /unblock <user_id или @username>")
        return
    target_id = await resolve_user_id(parts[1])
    if not target_id:
        await message.answer("Пользователь не найден.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET blocked=? WHERE user_id=?", (blocked, target_id))
        await log_admin_action(db, message.from_user.id, target_id, "block_user" if blocked else "unblock_user")
        await db.commit()
    await message.answer("✅ Доступ закрыт." if blocked else "✅ Доступ восстановлен.")


@dp.message(Command("export"))
async def admin_export_command(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    await send_admin_export(message.chat.id, message.from_user.id)


@dp.message(Command("rate"))
async def admin_rate_command(message: Message):
    global USDT_PER_STAR
    if not await is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    parts = (message.text or "").split()
    if len(parts) == 1:
        await message.answer(
            f"💎 Текущий курс: {USDT_PER_STAR} USDT за 1 ⭐-единицу.\n"
            f"Crypto Pay токен: {'подключён' if CRYPTOPAY_TOKEN else 'не подключён — Railway Variables → CRYPTOPAY_TOKEN'}.\n\n"
            "Задать курс по цене пакета: <code>/rate 100 1.50</code> (100 единиц = 1.50 USDT).\n"
            "Выключить Crypto Pay: <code>/rate off</code>.", parse_mode="HTML")
        return
    now = now_local().isoformat()
    if len(parts) == 2 and parts[1].lower() in {"off", "0", "выкл"}:
        updated = Decimal("0")
        details = "Crypto Pay rate disabled"
    elif len(parts) == 3:
        try:
            units = int(parts[1])
            package_usdt = Decimal(parts[2].replace(",", "."))
        except (ValueError, InvalidOperation):
            units, package_usdt = 0, Decimal("0")
        if not package_usdt.is_finite() or units <= 0 or units > 1_000_000_000 or package_usdt <= 0 or package_usdt > 1_000_000:
            await message.answer("Неверное значение. Укажи пакет до 1 000 000 000 единиц и цену от 0 до 1 000 000 USDT. Пример: <code>/rate 100 1.50</code>", parse_mode="HTML")
            return
        try:
            updated = (package_usdt / Decimal(units)).quantize(Decimal("0.00000001"))
        except InvalidOperation:
            await message.answer("Не удалось рассчитать курс. Проверь числа и попробуй ещё раз.")
            return
        if updated <= 0:
            await message.answer("Получился слишком маленький курс для хранения. Увеличь цену или уменьши размер пакета.")
            return
        details = f"package_units={units}; package_usdt={package_usdt}; per_unit={updated}"
    else:
        await message.answer("Формат: <code>/rate 100 1.50</code> или <code>/rate off</code>.", parse_mode="HTML")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('usdt_per_star',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (str(updated), now))
        await log_admin_action(db, message.from_user.id, None, "set_usdt_per_star", details)
        await db.commit()
    USDT_PER_STAR = updated
    if updated == 0:
        await message.answer("✅ Crypto Pay отключён: курс сохранён как 0.")
    else:
        await message.answer(f"✅ Курс сохранён: {updated} USDT за 1 ⭐-единицу. Инвойсы рассчитываются с округлением до 0.01 USDT.")


@dp.message(Command("note"))
async def admin_note_command(message: Message):
    if not await is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 4 or parts[1] not in ("offer", "payment") or not parts[2].isdigit():
        await message.answer("Формат: /note offer|payment <id> <текст примечания>")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO admin_notes(admin_id,entity_type,entity_id,note,created_at) VALUES(?,?,?,?,?)",
                         (message.from_user.id, parts[1], int(parts[2]), parts[3], now_local().isoformat()))
        await db.commit()
    await message.answer("📝 Примечание сохранено.")


if group_dp is not None:
    # Reuse the same administrator-only bonus logic with the group bot's own session/FSM.
    group_dp.message.register(save_group_bonus_amount, AdminForm.group_bonus_amount)
    group_dp.message.register(save_group_bonus_period, AdminForm.group_bonus_period)
    group_dp.message.register(fund_group_bonus_pool, AdminForm.group_bonus_fund)
    group_dp.callback_query.register(admin_group_bonus_action, F.data.startswith("bonus:"))

    async def send_group_private_home(chat_id, user_id):
        rows = [[button("👤 Мой профиль", "groupuser:profile", "primary")]]
        if await is_admin(user_id):
            rows.append([button("⚙️ Настроить пополнение", "groupadmin:bonus", "success")])
        await group_bot.send_message(
            chat_id,
            "👋 <b>ГРУППОВОЙ БОТ</b>\n\n"
            "Здесь можно посмотреть профиль и внутренний баланс, общий с оффер-ботом. "
            "Пополнение по групповой акции доступно в настроенной группе, пока в резерве есть средства.",
            parse_mode="HTML", reply_markup=kb(rows),
        )

    async def send_group_profile(chat_id, user):
        await ensure_user(user)
        row = await row_one("SELECT balance,reserved FROM users WHERE user_id=?", (user.id,))
        stats = await row_one("SELECT COUNT(*) AS n FROM offers WHERE buyer_id=? AND status!='draft'", (user.id,))
        username = "@" + html.escape(user.username) if user.username else "не указан"
        await group_bot.send_message(
            chat_id,
            "👤 <b>МОЙ ПРОФИЛЬ</b>\n\n"
            f"🆔 ID: <code>{user.id}</code>\n"
            f"💬 Username: {username}\n\n"
            f"💰 Доступно: <b>{row['balance']:,} ⭐-единиц</b>\n"
            f"🔒 В резерве: <b>{row['reserved']:,} ⭐-единиц</b>\n"
            f"🎁 Создано офферов: <b>{stats['n']}</b>\n\n"
            "Это внутренний баланс сервиса, общий с оффер-ботом, а не Stars в Telegram.",
            parse_mode="HTML", reply_markup=kb([[button("↩️ Меню", "groupuser:home")]]),
        )

    @group_dp.message(Command("start", "profile", "admin", "bonus"), F.chat.type == "private")
    async def group_private_admin(message: Message):
        await ensure_user(message.from_user)
        cmd = (message.text or "").split()[0].split("@", 1)[0].lower()
        if cmd in {"/admin", "/bonus"}:
            if not await is_admin(message.from_user.id):
                await message.answer("Управление пополнением доступно только администраторам сервиса.")
                return
            await send_admin_group_bonus_settings(message.chat.id, group_bot)
        elif cmd == "/profile":
            await send_group_profile(message.chat.id, message.from_user)
        else:
            await send_group_private_home(message.chat.id, message.from_user.id)

    @group_dp.callback_query(F.data.in_({"groupuser:home", "groupuser:profile", "groupadmin:home", "groupadmin:bonus"}))
    async def group_admin_navigation(callback: CallbackQuery):
        if not callback.message or callback.message.chat.type != "private" or callback.message.chat.id != callback.from_user.id:
            await callback.answer("Доступно только в личном чате с ботом.", show_alert=True)
            return
        if callback.data == "groupadmin:bonus":
            if not await is_admin(callback.from_user.id):
                await callback.answer("Только для администраторов сервиса.", show_alert=True)
                return
            await send_admin_group_bonus_settings(callback.message.chat.id, group_bot)
        elif callback.data == "groupuser:profile":
            await send_group_profile(callback.message.chat.id, callback.from_user)
        else:
            await send_group_private_home(callback.message.chat.id, callback.from_user.id)
        await callback.answer()

    def group_bonus_markup():
        return kb([[InlineKeyboardButton(text="💰 Пополнить баланс", callback_data="groupbonus:claim", style="success")]])


    async def send_group_temporary(message: Message, text: str, reply_markup=None, delete_after: int = 120):
        sent = await message.answer(text, reply_markup=reply_markup, parse_mode="HTML", disable_web_page_preview=True)
        async def delete_later():
            await asyncio.sleep(delete_after)
            try:
                await group_bot.delete_message(sent.chat.id, sent.message_id)
            except Exception:
                logging.debug("Could not auto-delete temporary group response; grant delete-message permission if desired")
            if message.text and message.text.startswith("/"):
                try:
                    await group_bot.delete_message(message.chat.id, message.message_id)
                except Exception:
                    logging.debug("Could not auto-delete group command; grant delete-message permission if desired")
        asyncio.create_task(delete_later())
        return sent


    @group_dp.message(F.new_chat_members)
    async def group_bonus_welcome(message: Message):
        if BONUS_GROUP_ID is None or message.chat.id != BONUS_GROUP_ID:
            return
        settings = await get_group_bonus_settings()
        for member in message.new_chat_members:
            if member.is_bot:
                continue
            display = html.escape(member.first_name or member.username or "Участник")
            mention = f'<a href="tg://user?id={member.id}">{display}</a>'
            text = f"👋 Добро пожаловать, {mention}!\nИнформация закреплена сверху — прочитай её внимательно. Если что-то непонятно, задавай вопросы."
            markup = group_bonus_markup() if settings["enabled"] else None
            try:
                await send_group_temporary(message, text, reply_markup=markup, delete_after=180)
            except Exception:
                logging.exception("Could not post group welcome message")
                break


    async def send_offer_leaderboard(message: Message, period_key: str):
        periods = {
            "day": (timedelta(hours=24), "ПОСЛЕДНИЕ 24 ЧАСА"),
            "week": (timedelta(days=7), "ПОСЛЕДНИЕ 7 ДНЕЙ"),
            "month": (timedelta(days=30), "ПОСЛЕДНИЕ 30 ДНЕЙ"),
        }
        if period_key not in periods:
            return
        duration, title = periods[period_key]
        end = now_moscow()
        start = end - duration
        start_db, end_db = start.isoformat(), end.isoformat()
        rows = await rows_all(
            "SELECT o.buyer_id, COALESCE(MAX(NULLIF(o.buyer_username,'')),MAX(u.username)) AS username, COUNT(*) AS total "
            "FROM offers o LEFT JOIN users u ON u.user_id=o.buyer_id "
            "WHERE o.created_at>=? AND o.created_at<=? AND o.status!='draft' "
            "GROUP BY o.buyer_id ORDER BY total DESC,o.buyer_id ASC LIMIT 10",
            (start_db, end_db),
        )
        total_row = await row_one("SELECT COUNT(*) AS total FROM offers WHERE created_at>=? AND created_at<=? AND status!='draft'", (start_db, end_db))
        total_offers = total_row["total"] if total_row else 0
        lines = [
            f"📊 <b>ТОП ОФФЕРОВ · {title}</b>",
            f"Период: {start.strftime('%d.%m %H:%M')}–{end.strftime('%d.%m %H:%M')} МСК",
            "",
        ]
        if rows:
            for place, row in enumerate(rows, start=1):
                username = row["username"]
                label = f"@{html.escape(username)}" if username else f'<a href="tg://user?id={row["buyer_id"]}">Участник</a>'
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(place, f"{place}.")
                lines.append(f"{medal} {label} — <b>{row['total']}</b> офферов")
        else:
            lines.append("За этот период офферов пока нет.")
        lines.extend(["", f"Всего отправлено: <b>{total_offers}</b>", "Черновики не учитываются; считаются офферы, созданные через основного бота."])
        await send_group_temporary(message, "\n".join(lines), delete_after=180)


    @group_dp.message(Command("day"))
    async def group_day_command(message: Message):
        if BONUS_GROUP_ID is None or message.chat.id != BONUS_GROUP_ID:
            return
        await send_offer_leaderboard(message, "day")


    @group_dp.message(Command("week"))
    async def group_week_command(message: Message):
        if BONUS_GROUP_ID is None or message.chat.id != BONUS_GROUP_ID:
            return
        await send_offer_leaderboard(message, "week")


    @group_dp.message(Command("month"))
    async def group_month_command(message: Message):
        if BONUS_GROUP_ID is None or message.chat.id != BONUS_GROUP_ID:
            return
        await send_offer_leaderboard(message, "month")


    @group_dp.message(Command("help"))
    async def group_help_command(message: Message):
        if BONUS_GROUP_ID is None or message.chat.id != BONUS_GROUP_ID:
            return
        help_text = (
            "ℹ️ <b>КОМАНДЫ ГРУППЫ</b>\n"
            "/day — топ офферов за 24 часа\n"
            "/week — топ за 7 дней\n"
            "/month — топ за 30 дней\n"
            "/bonus — показать кнопку пополнения по акции\n\n"
            "Команды также доступны в меню бота. Это сообщение удалится через минуту."
        )
        await send_group_temporary(message, help_text, delete_after=60)


    @group_dp.message(Command("bonus"))
    async def group_bonus_command(message: Message):
        if BONUS_GROUP_ID is None or message.chat.id != BONUS_GROUP_ID:
            return
        settings = await get_group_bonus_settings()
        if not settings["enabled"]:
            await send_group_temporary(message, "💰 Пополнение по групповой акции сейчас приостановлено.", delete_after=60)
            return
        await send_group_temporary(
            message,
            f"💰 Пополнение по акции: <b>{settings['amount']:,} ⭐-единиц</b> на внутренний баланс · повторно раз в {settings['period']} ч., пока есть резерв.",
            reply_markup=group_bonus_markup(), delete_after=60,
        )


    @group_dp.callback_query(F.data == "groupbonus:claim")
    async def group_bonus_claim(callback: CallbackQuery):
        if BONUS_GROUP_ID is None or callback.message is None or callback.message.chat.id != BONUS_GROUP_ID:
            await callback.answer("Кнопка доступна только в настроенной группе.", show_alert=True)
            return
        try:
            member = await group_bot.get_chat_member(BONUS_GROUP_ID, callback.from_user.id)
            status = getattr(member.status, "value", str(member.status))
            member_ok = status in {"creator", "administrator", "member"} or (status == "restricted" and bool(getattr(member, "is_member", False)))
        except Exception:
            logging.exception("Group membership verification failed")
            await callback.answer("Не удалось проверить участие. Попробуй позже или сообщи администратору группы.", show_alert=True)
            return
        if not member_ok:
            await callback.answer("Чтобы получить пополнение по акции, нужно состоять в группе.", show_alert=True)
            return
        await ensure_user(callback.from_user)
        now = now_local()
        stamp = now.isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            setting_rows = await (await db.execute("SELECT key,value FROM app_settings WHERE key IN ('group_bonus_amount','group_bonus_period_hours','group_bonus_pool','group_bonus_enabled')")).fetchall()
            cfg = {row["key"]: row["value"] for row in setting_rows}
            if cfg.get("group_bonus_enabled") != "1":
                await db.rollback()
                await callback.answer("Акция сейчас приостановлена.", show_alert=True)
                return
            amount = int(cfg.get("group_bonus_amount", "0"))
            period = int(cfg.get("group_bonus_period_hours", "24"))
            pool = int(cfg.get("group_bonus_pool", "0"))
            if amount < 1 or period < 1:
                await db.rollback()
                await callback.answer("Пополнение по акции пока не настроено.", show_alert=True)
                return
            user = await (await db.execute("SELECT balance,blocked FROM users WHERE user_id=?", (callback.from_user.id,))).fetchone()
            if not user or user["blocked"]:
                await db.rollback()
                await callback.answer("Не удалось начислить средства на этот аккаунт.", show_alert=True)
                return
            previous = await (await db.execute("SELECT claimed_at FROM group_bonus_claims WHERE user_id=? ORDER BY id DESC LIMIT 1", (callback.from_user.id,))).fetchone()
            if previous:
                try:
                    last_claim = datetime.fromisoformat(previous["claimed_at"])
                except ValueError:
                    last_claim = now
                ready_at = last_claim + timedelta(hours=period)
                if ready_at > now:
                    remaining = ready_at - now
                    hours = max(1, int((remaining.total_seconds() + 3599) // 3600))
                    await db.rollback()
                    await callback.answer(f"Следующее пополнение по акции доступно примерно через {hours} ч.", show_alert=True)
                    return
            if pool < amount:
                await db.rollback()
                await callback.answer("Резерв акции временно закончился.", show_alert=True)
                return
            await db.execute("UPDATE app_settings SET value=?,updated_at=? WHERE key='group_bonus_pool'", (str(pool - amount), stamp))
            await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, callback.from_user.id))
            await db.execute("INSERT INTO group_bonus_claims(user_id,group_id,amount,claimed_at) VALUES(?,?,?,?)", (callback.from_user.id, BONUS_GROUP_ID, amount, stamp))
            await ledger_entry(db, callback.from_user.id, amount, 0, "group_daily_bonus", None, None, f"Group bonus claim; pool remaining={pool - amount}")
            await db.commit()
        await callback.answer(f"Начислено {amount:,} ⭐! Следующее получение через {period} ч.", show_alert=True)



# Telegram profile Chat Automation is connected-bot MTProto, not Bot API Business updates.
async def persist_business_connection(connection):
    owner_id = int(connection.user_id)
    enabled = not bool(getattr(connection, "disabled", False))
    rights = getattr(connection, "rights", None)
    can_reply = bool(getattr(rights, "reply", False)) if rights else False
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO business_connections(connection_id,owner_user_id,dc_id,can_reply,is_enabled,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(connection_id) DO UPDATE SET owner_user_id=excluded.owner_user_id,dc_id=excluded.dc_id,can_reply=excluded.can_reply,is_enabled=excluded.is_enabled,updated_at=excluded.updated_at",
            (connection.connection_id, owner_id, int(connection.dc_id), int(can_reply), int(enabled), now_local().isoformat()),
        )
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id,username,created_at) VALUES(?,NULL,?)",
            (owner_id, now_local().isoformat()),
        )
        await db.commit()
    logging.info("Offers profile connection saved: owner=%s enabled=%s can_reply=%s", owner_id, enabled, can_reply)


async def get_business_connection(connection_id: str, refresh: bool = False):
    row = await row_one("SELECT * FROM business_connections WHERE connection_id=?", (connection_id,))
    if row and not refresh:
        return row
    if not offers_mtproto_client:
        return None
    try:
        result = await offers_mtproto_client(functions.account.GetBotBusinessConnectionRequest(connection_id))
        updates = getattr(result, "updates", None) or []
        for update in updates:
            connection = getattr(update, "connection", None)
            if connection and getattr(connection, "connection_id", None) == connection_id:
                await persist_business_connection(connection)
                return await row_one("SELECT * FROM business_connections WHERE connection_id=?", (connection_id,))
        connection = getattr(result, "connection", None)
        if connection and getattr(connection, "connection_id", None) == connection_id:
            await persist_business_connection(connection)
            return await row_one("SELECT * FROM business_connections WHERE connection_id=?", (connection_id,))
    except Exception:
        logging.exception("Could not fetch connected profile details from Telegram")
    return None


async def business_call(connection_row, request):
    """Invoke a business-ready MTProto method on the profile connection's Telegram DC."""
    if not offers_mtproto_client:
        raise RuntimeError("MTProto profile automation is not configured")
    dc_id = int(connection_row["dc_id"])
    wrapped = functions.InvokeWithBusinessConnectionRequest(
        connection_id=connection_row["connection_id"], query=request,
    )
    # ExportAuthorization cannot export to the same DC as the current session.
    if dc_id == offers_mtproto_client.session.dc_id:
        return await offers_mtproto_client(wrapped)
    sender = await offers_mtproto_client._borrow_exported_sender(dc_id)
    try:
        return await offers_mtproto_client._call(sender, wrapped)
    finally:
        await offers_mtproto_client._return_exported_sender(sender)


async def notify_offer_chat(offer, text):
    if not offer['business_connection_id'] or not offers_mtproto_client:
        return
    try:
        connection = await get_business_connection(offer['business_connection_id'])
        if not connection or not connection['is_enabled'] or not connection['can_reply']:
            return
        peer = await offers_mtproto_client.get_input_entity(tl_types.PeerUser(offer['business_chat_id']))
        await business_send_text(connection, peer, text, no_webpage=True)
    except Exception:
        logging.exception("Could not notify connected chat about offer %s", offer['id'])


def business_offer_markup(offer_id: int):
    return tl_types.ReplyInlineMarkup(rows=[tl_types.KeyboardInlineButtonRow(buttons=[
        tl_types.KeyboardInlineButton("⊗ Отклонить", tl_types.InlineButtonTypeCallback(data=f"reject:{offer_id}".encode())),
        tl_types.KeyboardInlineButton("✓ Принять", tl_types.InlineButtonTypeCallback(data=f"accept:{offer_id}".encode())),
    ])])


async def send_business_button_diagnostics(connection_row, peer, chat_id):
    """Send two harmless test buttons through independent Telegram APIs; no deal is created."""
    tl_markup = tl_types.ReplyInlineMarkup(rows=[tl_types.KeyboardInlineButtonRow(buttons=[
        tl_types.KeyboardInlineButton("🧪 Проверка A", tl_types.InlineButtonTypeCallback(data=b"diag:ping")),
    ])])
    try:
        await business_send_text(connection_row, peer, "🧪 ТЕСТ A: под этим сообщением должна быть кнопка. Сделка не создаётся.", reply_markup=tl_markup, no_webpage=True)
        logging.info("Business button diagnostic A: sent")
    except Exception:
        logging.exception("Business button diagnostic A failed")
    try:
        await bot.send_message(
            chat_id=chat_id, business_connection_id=connection_row['connection_id'],
            text="🧪 ТЕСТ B: под этим сообщением должна быть кнопка. Сделка не создаётся.",
            reply_markup=kb([[button("🧪 Проверка B", "diag:ping")]]),
        )
        logging.info("Business button diagnostic B: sent")
    except Exception:
        logging.exception("Business button diagnostic B failed")


@dp.callback_query(F.data == "diag:ping")
async def business_diagnostic_bot_api_callback(callback: CallbackQuery):
    await callback.answer("Тестовая кнопка работает. Сделка не создавалась.", show_alert=True)


def business_confirm_markup(offer_id: int):
    return tl_types.ReplyInlineMarkup(rows=[tl_types.KeyboardInlineButtonRow(buttons=[
        tl_types.KeyboardInlineButton("✅ Я передал(а) подарок", tl_types.InlineButtonTypeCallback(data=f"confirm:{offer_id}".encode()), tl_types.KeyboardButtonStyle(bg_primary=True)),
    ])])


def business_offer_text(row):
    nft_name = row['nft_title'] or 'NFT-подарок'
    nft_number = row['nft_number']
    nft_label = f"{nft_name} #{nft_number}" if nft_number else nft_name
    buyer = f"@{row['buyer_username']}" if row['buyer_username'] else f"Пользователь {row['buyer_id']}"
    link = html.escape(row['gift_url'], quote=True)
    return (
        f"<b>{html.escape(buyer)}</b> предлагает вам <b>{row['amount']:,} ⭐-единиц</b> "
        f"за подарок <a href=\"{link}\"><b>{html.escape(nft_label)}</b></a>.\n\n"
        f"⏳ Предложение действует {OFFER_TTL_HOURS} ч. с момента отправки.\n"
        "⭐ — внутренние единицы сервиса, не перевод Telegram Stars."
    )


def business_sent_message_id(result):
    direct = getattr(result, "id", None)
    if isinstance(direct, int) and direct > 0:
        return direct
    updates = getattr(result, "updates", None) or []
    for update in updates:
        if isinstance(update, tl_types.UpdateMessageID) and update.id > 0:
            return update.id
    for update in updates:
        message = getattr(update, "message", None)
        if message and getattr(message, "out", False) and getattr(message, "id", 0) > 0:
            return message.id
    return None


async def business_send_text(connection_row, peer, text, reply_markup=None, no_webpage=False, entities=None):
    request = functions.messages.SendMessageRequest(
        peer=peer, message=text,
        no_webpage=no_webpage, reply_markup=reply_markup, entities=entities,
    )
    return await business_call(connection_row, request)


async def business_edit_text(connection_row, peer, message_id: int, text: str, reply_markup=None, no_webpage=False):
    request = functions.messages.EditMessageRequest(
        peer=peer, id=message_id, message=text,
        no_webpage=no_webpage, reply_markup=reply_markup,
    )
    return await business_call(connection_row, request)


async def handle_business_connection_event(event):
    try:
        await persist_business_connection(event.connection)
    except Exception:
        logging.exception("Could not save Telegram profile connection event")


async def handle_business_message_event(event):
    message = event.message
    connection_id = getattr(event, "connection_id", None)
    if not connection_id or not isinstance(getattr(message, "peer_id", None), tl_types.PeerUser):
        return
    connection_row = await get_business_connection(connection_id)
    if not connection_row or not connection_row["is_enabled"]:
        return
    sender_id = getattr(getattr(message, "from_id", None), "user_id", None)
    if sender_id is None or int(sender_id) != int(connection_row["owner_user_id"]):
        return
    text = (getattr(message, "message", None) or "").strip()
    parts = text.split()
    if not parts:
        return
    command = parts[0].split("@", 1)[0].lower()
    if command not in {"/offer", "/testbuttons"}:
        return
    if not connection_row["can_reply"]:
        refreshed = await get_business_connection(connection_id, refresh=True)
        if refreshed:
            connection_row = refreshed
    if not connection_row["is_enabled"]:
        return
    if not connection_row["can_reply"]:
        try:
            await bot.send_message(connection_row["owner_user_id"], "Разреши Offers-боту отвечать в настройках «Автоматизация чатов», затем повтори /offer.")
        except Exception:
            logging.info("Could not DM profile owner about missing reply permission")
        return
    try:
        peer = await offers_mtproto_client.get_input_entity(message.peer_id)
    except Exception:
        logging.exception("Could not resolve the target private chat for /offer")
        return
    if command == "/testbuttons":
        await send_business_button_diagnostics(connection_row, peer, message.peer_id.user_id)
        return
    if len(parts) != 3:
        await business_send_text(
            connection_row, peer,
            "Формат: /offer https://t.me/nft/Название-123 250\nУкажи ссылку на NFT и цену. Получатель NFT фиксирован: @staroffers_bank.",
            no_webpage=True,
        )
        return
    raw_link, raw_amount = parts[1], parts[2]
    if not raw_amount.isdigit() or not 1 <= int(raw_amount) <= 1_000_000:
        await business_send_text(connection_row, peer, "Цена должна быть целым числом от 1 до 1 000 000 внутренних ⭐.", no_webpage=True)
        return
    normalized = normalize_nft_link(raw_link)
    if not normalized:
        await business_send_text(connection_row, peer, "Нужна публичная ссылка вида https://t.me/nft/Название-123.", no_webpage=True)
        return
    if OFFERS_PAUSED:
        await business_send_text(connection_row, peer, "⏸ Создание новых офферов временно приостановлено.", no_webpage=True)
        return
    amount = int(raw_amount)
    nft = await fetch_nft_metadata(normalized[0])
    if not nft:
        await business_send_text(connection_row, peer, "Не удалось проверить публичную страницу NFT. Проверь ссылку и повтори /offer.", no_webpage=True)
        return
    now = now_local().isoformat()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            existing = await (await db.execute(
                "SELECT id FROM offers WHERE business_connection_id=? AND business_command_id=?",
                (connection_id, message.id),
            )).fetchone()
            if existing:
                await db.rollback()
                return
            account = await (await db.execute("SELECT balance,blocked,username FROM users WHERE user_id=?", (connection_row["owner_user_id"],))).fetchone()
            if not account or account["blocked"]:
                await db.rollback()
                await business_send_text(connection_row, peer, "Оффер не создан: аккаунт недоступен. Обратись в поддержку.", no_webpage=True)
                return
            if account["balance"] < amount:
                await db.rollback()
                await business_send_text(connection_row, peer, f"Оффер не создан: недостаточно доступного внутреннего баланса. Нужно {amount} ⭐; пополни баланс в основном боте.", no_webpage=True)
                try:
                    await bot.send_message(connection_row["owner_user_id"], f"Не удалось создать оффер: нужно {amount} доступных ⭐. Пополни баланс и повтори /offer.")
                except Exception:
                    pass
                return
            cursor = await db.execute(
                "INSERT INTO offers(buyer_id,buyer_username,receiver_username,gift_url,amount,nft_title,nft_number,status,virtual_reserve,business_connection_id,business_chat_id,business_command_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,'open',0,?,?,?,?,?)",
                (connection_row["owner_user_id"], account["username"], GIFT_CUSTODY_USERNAME, normalized[0], amount,
                 nft["title"], nft["number"], connection_id, getattr(message.peer_id, "user_id", None), message.id, now, now),
            )
            offer_id = cursor.lastrowid
            await db.commit()
        offer = await row_one("SELECT * FROM offers WHERE id=?", (offer_id,))
        card_text, card_entities = telethon_html.parse(business_offer_text(offer))
        sent = await business_send_text(connection_row, peer, card_text, business_offer_markup(offer_id), no_webpage=False, entities=card_entities)
        sent_message_id = business_sent_message_id(sent)
        returned_messages = [getattr(update, "message", None) for update in (getattr(sent, "updates", None) or [])]
        returned_messages = [message for message in returned_messages if getattr(message, "id", None) == sent_message_id]
        if returned_messages:
            returned_markup = getattr(returned_messages[0], "reply_markup", None)
            logging.info("Offer %s Telegram response: message_id=%s keyboard=%s rows=%s", offer_id, sent_message_id,
                         type(returned_markup).__name__ if returned_markup else "absent",
                         len(getattr(returned_markup, "rows", []) or []) if returned_markup else 0)
        else:
            logging.info("Offer %s Telegram response: message_id=%s response=%s; full message not returned, keyboard unknown",
                         offer_id, sent_message_id, type(sent).__name__)
        if sent_message_id is None:
            raise RuntimeError("Telegram did not return the published offer message ID")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE offers SET business_message_id=?,updated_at=? WHERE id=?", (sent_message_id, now_local().isoformat(), offer_id))
            await db.commit()
        # Delete only the owner's original command, only after the offer is published and saved.
        try:
            await business_call(connection_row, functions.messages.DeleteMessagesRequest(id=[message.id], revoke=True))
        except Exception:
            logging.exception("Offer %s published but the original command could not be deleted", offer_id)
            try:
                await bot.send_message(connection_row["owner_user_id"], "Оффер опубликован, но команду /offer не удалось удалить. Для автоудаления может потребоваться право удалять любые сообщения в настройках автоматизации; это широкое право. Или удали команду вручную.")
            except Exception:
                logging.info("Could not notify owner about undeleted offer command")
    except Exception:
        logging.exception("Could not publish offer created with connected profile")
        if "offer_id" in locals() and offer_id:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE offers SET status='expired',updated_at=? WHERE id=? AND status='open'", (now_local().isoformat(), offer_id))
                await db.commit()
        try:
            await business_send_text(connection_row, peer, "Не удалось опубликовать оффер. Проверь разрешения Offers-бота и повтори команду.", no_webpage=True)
        except Exception:
            logging.exception("Could not report failed connected-profile offer")


async def answer_business_callback(query_id: int, text: str, alert: bool = False):
    if not offers_mtproto_client:
        return
    try:
        await offers_mtproto_client(functions.messages.SetBotCallbackAnswerRequest(
            query_id=query_id, cache_time=0, message=text, alert=alert,
        ))
    except Exception:
        logging.exception("Could not answer connected-profile callback query")


async def confirm_business_transfer(event, connection_row, input_peer, peer, offer_id, user_id):
    now = now_local().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        offer = await (await db.execute("SELECT * FROM offers WHERE id=?", (offer_id,))).fetchone()
        if (not offer or offer["status"] != "awaiting_gift" or offer["seller_id"] != user_id
                or offer["buyer_id"] != connection_row["owner_user_id"]
                or offer["business_connection_id"] != event.connection_id
                or offer["business_chat_id"] != peer.user_id
                or offer["business_message_id"] != event.message.id
                or offer["receiver_username"] != GIFT_CUSTODY_USERNAME):
            await db.rollback()
            await answer_business_callback(event.query_id, "Подтверждение недоступно или уже отправлено.", True)
            return
        changed = await db.execute("UPDATE offers SET status='review_pending',updated_at=? WHERE id=? AND seller_id=? AND status='awaiting_gift'", (now, offer_id, user_id))
        if changed.rowcount != 1:
            await db.rollback()
            await answer_business_callback(event.query_id, "Статус сделки уже изменился.", True)
            return
        await log_admin_action(db, user_id, offer["buyer_id"], "business_transfer_claimed", f"offer_id={offer_id}; verification=manual")
        await db.commit()
    await answer_business_callback(event.query_id, "Заявка отправлена на ручную проверку.", True)
    final_text = (
        f"🔎 СДЕЛКА №{offer_id} · ОЖИДАЕТ ПРОВЕРКИ\n\n"
        f"Продавец сообщил о передаче NFT на @{GIFT_CUSTODY_USERNAME}. "
        "Это ещё не подтверждение получения: администратор вручную проверит подарок. "
        "После проверки выплата выполняется вручную.\n\n"
        "⚠️ Не передавай подарок повторно и не сообщай никому коды или пароль."
    )
    try:
        await business_edit_text(connection_row, input_peer, event.message.id, final_text, reply_markup=None, no_webpage=True)
    except Exception:
        logging.exception("Could not update transfer claim in chat for offer %s", offer_id)
        try:
            await business_send_text(connection_row, input_peer, final_text, no_webpage=True)
        except Exception:
            logging.exception("Could not send transfer claim status for offer %s", offer_id)
    admin_text = (
        f"🔎 <b>ПРОВЕРКА ПЕРЕДАЧИ · ОФФЕР #{offer_id}</b>\n\n"
        f"Продавец: <code>{user_id}</code>\nПокупатель: <code>{offer['buyer_id']}</code>\n"
        f"NFT: {html.escape(offer['gift_url'])}\nПолучатель: @{GIFT_CUSTODY_USERNAME}\n"
        f"Сумма: {offer['amount']} ⭐-единиц\n\n"
        "Продавец только заявил о передаче; ссылка-доказательство не предоставлялась. "
        "Проверь реальное получение именно этого NFT на аккаунте сервиса перед подтверждением."
    )
    await admin_notify(admin_text, admin_offer_keyboard(offer_id, "review"))
    try:
        await bot.send_message(offer['buyer_id'], f"🔎 Продавец сообщил о передаче по офферу #{offer_id}. Администратор проверит получение вручную.")
    except Exception:
        logging.info("Could not notify buyer about transfer claim %s", offer_id)


async def handle_business_callback_event(event):
    connection_id = getattr(event, "connection_id", None)
    message = getattr(event, "message", None)
    data = getattr(event, "data", None) or b""
    user_id = int(getattr(event, "user_id", 0) or 0)
    if not connection_id or not message or not user_id:
        return
    if data == b"diag:ping":
        await answer_business_callback(event.query_id, "Тестовая кнопка работает. Сделка не создавалась.", True)
        return
    try:
        action, raw_offer_id = data.decode("utf-8").split(":", 1)
        offer_id = int(raw_offer_id)
    except (UnicodeDecodeError, ValueError):
        await answer_business_callback(event.query_id, "Кнопка недействительна.", True)
        return
    if action not in {"accept", "reject", "confirm"}:
        await answer_business_callback(event.query_id, "Неизвестное действие.", True)
        return
    if await is_blocked(user_id):
        await answer_business_callback(event.query_id, "Доступ ограничен.", True)
        return
    seller_username = None
    try:
        seller_entity = await offers_mtproto_client.get_entity(user_id)
        seller_username = getattr(seller_entity, "username", None)
    except Exception:
        logging.info("Could not resolve seller username for user %s", user_id)
    connection_row = await get_business_connection(connection_id)
    if not connection_row or not connection_row["is_enabled"]:
        await answer_business_callback(event.query_id, "Подключение профиля отключено.", True)
        return
    peer = getattr(message, "peer_id", None)
    try:
        input_peer = await offers_mtproto_client.get_input_entity(peer)
    except Exception:
        input_peer = None
    if input_peer is None:
        await answer_business_callback(event.query_id, "Не удалось определить чат сделки. Напиши в поддержку.", True)
        return
    if not isinstance(peer, tl_types.PeerUser) or peer.user_id != user_id:
        await answer_business_callback(event.query_id, "Эта кнопка доступна только собеседнику сделки.", True)
        return
    if action == "confirm":
        await confirm_business_transfer(event, connection_row, input_peer, peer, offer_id, user_id)
        return
    now = now_local().isoformat()
    result = None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        offer = await (await db.execute("SELECT * FROM offers WHERE id=?", (offer_id,))).fetchone()
        if (not offer or offer["status"] != "open" or offer["buyer_id"] != connection_row["owner_user_id"]
                or offer["business_connection_id"] != connection_id or offer["business_message_id"] != message.id
                or offer["business_chat_id"] != peer.user_id or offer["buyer_id"] == user_id):
            await db.rollback()
            await answer_business_callback(event.query_id, "Оффер уже недоступен или это собственный оффер.", True)
            return
        if offer["receiver_username"] != GIFT_CUSTODY_USERNAME:
            await db.rollback()
            await answer_business_callback(event.query_id, "Оффер создан по старым правилам; попроси создать новый.", True)
            return
        await db.execute("INSERT OR IGNORE INTO users(user_id,username,created_at) VALUES(?,?,?)", (user_id, seller_username, now))
        if seller_username:
            await db.execute("UPDATE users SET username=? WHERE user_id=?", (seller_username, user_id))
        if action == "reject":
            changed = await db.execute("UPDATE offers SET status='rejected',rejected_by=?,updated_at=? WHERE id=? AND status='open'", (user_id, now, offer_id))
            if changed.rowcount != 1:
                await db.rollback()
                await answer_business_callback(event.query_id, "Оффер уже обработан.", True)
                return
            await log_admin_action(db, user_id, offer["buyer_id"], "business_offer_rejected", f"offer_id={offer_id}")
            await db.commit()
            result = ("reject", dict(offer))
        else:
            try:
                created = datetime.fromisoformat(offer["created_at"])
            except (TypeError, ValueError):
                created = now_local() - timedelta(days=3650)
            if now_local() - created > timedelta(hours=OFFER_TTL_HOURS):
                await db.execute("UPDATE offers SET status='expired',updated_at=? WHERE id=? AND status='open'", (now, offer_id))
                await db.commit()
                await answer_business_callback(event.query_id, "Срок действия оффера истёк.", True)
                return
            account = await (await db.execute("SELECT balance,blocked FROM users WHERE user_id=?", (offer["buyer_id"],))).fetchone()
            if not account or account["blocked"] or account["balance"] < offer["amount"]:
                await db.rollback()
                await answer_business_callback(event.query_id, "Недостаточно доступного баланса — принятие не сохранено.", True)
                try:
                    await bot.send_message(offer["buyer_id"], f"Оффер #{offer_id} не принят: нужно {offer['amount']} доступных ⭐. Пополни баланс и попроси продавца попробовать снова.")
                except Exception:
                    pass
                return
            changed = await db.execute(
                "UPDATE offers SET seller_id=?,seller_username=?,status='awaiting_gift',updated_at=? WHERE id=? AND status='open'",
                (user_id, seller_username, now, offer_id),
            )
            if changed.rowcount != 1:
                await db.rollback()
                await answer_business_callback(event.query_id, "Оффер уже обработан.", True)
                return
            await db.execute("UPDATE users SET balance=balance-?,reserved=reserved+? WHERE user_id=?", (offer["amount"], offer["amount"], offer["buyer_id"]))
            await ledger_entry(db, offer["buyer_id"], -offer["amount"], offer["amount"], "offer_reserve", offer_id, user_id)
            await log_admin_action(db, user_id, offer["buyer_id"], "business_offer_accepted", f"offer_id={offer_id}")
            await db.commit()
            result = ("accept", dict(offer))
    action, offer = result
    await answer_business_callback(event.query_id, "Принято" if action == "accept" else "Отклонено")
    if action == "reject":
        final_text = f"❌ ОФФЕР №{offer_id} ОТКЛОНЁН\n\nНичего не зарезервировано."
        markup = None
        buyer_notice = f"❌ Оффер #{offer_id} отклонён; баланс не менялся."
    else:
        final_text = (
            f"✅ ОФФЕР №{offer_id} ПРИНЯТ\n\n"
            f"🎁 Подарок: {offer['nft_title'] or 'NFT'} #{offer['nft_number'] or '—'}\n"
            f"🔗 {offer['gift_url']}\n"
            f"🔒 В резерве: {offer['amount']:,} ⭐-единиц внутреннего баланса.\n\n"
            f"1️⃣ Передай именно этот NFT на @{GIFT_CUSTODY_USERNAME} через интерфейс Telegram. Внимательно проверь username получателя перед отправкой.\n"
            "2️⃣ После фактической передачи нажми кнопку ниже. Администратор вручную проверит получение и выполнит выплату.\n\n"
            "⚠️ БЕЗОПАСНОСТЬ\n"
            "• Не отправляй подарок напрямую покупателю или на другой аккаунт, даже если тебя об этом просят.\n"
            "• Не переходи по подозрительным ссылкам, не вводи коды входа и пароль Telegram, не подключай кошелёк по просьбе собеседника.\n"
            "• Не плати за ‘разблокировку выплаты’ или ‘проверку’. При сомнениях остановись и свяжись с поддержкой через проверенный контакт.\n\n"
            "Кнопка сообщает администратору о передаче; сама по себе она не доказывает получение и не запускает автоматическую выплату."
        )
        markup = business_confirm_markup(offer_id)
        buyer_notice = f"✅ Продавец принял оффер #{offer_id}; {offer['amount']} ⭐ зарезервированы."
    try:
        await business_edit_text(connection_row, input_peer, message.id, final_text, reply_markup=markup, no_webpage=True)
    except Exception:
        logging.exception("Could not edit connected-profile offer %s; trying a separate status message", offer_id)
        try:
            sent = await business_send_text(connection_row, input_peer, final_text, reply_markup=markup, no_webpage=True)
            new_id = business_sent_message_id(sent)
            if action == "accept" and new_id:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE offers SET business_message_id=? WHERE id=? AND status='awaiting_gift' AND business_message_id=?", (new_id, offer_id, message.id))
                    await db.commit()
            elif action == "accept":
                logging.error("Fallback status for offer %s has no message ID; confirmation button may be unavailable", offer_id)
        except Exception:
            logging.exception("Could not send status message for connected-profile offer %s", offer_id)
    try:
        await bot.send_message(offer["buyer_id"], buyer_notice)
    except Exception:
        logging.info("Could not notify buyer about connected-profile offer %s", offer_id)
    if action == "accept":
        await admin_notify(f"🔒 Оффер №{offer_id} принят; сумма {offer['amount']} ⭐; buyer_id={offer['buyer_id']}.")


async def start_offers_mtproto():
    global offers_mtproto_client
    if offers_mtproto_client is not None:
        return
    if not MTPROTO_API_ID or not MTPROTO_API_HASH:
        logging.warning("MTPROTO_API_ID/HASH not configured; profile Chat Automation for offers is disabled; group features remain available")
        return
    # A separate session avoids accidentally reusing the old group bot's MTProto login.
    session_path = str(Path(DB_PATH).parent / "offers_main_bot")
    client = TelegramClient(session_path, MTPROTO_API_ID, MTPROTO_API_HASH)
    try:
        client.add_event_handler(handle_business_connection_event, events.Raw(types=tl_types.UpdateBotBusinessConnect))
        client.add_event_handler(handle_business_message_event, events.Raw(types=tl_types.UpdateBotNewBusinessMessage))
        client.add_event_handler(handle_business_callback_event, events.Raw(types=tl_types.UpdateBusinessBotCallbackQuery))
        offers_mtproto_client = client
        await client.start(bot_token=BOT_TOKEN)
        me = await client.get_me()
        expected_bot_id = int(BOT_TOKEN.split(':', 1)[0])
        if me.id != expected_bot_id:
            raise RuntimeError("MTProto session belongs to a different bot; stop and remove offers_main_bot.session only after backing it up")
        logging.info("Offers profile automation connected over MTProto as @%s", getattr(me, "username", None))
    except BaseException:
        offers_mtproto_client = None
        try:
            await client.disconnect()
        except Exception:
            logging.exception("Could not disconnect failed Offers MTProto client")
        raise


async def offers_mtproto_supervisor():
    if not MTPROTO_API_ID or not MTPROTO_API_HASH:
        logging.warning("Offers profile automation is disabled: configure MTPROTO_API_ID/HASH")
        return
    delay = 5
    while True:
        try:
            await start_offers_mtproto()
            delay = 5
            await offers_mtproto_client.disconnected
            logging.warning("Offers MTProto client disconnected; reconnecting")
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Offers MTProto connection failed; retrying in %s seconds", delay)
        finally:
            await stop_offers_mtproto()
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300)


async def stop_offers_mtproto():
    global offers_mtproto_client
    client = offers_mtproto_client
    offers_mtproto_client = None
    if client is None:
        return
    try:
        await client.disconnect()
    except Exception:
        logging.exception("Could not disconnect Offers MTProto client cleanly")


async def group_polling_supervisor():
    delay = 5
    while True:
        try:
            await group_dp.start_polling(group_bot, handle_signals=False, close_bot_session=False)
            logging.warning("Group bot polling stopped; retrying")
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Group bot polling failed; retrying in %s seconds", delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300)


async def main():
    await init_db()
    await start_card_web_server()
    delay = 5
    while True:
        try:
            bot_info = await bot.get_me()
            break
        except TelegramUnauthorizedError as exc:
            logging.error("Telegram отклонил BOT_TOKEN. Проверь настройки и выпусти новый токен при необходимости.")
            raise SystemExit(1) from exc
        except Exception:
            logging.exception("Telegram preflight failed; retrying in %s seconds", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
    logging.info("Connected to Telegram as @%s", bot_info.username)
    common_commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="help", description="Как создать и отправить оффер"),
    ]
    try:
        await bot.set_my_commands(common_commands, scope=BotCommandScopeDefault())
    except Exception:
        logging.exception("Could not publish default bot commands")
    admin_commands = common_commands + [
        BotCommand(command="admin", description="Открыть админ-панель"),
        BotCommand(command="credit", description="Изменить баланс пользователя"),
        BotCommand(command="ledger", description="Посмотреть журнал баланса"),
        BotCommand(command="grant", description="Выдать подписку"),
        BotCommand(command="block", description="Заблокировать пользователя"),
        BotCommand(command="unblock", description="Разблокировать пользователя"),
        BotCommand(command="note", description="Добавить примечание к сделке/платежу"),
        BotCommand(command="rate", description="Настроить курс Crypto Pay"),
        BotCommand(command="export", description="Скачать отчёты CSV"),
    ]
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            logging.exception("Could not publish admin commands for configured admin ID %s", admin_id)
    ban_middleware = BanMiddleware()
    dp.message.middleware(ban_middleware)
    dp.callback_query.middleware(ban_middleware)
    dp.inline_query.middleware(ban_middleware)
    asyncio.create_task(crypto_poll_loop())
    asyncio.create_task(maintenance_loop())
    asyncio.create_task(backup_loop())
    background_tasks = [asyncio.create_task(offers_mtproto_supervisor())]
    if group_bot and group_dp:
        try:
            await group_bot.set_my_commands([
                BotCommand(command="day", description="Топ офферов за 24 часа"),
                BotCommand(command="week", description="Топ офферов за неделю"),
                BotCommand(command="month", description="Топ офферов за месяц"),
                BotCommand(command="bonus", description="Пополнение баланса по акции"),
                BotCommand(command="help", description="Команды группового бота"),
            ])
        except Exception:
            logging.exception("Could not publish Offers/group bot commands")
        background_tasks.append(asyncio.create_task(group_polling_supervisor()))
    try:
        await dp.start_polling(bot)
    finally:
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await stop_offers_mtproto()


if __name__ == "__main__":
    asyncio.run(main())
