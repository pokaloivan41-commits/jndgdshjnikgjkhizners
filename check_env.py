"""Offline .env validator. Never prints secret values."""
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
errors = []
warnings = []

if not ENV_PATH.exists():
    print("ОШИБКА: файл .env не найден. Запусти run_bot.bat — он создаст шаблон.")
    raise SystemExit(1)

try:
    values = dotenv_values(ENV_PATH, encoding="utf-8-sig")
except Exception as exc:
    print(f"ОШИБКА: не удалось прочитать .env ({type(exc).__name__}). Сохрани файл как обычный текст UTF-8.")
    raise SystemExit(1)

token = (values.get("BOT_TOKEN") or "").strip().strip('"').strip("'")
if not token or token == "put_fresh_token_here":
    errors.append("BOT_TOKEN не заполнен. Вставь новый токен от @BotFather.")
elif not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
    errors.append("BOT_TOKEN имеет неверный формат. Нужен полный токен вида цифры:секрет, без пробелов.")
else:
    print("✓ BOT_TOKEN: формат заполнен (значение скрыто)")

raw_admins = (values.get("ADMIN_IDS") or "").strip()
admin_parts = [part.strip() for part in raw_admins.split(",") if part.strip()]
if not admin_parts or not all(part.isdigit() and int(part) > 0 for part in admin_parts):
    errors.append("ADMIN_IDS должен содержать числовой Telegram ID (например, 123456789), не @username.")
else:
    print(f"✓ ADMIN_IDS: указано администраторов — {len(admin_parts)}")

stars_username = (values.get("STAR_RECEIVE_USERNAME") or "").strip().lstrip("@")
if stars_username.lower() in {"your_username", "username", "replace_with_stars_receiver_username"} or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", stars_username):
    errors.append("STAR_RECEIVE_USERNAME должен быть username длиной 5–32 символа без @.")
else:
    print(f"✓ STAR_RECEIVE_USERNAME: @{stars_username}")

group_token = (values.get("GROUP_BOT_TOKEN") or "").strip().strip('"').strip("'")
raw_group_id = (values.get("BONUS_GROUP_ID") or "").strip()
raw_api_id = (values.get("MTPROTO_API_ID") or "").strip()
api_hash = (values.get("MTPROTO_API_HASH") or "").strip().strip('"').strip("'")
if bool(raw_api_id) != bool(api_hash):
    errors.append("Для MTProto укажи одновременно MTPROTO_API_ID и MTPROTO_API_HASH из my.telegram.org.")
if raw_api_id:
    if not raw_api_id.isdigit() or int(raw_api_id) <= 0:
        errors.append("MTPROTO_API_ID должен быть положительным числом.")
    if not re.fullmatch(r"[A-Fa-f0-9]{32}", api_hash):
        errors.append("MTPROTO_API_HASH должен содержать 32 шестнадцатеричных символа.")
if not raw_api_id or not api_hash:
    errors.append("Для офферов через «Автоматизацию чатов» укажи MTPROTO_API_ID и MTPROTO_API_HASH из my.telegram.org.")
else:
    print("✓ Оффер-бот: MTProto credentials заданы (секреты скрыты)")
if group_token:
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", group_token):
        errors.append("GROUP_BOT_TOKEN имеет неверный формат. Нужен токен отдельного группового бота.")
    if token == group_token:
        errors.append("BOT_TOKEN и GROUP_BOT_TOKEN должны принадлежать двум разным ботам.")
    if raw_group_id:
        if not re.fullmatch(r"-\d+", raw_group_id):
            errors.append("BONUS_GROUP_ID должен быть отрицательным числовым ID группы вида -100... .")
        else:
            print("✓ Групповые команды/бонус: группа задана; добавь группового бота администратором.")
    else:
        warnings.append("BONUS_GROUP_ID пуст: групповой бонус и рейтинги отключены.")
else:
    errors.append("GROUP_BOT_TOKEN пуст: укажи токен отдельного группового бота.")

raw_rate = (values.get("USDT_PER_STAR") or "0").strip() or "0"
try:
    rate = Decimal(raw_rate)
    if rate < 0:
        errors.append("USDT_PER_STAR не может быть отрицательным.")
except InvalidOperation:
    rate = Decimal("0")
    errors.append("USDT_PER_STAR должен быть числом с точкой (например, 0.02) или 0.")

cryptopay_token = (values.get("CRYPTOPAY_TOKEN") or "").strip()
if rate > 0 and not cryptopay_token:
    errors.append("Для включения Crypto Pay добавь CRYPTOPAY_TOKEN или оставь USDT_PER_STAR=0.")
elif rate <= 0:
    warnings.append("Crypto Pay выключен (USDT_PER_STAR=0); Stars-пополнение продолжит работать.")
else:
    print("✓ Crypto Pay: токен и курс заданы (секрет скрыт)")

for name, minimum in (("OFFER_TTL_HOURS", 1), ("BACKUP_RETENTION_DAYS", 1)):
    raw = (values.get(name) or "").strip()
    if raw:
        try:
            if int(raw) < minimum:
                errors.append(f"{name} должен быть не меньше {minimum}.")
        except ValueError:
            errors.append(f"{name} должен быть целым числом.")

if errors:
    print("\nПроверь настройки .env:")
    for error in errors:
        print(f"  • {error}")
    raise SystemExit(1)

for warning in warnings:
    print(f"ℹ {warning}")
print("\nНастройки .env выглядят корректно. Секреты не выводились на экран.")
