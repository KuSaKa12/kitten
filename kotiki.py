import os
import stat
import hmac
import hashlib
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import html
import aiosqlite
from dotenv import load_dotenv

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram import BaseMiddleware


load_dotenv()

# =========================================================================
# ШИФРОВАНИЕ: AES-256-GCM (AEAD) + BLIND INDEX (HMAC-SHA256) ДЛЯ ПОИСКА
# =========================================================================
# По 152-ФЗ персональные данные должны храниться защищённым способом (ст. 19
# — технические меры защиты). AES-GCM даёт конфиденциальность+целостность,
# но он НЕДЕТЕРМИНИРОВАН (случайный nonce на каждое шифрование), поэтому
# зашифрованный BLOB нельзя искать через "WHERE column = ?" — совпадений
# просто не будет.
#
# Для полей, по которым боту реально нужен точный поиск в БД (статус,
# пол/цель поиска, возрастная категория, идентификатор фото для защиты от
# дублей), рядом с зашифрованным значением хранится blind index —
# HMAC-SHA256(значение) на ОТДЕЛЬНОМ ключе. HMAC детерминирован: одинаковый
# вход всегда даёт одинаковый хэш, поэтому по нему можно делать
# WHERE col_idx = ?, но восстановить исходное значение из хэша нельзя.
#
# Важно понимать границы этого метода: если у злоумышленника есть ключ (тот
# же секрет, что защищает и AES-часть), он может подбором по словарю
# (например, все возможные статусы: idle/searching/playing/banned)
# сопоставить хэши со значениями для полей с маленьким числом вариантов.
# Это стандартный компромисс для "encryption with equality search" и
# рассматривается как техническая мера защиты (шифрование при хранении),
# а не как полная анонимизация. Полная анонимизация таких полей
# несовместима с тем, что бот должен подбирать пару по этим же критериям.
#
# ID пользователя Telegram (user_id / current_opponent / sender_id /
# receiver_id) НЕ шифруются: это операционный идентификатор, через который
# бот физически отправляет сообщения через Bot API, а также первичный и
# внешние ключи таблиц. Шифровать первичный ключ технически бессмысленно —
# для отправки сообщения он всё равно должен быть расшифрован в исходный
# int, и все JOIN/DELETE по нему перестанут работать без него же в
# открытом виде.
#
# Поля с диапазонным поиском (uploaded_at, chat_history.timestamp) также не
# шифруются — по вашему списку исключений и потому, что для дат нужен
# порядок (>, <), а не только равенство, что blind index не поддерживает.

_NONCE_LEN = 12
_TAG_LEN = 16


def get_encryption_key() -> bytes:
    env_key = os.getenv('ENCRYPTION_KEY')
    if not env_key:
        raise ValueError("ENCRYPTION_KEY не задан в переменных окружения!")

    if len(env_key) == 64:
        try:
            key = bytes.fromhex(env_key)
            if len(key) == 32:
                return key
        except ValueError:
            pass

    if len(env_key.encode('utf-8')) < 16:
        raise ValueError("ENCRYPTION_KEY слишком короткий (минимум 16 байт для режима с KDF).")
    salt = os.getenv('ENCRYPTION_KEY_SALT', 'kotoLOVe-v2-static-salt').encode('utf-8')
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b'kotoLOVe-aes-gcm-key')
    return hkdf.derive(env_key.encode('utf-8'))


def get_hmac_key() -> bytes:
    """Отдельный ключ для blind index. Выводится из того же секрета, что и
    ключ шифрования, но с другим 'info' в HKDF — так ключи криптографически
    независимы (компрометация одного не даёт другой напрямую), при этом
    не нужен второй секрет в переменных окружения."""
    base = get_encryption_key()
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b'kotoLOVe-hmac-blindindex-key')
    return hkdf.derive(base)


_KEY: Optional[bytes] = None
_HMAC_KEY: Optional[bytes] = None


def get_cached_key() -> bytes:
    global _KEY
    if _KEY is None:
        _KEY = get_encryption_key()
    return _KEY


def get_cached_hmac_key() -> bytes:
    global _HMAC_KEY
    if _HMAC_KEY is None:
        _HMAC_KEY = get_hmac_key()
    return _HMAC_KEY


def encrypt_data(plaintext: Optional[str]) -> Optional[bytes]:
    """Шифрует строку для хранения в БД (BLOB). None остаётся None."""
    if plaintext is None:
        return None
    key = get_cached_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode('utf-8'), None)
    return nonce + ciphertext


def decrypt_data(blob: Optional[bytes]) -> Optional[str]:
    """Обратная операция к encrypt_data. None остаётся None."""
    if blob is None:
        return None
    if len(blob) < _NONCE_LEN + _TAG_LEN:
        raise ValueError("Некорректные зашифрованные данные")
    key = get_cached_key()
    aesgcm = AESGCM(key)
    nonce, ciphertext = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode('utf-8')


def safe_decrypt(blob: Optional[bytes], context: str = "") -> Optional[str]:
    """decrypt_data с проглатыванием ошибки в лог вместо падения хендлера
    (например, если БД была повреждена или ключ сменился)."""
    if blob is None:
        return None
    try:
        return decrypt_data(blob)
    except Exception as e:
        logging.error(f"Не удалось расшифровать поле ({context}): {e}")
        return None


def blind_index(value: Optional[str]) -> Optional[str]:
    """Детерминированный псевдоним значения для точного поиска по
    зашифрованному полю (HMAC-SHA256, отдельный ключ). НЕ является
    шифрованием: восстановить исходное значение из индекса нельзя, но
    одинаковые входные значения всегда дают одинаковый индекс — этого
    достаточно для WHERE column_idx = ? без хранения самого значения в
    открытом виде."""
    if value is None:
        return None
    key = get_cached_hmac_key()
    return hmac.new(key, value.encode('utf-8'), hashlib.sha256).hexdigest()


def _crypto_selftest():
    """Самопроверка шифрования и blind index. Запускается только при
    DEBUG=1 и ничего чувствительного в лог/консоль не пишет."""
    original = "self-test-string"
    blob = encrypt_data(original)
    assert decrypt_data(blob) == original, "Расшифрованная строка не совпала с исходной"
    idx1 = blind_index(original)
    idx2 = blind_index(original)
    assert idx1 == idx2, "Blind index не детерминирован"
    assert idx1 != blind_index("другая строка"), "Blind index коллизия на разных значениях"
    logging.debug("Самопроверка шифрования и blind index пройдена успешно.")


class BanCheckMiddleware(BaseMiddleware):
    """Проверка бана по кэшу в памяти (banned_users_cache), а не запросом к
    БД на каждое апдейт-событие."""

    async def __call__(self, handler, event, data):
        user = event.from_user if hasattr(event, "from_user") else None
        if user is None:
            return await handler(event, data)

        if user.id in banned_users_cache:
            if isinstance(event, Message) and event.text and event.text.startswith("/delete_data"):
                return await handler(event, data)
            if isinstance(event, CallbackQuery) and event.data and event.data.startswith("delete_confirm_"):
                return await handler(event, data)

            if isinstance(event, Message):
                await event.answer("🚫 Вы заблокированы. Вы можете использовать только команду /delete_data, чтобы удалить свои данные.")
            elif isinstance(event, CallbackQuery):
                await event.answer("🚫 Вы заблокированы.", show_alert=True)
            return

        return await handler(event, data)


# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ (только ссылки, без инициализации) ---

db_conn = None
bot = None
dp = None
banned_users_cache: set = set()

CHANNEL_ID = "@ITkaktusik"
db_path = "/app/data/cat_game.db"
os.makedirs(os.path.dirname(db_path), exist_ok=True)

if os.path.exists(db_path):
    os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)

# --- БАЗА ДАННЫХ ---

async def refresh_banned_cache():
    global banned_users_cache
    if db_conn is None:
        return
    async with db_conn.execute("SELECT user_id FROM banned_users") as cursor:
        rows = await cursor.fetchall()
    banned_users_cache = {row[0] for row in rows}


async def init_db():
    global db_conn

    db_dir = os.path.dirname(db_path)
    os.makedirs(db_dir, exist_ok=True)

    if os.path.exists(db_path):
        os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)

    if db_conn is None:
        db_conn = await aiosqlite.connect(db_path)

    await db_conn.execute("PRAGMA journal_mode=WAL;")
    await db_conn.execute("PRAGMA foreign_keys=ON;")

    await db_conn.execute('''
    CREATE TABLE IF NOT EXISTS banned_users (
        user_id INTEGER PRIMARY KEY
    )''')

    # user_id / current_opponent НЕ шифруются: операционный ID Telegram,
    # первичный/внешний ключ (см. пояснение в шапке файла).
    # Остальные персональные поля хранятся как пара (зашифрованное
    # значение + blind index для поиска).
    await db_conn.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username BLOB,                    -- зашифровано (AES-GCM)
        age_category_enc BLOB,            -- зашифровано (AES-GCM)
        age_category_idx TEXT,            -- blind index (HMAC) для матчинга
        gender_enc BLOB,
        gender_idx TEXT,
        target_gender_enc BLOB,
        target_gender_idx TEXT,
        status_enc BLOB,
        status_idx TEXT,
        current_match_cats INTEGER DEFAULT 0,
        total_cats INTEGER DEFAULT 0,
        current_opponent INTEGER DEFAULT NULL,
        consent_policy_version TEXT,
        consent_timestamp DATETIME
    )''')

    await db_conn.execute('''
    CREATE TABLE IF NOT EXISTS cat_photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_unique_id_enc BLOB NOT NULL, -- зашифровано (AES-GCM)
        file_unique_id_idx TEXT NOT NULL, -- blind index для проверки дублей
        file_id BLOB,                     -- зашифровано (AES-GCM)
        user_id INTEGER NOT NULL,
        uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    await db_conn.execute('''
    CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER,
        receiver_id INTEGER,
        text BLOB,                        -- зашифровано (AES-GCM)
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    await db_conn.execute("CREATE INDEX IF NOT EXISTS idx_users_search ON users(status_idx, age_category_idx, gender_idx, target_gender_idx)")
    await db_conn.execute("CREATE INDEX IF NOT EXISTS idx_cat_photos_unique ON cat_photos(file_unique_id_idx)")
    await db_conn.execute("CREATE INDEX IF NOT EXISTS idx_cat_photos_uploaded_at ON cat_photos(uploaded_at)")
    await db_conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_history_pair ON chat_history(sender_id, receiver_id, timestamp)")
    await db_conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_history_timestamp ON chat_history(timestamp)")

    await db_conn.commit()
    await refresh_banned_cache()


async def cleanup_old_data():
    if db_conn is None:
        logging.warning("База данных ещё не подключена, пропускаем очистку.")
        return
    try:
        await db_conn.execute(
            "DELETE FROM cat_photos WHERE uploaded_at < datetime('now', '-180 days')"
        )
        await db_conn.execute(
            "DELETE FROM chat_history WHERE timestamp < datetime('now', '-3 days')"
        )
        await db_conn.commit()
        logging.info("Очистка старых фото (>180 дней) и переписки (>3 дней) завершена.")
    except Exception as e:
        logging.error(f"Ошибка очистки старых данных: {e}")


async def periodic_cleanup():
    while True:
        try:
            await asyncio.sleep(86400)
            if db_conn is not None:
                await cleanup_old_data()
        except asyncio.CancelledError:
            logging.info("Задача периодической очистки остановлена.")
            break
        except Exception as e:
            logging.error(f"Критическая ошибка в цикле очистки: {e}")
            await asyncio.sleep(3600)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
MODERATION_CHAT_ID = os.getenv("MODERATION_CHAT_ID", ADMIN_ID)


router = Router()


class Registration(StatesGroup):
    waiting_for_rules = State()
    waiting_for_age = State()
    waiting_for_gender = State()
    waiting_for_target_gender = State()

class ReportState(StatesGroup):
    waiting_for_text = State()


# --- КЛАВИАТУРЫ ---
def get_delete_confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 Да, удалить всё", callback_data="delete_confirm_yes"),
            InlineKeyboardButton(text="❌ Нет, отмена", callback_data="delete_confirm_no")
        ]
    ])

def get_age_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="14 - 17 лет"), KeyboardButton(text="18 лет и старше")]
    ], resize_keyboard=True, one_time_keyboard=True)

def get_rules_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я ознакомился, начать!", callback_data="start_registration")]
    ])

def get_gender_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Парень"), KeyboardButton(text="Девушка")]
    ], resize_keyboard=True, one_time_keyboard=True)

def get_target_gender_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Парня"), KeyboardButton(text="Девушку")],
        [KeyboardButton(text="Всё равно")]
    ], resize_keyboard=True, one_time_keyboard=True)

def get_main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔍 Найти игрока")],
        [KeyboardButton(text="⚙️ Изменить профиль"), KeyboardButton(text="🏆 Таблица лидеров")]
    ], resize_keyboard=True)

def get_search_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🛑 Остановить поиск")]
    ], resize_keyboard=True)

def get_game_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🏁 Завершить игру")],
        [KeyboardButton(text="🚨 Пожаловаться на собеседника")]
    ], resize_keyboard=True)

def get_confirm_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, выйти", callback_data="confirm_exit_yes"),
            InlineKeyboardButton(text="❌ Нет, играем дальше", callback_data="confirm_exit_no")
        ]
    ])

def get_moderation_kb(target_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔨 Забанить", callback_data=f"mod_ban:{target_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mod_dismiss:{target_id}")
        ]
    ])


async def check_subscription(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        if member.status in ['creator', 'administrator', 'member']:
            return True
        return False
    except Exception as e:
        logging.error(f"Ошибка проверки подписки: {e}")
        return True


# --- АДМИН ПАНЕЛЬ: БАН (Ручная команда) ---
@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    if str(message.from_user.id) != str(ADMIN_ID):
        return

    if not command.args:
        await message.answer("⚠️ Использование: /ban <ID_пользователя>\nНапример: /ban 123456789")
        return

    try:
        target_id = int(command.args)
    except ValueError:
        await message.answer("❌ ID должен быть числом.")
        return

    await perform_ban(target_id)
    await message.answer(f"✅ Пользователь с ID <code>{target_id}</code> навсегда заблокирован.", parse_mode="HTML")

async def perform_ban(target_id: int):
    async with db_conn.execute("SELECT current_opponent FROM users WHERE user_id = ?", (target_id,)) as cursor:
        res = await cursor.fetchone()

    if res and res[0]:
        opp_id = res[0]

        await db_conn.execute(
            "DELETE FROM chat_history "
            "WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)",
            (target_id, opp_id, opp_id, target_id)
        )

        await db_conn.execute(
            "UPDATE users SET status_enc = ?, status_idx = ?, current_opponent = NULL WHERE user_id = ?",
            (encrypt_data('idle'), blind_index('idle'), opp_id)
        )
        await db_conn.commit()

        try:
            await bot.send_message(
                opp_id,
                "🚨 Твой собеседник был заблокирован администратором за нарушение правил. Игра завершена.",
                reply_markup=get_main_menu()
            )
        except Exception as e:
            logging.warning(f"Не удалось уведомить оппонента {opp_id} о бане: {e}")

    await db_conn.execute("INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)", (target_id,))
    await db_conn.execute(
        "UPDATE users SET status_enc = ?, status_idx = ?, current_opponent = NULL WHERE user_id = ?",
        (encrypt_data('banned'), blind_index('banned'), target_id)
    )
    await db_conn.commit()
    banned_users_cache.add(target_id)

    try:
        await bot.send_message(
            target_id,
            "⛔️ <b>Вы были навсегда заблокированы администратором/модератором за нарушение правил.</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning(f"Не удалось отправить сообщение о бане пользователю {target_id}: {e}")


# Команда для удаления данных
@router.message(Command("delete_data"))
async def cmd_delete_data(message: Message):
    await message.answer(
        "⚠️ <b>ВНИМАНИЕ!</b>\n"
        "Ты собираешься удалить все данные о себе из базы данных бота «котоLOVе».\n\n"
        "Это действие <b>необратимо</b>: твой профиль, счетчик котов и вся статистика будут стерты навсегда. "
        "Ты уверен, что хочешь продолжить?",
        reply_markup=get_delete_confirm_kb(),
        parse_mode="HTML"
    )

@router.callback_query(F.data.startswith("delete_confirm_"))
async def process_delete_confirm(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    action = callback.data.split("_")[-1]

    if action == "no":
        await callback.message.edit_text("✅ Отмена. Твои данные остались в безопасности.")
        await callback.answer()
        return

    async with db_conn.execute("SELECT current_opponent FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()
    opp_id = res[0] if res else None

    try:
        if opp_id:
            await db_conn.execute(
                "UPDATE users SET status_enc = ?, status_idx = ?, current_opponent = NULL, current_match_cats = 0 WHERE user_id = ?",
                (encrypt_data('idle'), blind_index('idle'), opp_id)
            )

        await db_conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        await db_conn.execute("DELETE FROM cat_photos WHERE user_id = ?", (user_id,))
        await db_conn.execute("DELETE FROM chat_history WHERE sender_id = ? OR receiver_id = ?", (user_id, user_id))

        await db_conn.commit()

    except Exception as e:
        await db_conn.rollback()
        logging.error(f"[DELETE ERROR] Ошибка при удалении пользователя {user_id}: {e}")
        await callback.message.edit_text("❌ Произошла ошибка базы данных. Попробуй позже.")
        await callback.answer()
        return

    if opp_id:
        try:
            await callback.bot.send_message(
                opp_id,
                "⚠️ Твой собеседник удалил свой профиль. Игра завершена.",
                reply_markup=get_main_menu()
            )
        except Exception as e:
            logging.warning(f"Не удалось уведомить оппонента {opp_id} об удалении: {e}")

    await state.clear()
    await callback.message.edit_text("🗑 Все твои данные были успешно удалены из базы. Надеемся еще увидеть тебя!")
    await callback.answer("Данные удалены.")

# --- ОБРАБОТЧИКИ КНОПОК МОДЕРАЦИИ (INLINE) ---
@router.callback_query(F.data.startswith("mod_ban:"))
async def mod_ban_handler(callback: CallbackQuery):
    target_id = int(callback.data.split(":")[1])
    await perform_ban(target_id)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply(f"🔨 <b>Решение принято:</b> Пользователь <code>{target_id}</code> ЗАБАНЕН.", parse_mode="HTML")
    except Exception as e:
        logging.warning(f"Не удалось обновить сообщение модерации после бана {target_id}: {e}")
    await callback.answer("Пользователь заблокирован.")

@router.callback_query(F.data.startswith("mod_dismiss:"))
async def mod_dismiss_handler(callback: CallbackQuery):
    target_id = int(callback.data.split(":")[1])
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply(f"❌ <b>Решение принято:</b> Жалоба на пользователя <code>{target_id}</code> ОТКЛОНЕНА.", parse_mode="HTML")
    except Exception as e:
        logging.warning(f"Не удалось обновить сообщение модерации после отклонения жалобы на {target_id}: {e}")
    await callback.answer("Жалоба отклонена.")


async def start_registration_flow(message: Message, state: FSMContext, text_prefix=""):
    await message.answer(
        f"{text_prefix}Давай настроим твой профиль. Укажи свой возраст:",
        reply_markup=get_age_kb()
    )
    await state.set_state(Registration.waiting_for_age)

@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    if str(message.from_user.id) != str(ADMIN_ID):
        return

    if not command.args:
        await message.answer("⚠️ Использование: /unban <ID>")
        return

    try:
        target_id = int(command.args)
        await db_conn.execute("DELETE FROM banned_users WHERE user_id = ?", (target_id,))
        await db_conn.execute(
            "UPDATE users SET status_enc = ?, status_idx = ? WHERE user_id = ? AND status_idx = ?",
            (encrypt_data('idle'), blind_index('idle'), target_id, blind_index('banned'))
        )
        await db_conn.commit()
        banned_users_cache.discard(target_id)

        await message.answer(f"✅ Пользователь <code>{target_id}</code> успешно разбанен!", parse_mode="HTML")
    except ValueError:
        await message.answer("❌ ID должен быть числом.")

# --- СИСТЕМА ОБЫЧНЫХ РЕПОРТОВ (БАГИ, ПРЕДЛОЖЕНИЯ РАЗРАБУ) ---
@router.message(Command("report"))
async def cmd_report(message: Message, state: FSMContext):
    async with db_conn.execute("SELECT status_enc FROM users WHERE user_id = ?", (message.from_user.id,)) as cursor:
        res = await cursor.fetchone()
    if res and safe_decrypt(res[0], "cmd_report.status") == 'banned':
        return

    await message.answer("📝 Напиши своё предложение или баг-репорт (можно прикрепить скриншот) <b>одним сообщением</b>, и я передам его создателю бота!", parse_mode="HTML")
    await state.set_state(ReportState.waiting_for_text)

@router.message(ReportState.waiting_for_text)
async def process_report(message: Message, state: FSMContext):
    user_id = message.from_user.id

    async with db_conn.execute("SELECT current_opponent FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()
    opp_id_text = f"Последний собеседник ID: <code>{res[0]}</code>" if res and res[0] else "Собеседника нет"

    safe_username = html.escape(message.from_user.username or 'без_юзернейма')
    user_info = f"От: @{safe_username} (ID: <code>{user_id}</code>)\n{opp_id_text}"

    raw_text = message.text or message.caption
    report_text = html.escape(raw_text) if raw_text else "<i>Без текста</i>"

    if ADMIN_ID:
        try:
            if message.photo:
                await bot.send_photo(
                    ADMIN_ID,
                    message.photo[-1].file_id,
                    caption=f"🔔 <b>Новый отзыв/репорт (с фото)!</b>\n{user_info}\n\n<b>Текст:</b>\n{report_text}",
                    parse_mode="HTML"
                )
            else:
                await bot.send_message(
                    ADMIN_ID,
                    f"🔔 <b>Новый отзыв/репорт!</b>\n{user_info}\n\n<b>Текст:</b>\n{report_text}",
                    parse_mode="HTML"
                )
            await message.answer("✅ Спасибо! Твой отзыв успешно отправлен разработчику.")
        except Exception as e:
            logging.error(f"Ошибка отправки репорта админу: {e}")
            await message.answer("❌ Произошла ошибка при отправке. Попробуй позже.")
    else:
        await message.answer("❌ Администратор бота временно недоступен, но твой репорт принят в космос!")

    await state.clear()


# --- СТАРТ ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id

    async with db_conn.execute("SELECT user_id FROM banned_users WHERE user_id = ?", (user_id,)) as cursor:
        if await cursor.fetchone():
            await message.answer("❌ Вы навсегда заблокированы за нарушение правил сервиса.")
            return

    async with db_conn.execute("SELECT age_category_enc, status_enc FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()

    age_val = safe_decrypt(res[0], "cmd_start.age") if res else None
    status_val = safe_decrypt(res[1], "cmd_start.status") if res else None

    if res and status_val == 'banned':
        await message.answer("❌ Вы навсегда заблокированы за нарушение правил сервиса.")
        return

    if res and age_val is not None:
        await db_conn.execute(
            "UPDATE users SET status_enc = ?, status_idx = ?, current_opponent = NULL, current_match_cats = 0 WHERE user_id = ?",
            (encrypt_data('idle'), blind_index('idle'), user_id)
        )

        await db_conn.execute(
            "DELETE FROM chat_history WHERE sender_id = ? OR receiver_id = ?",
            (user_id, user_id)
        )
        await db_conn.commit()

    rules_text = (
        "🐾 <b>Добро пожаловать в «котоLOVе»!</b>\n"
        "<i>Твой уютный уголок для знакомств, общения и поиска друзей через любовь к хвостикам.</i> 🐈\n\n"
        "Чтобы наше комьюнити оставалось безопасным и ламповым, пожалуйста, соблюдай эти простые правила:\n\n"
        "📸 <b>1. Только свои котики</b>\n"
        "Используй исключительно реальные фото своих питомцев. Картинкам из интернета здесь не место!\n\n"
        "🔞 <b>2. Строго без 18+</b>\n"
        "Запрещен любой непристойный, эротический или порнографический контент. <i>Нарушение = вечный бан.</i>\n\n"
        "🛡 <b>3. Честность и безопасность</b>\n"
        "Никакого мошенничества, спама и попыток выманивания личных данных или денег. <i>Нарушение = вечный бан.</i>\n\n"
        "💌 <b>4. Взаимное уважение</b>\n"
        "Мы за позитив! Токсичность, травля и оскорбления недопустимы. Собеседник — тоже человек.\n\n"
        "🌟 <b>5. Больше разнообразия</b>\n"
        "Один ракурс — один котик. Не спамь одним и тем же снимком, покажи пушистого во всей красе!\n\n"
        "⚠️ Регистрация от 14 лет. Для пользователей 14–17 лет нужно согласие родителей.\n\n"
        "👇 <i>Нажимая кнопку ниже, ты подтверждаешь, что принимаешь <a href='https://docs.google.com/document/d/1b_JCf_2SJR3hUNie0USd2h8vSmk0Z-TpBfVVwVvR7Wk/edit?usp=sharing'>Пользовательское соглашение</a> и <a href='https://docs.google.com/document/d/1Z99gTfiVyo3CaDZXbqSJ1lsqw2Rp3VuvjjdojzZWTQw/edit?usp=sharing'>Политику конфиденциальности</a>, и готов(а) соблюдать правила сервиса!</i>"
    )
    await message.answer(
        rules_text,
        reply_markup=get_rules_inline_kb(),
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    await state.set_state(Registration.waiting_for_rules)


# --- КНОПКА: ИЗМЕНИТЬ ПРОФИЛЬ ---
@router.message(F.text == "⚙️ Изменить профиль")
async def change_profile(message: Message, state: FSMContext):
    user_id = message.from_user.id

    async with db_conn.execute("SELECT status_enc FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()

    if not res:
        await cmd_start(message, state)
        return

    status_val = safe_decrypt(res[0], "change_profile.status")

    if status_val == 'banned':
        await message.answer("❌ Вы навсегда заблокированы за нарушение правил сервиса.")
        return

    if status_val == 'playing':
        await message.answer("Вы не можете изменить профиль во время общения! Сначала завершите текущий диалог.")
        return

    if status_val == 'searching':
        await db_conn.execute(
            "UPDATE users SET status_enc = ?, status_idx = ? WHERE user_id = ?",
            (encrypt_data('idle'), blind_index('idle'), user_id)
        )
        await db_conn.commit()
    await start_registration_flow(message, state, "🔄 Сброс настроек анкеты.\n")


@router.callback_query(Registration.waiting_for_rules, F.data == "start_registration")
async def process_rules_callback(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    policy_version = "v1.2"
    now = datetime.now()

    async with db_conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()

    username_enc = encrypt_data(callback.from_user.username or f"User_{user_id}")

    if row is None:
        await db_conn.execute(
            """
            INSERT INTO users (
                user_id, username,
                age_category_enc, age_category_idx,
                gender_enc, gender_idx,
                target_gender_enc, target_gender_idx,
                status_enc, status_idx,
                consent_policy_version, consent_timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                username_enc,
                None, None,
                None, None,
                None, None,
                encrypt_data('idle'), blind_index('idle'),
                policy_version, now
            )
        )
    else:
        await db_conn.execute(
            """
            UPDATE users
            SET consent_policy_version = ?, consent_timestamp = ?
            WHERE user_id = ?
            """,
            (policy_version, now, user_id)
        )

    await db_conn.commit()

    try:
        await callback.message.delete()
    except Exception as e:
        logging.debug(f"Не удалось удалить сообщение с правилами: {e}")

    await callback.message.answer(
        "Отлично! Приступаем к настройке профиля.\nУкажи свой возраст:",
        reply_markup=get_age_kb()
    )
    await state.set_state(Registration.waiting_for_age)
    await callback.answer()

# --- ПРОЦЕСС РЕГИСТРАЦИИ ---
@router.message(Registration.waiting_for_age)
async def process_age(message: Message, state: FSMContext):
    valid_ages = ["14 - 17 лет", "18 лет и старше"]

    if message.text not in valid_ages:
        await message.answer("Пожалуйста, выбери вариант на клавиатуре!")
        return
    await state.update_data(age_category=message.text)
    await message.answer("Отлично! Теперь укажи свой пол:", reply_markup=get_gender_kb())
    await state.set_state(Registration.waiting_for_gender)


@router.message(Registration.waiting_for_gender)
async def process_gender(message: Message, state: FSMContext):
    if message.text not in ["Парень", "Девушка"]:
        await message.answer("Используй кнопки для выбора пола!")
        return
    gender_val = "male" if message.text == "Парень" else "female"
    await state.update_data(gender=gender_val)
    await message.answer("Кого ты хочешь найти для игры?", reply_markup=get_target_gender_kb())
    await state.set_state(Registration.waiting_for_target_gender)


@router.message(Registration.waiting_for_target_gender)
async def process_target_gender(message: Message, state: FSMContext):
    if message.text not in ["Парня", "Девушку", "Всё равно"]:
        await message.answer("Выбери вариант на клавиатуре!")
        return

    user_data = await state.get_data()
    user_id = message.from_user.id
    username = message.from_user.username or f"User_{user_id}"

    target_val = "any"
    if message.text == "Парня":
        target_val = "male"
    elif message.text == "Девушку":
        target_val = "female"

    async with db_conn.execute(
        "SELECT total_cats FROM users WHERE user_id = ?",
        (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
        total_cats_to_save = row[0] if row else 0

    await db_conn.execute(
        """
        UPDATE users
        SET username = ?,
            age_category_enc = ?, age_category_idx = ?,
            gender_enc = ?, gender_idx = ?,
            target_gender_enc = ?, target_gender_idx = ?,
            status_enc = ?, status_idx = ?,
            current_match_cats = 0,
            total_cats = ?
        WHERE user_id = ?
        """,
        (
            encrypt_data(username),
            encrypt_data(user_data['age_category']), blind_index(user_data['age_category']),
            encrypt_data(user_data['gender']), blind_index(user_data['gender']),
            encrypt_data(target_val), blind_index(target_val),
            encrypt_data('idle'), blind_index('idle'),
            total_cats_to_save,
            user_id
        )
    )
    await db_conn.commit()

    await state.clear()
    await message.answer(
        "🎉 Профиль успешно обновлён!\n"
        "Нажми «🔍 Найти игрока», чтобы начать соревнование по новым критериям.",
        reply_markup=get_main_menu()
    )


# --- ПОИСК ИГРОКА ---
@router.message(F.text == "🔍 Найти игрока")
async def find_player(message: Message, state: FSMContext):
    user_id = message.from_user.id

    if not await check_subscription(user_id):
        await message.answer(
            f"❌ Чтобы играть, нужно подписаться на наш канал: {CHANNEL_ID}\n\n"
            "Подпишись и нажми кнопку «🔍 Найти игрока» еще раз!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📢 Подписаться на канал", url=f"https://t.me/{CHANNEL_ID.replace('@', '')}")]
            ])
        )
        return

    async with db_conn.execute(
        "SELECT status_enc, gender_enc, target_gender_enc, age_category_enc FROM users WHERE user_id = ?",
        (user_id,)
    ) as cursor:
        u_data = await cursor.fetchone()

    if not u_data:
        await cmd_start(message, state)
        return

    current_status = safe_decrypt(u_data[0], "find_player.status")
    u_gender = safe_decrypt(u_data[1], "find_player.gender")
    u_target = safe_decrypt(u_data[2], "find_player.target_gender")
    u_age = safe_decrypt(u_data[3], "find_player.age_category")

    if current_status == 'banned':
        await message.answer("❌ Вы навсегда заблокированы за нарушение правил сервиса.")
        return

    if current_status == 'playing':
        await message.answer("Ты уже находишься в активной игре!", reply_markup=get_game_menu())
        return

    searching_idx = blind_index('searching')
    any_idx = blind_index('any')
    u_gender_idx = blind_index(u_gender)
    u_target_idx = blind_index(u_target)
    u_age_idx = blind_index(u_age)

    # Строгое условие AND age_category_idx = ? — не даёт свести в паре
    # несовершеннолетнего (14-17) и взрослого пользователя.
    query = """
        UPDATE users
        SET status_enc = ?, status_idx = ?, current_opponent = ?, current_match_cats = 0
        WHERE user_id = (
            SELECT user_id FROM users
            WHERE status_idx = ?
              AND user_id != ?
              AND (target_gender_idx = ? OR target_gender_idx = ?)
              AND (? = ? OR gender_idx = ?)
              AND age_category_idx = ?
            LIMIT 1
        )
        RETURNING user_id
    """

    async with db_conn.execute(
        query,
        (
            encrypt_data('playing'), blind_index('playing'), user_id,
            searching_idx, user_id,
            any_idx, u_gender_idx,
            u_target_idx, any_idx, u_target_idx,
            u_age_idx
        )
    ) as cursor:
        opponent = await cursor.fetchone()

    if opponent:
        opponent_id = opponent[0]

        await db_conn.execute(
            "UPDATE users SET status_enc = ?, status_idx = ?, current_opponent = ?, current_match_cats = 0 WHERE user_id = ?",
            (encrypt_data('playing'), blind_index('playing'), opponent_id, user_id)
        )
        await db_conn.commit()

        await message.answer("🎉 Собеседник найден! Начинаем общение. Присылай фото котиков!", reply_markup=get_game_menu())
        await bot.send_message(opponent_id, "🎉 Собеседник найден! Начинаем общение. Присылай фото котиков!", reply_markup=get_game_menu())
    else:
        await db_conn.execute(
            "UPDATE users SET status_enc = ?, status_idx = ?, current_opponent = NULL WHERE user_id = ?",
            (encrypt_data('searching'), blind_index('searching'), user_id)
        )
        await db_conn.commit()
        await message.answer("🔍 Ищем собеседника... Пожалуйста, подожди.", reply_markup=get_search_menu())


# --- ОСТАНОВКА ПОИСКА ---
@router.message(F.text == "🛑 Остановить поиск")
async def stop_search(message: Message):
    user_id = message.from_user.id

    async with db_conn.execute("SELECT status_enc FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()

    status_val = safe_decrypt(res[0], "stop_search.status") if res else None

    if not res or status_val != 'searching':
        await message.answer("Ты сейчас не в поиске.", reply_markup=get_main_menu())
        return

    await db_conn.execute(
        "UPDATE users SET status_enc = ?, status_idx = ?, current_opponent = NULL WHERE user_id = ?",
        (encrypt_data('idle'), blind_index('idle'), user_id)
    )
    await db_conn.commit()
    await message.answer("🛑 Поиск остановлен.", reply_markup=get_main_menu())


# --- ЗАПРОС НА ЗАВЕРШЕНИЕ ИГРЫ ---
@router.message(F.text == "🏁 Завершить игру")
async def ask_end_game(message: Message):
    user_id = message.from_user.id
    async with db_conn.execute("SELECT status_enc FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()

    status_val = safe_decrypt(res[0], "ask_end_game.status") if res else None

    if status_val == 'playing':
        await message.answer(
            "⚠️ Ты уверен, что хочешь завершить диалог?\n"
            "Твой счётчик котов в этой игре сбросится!",
            reply_markup=get_confirm_inline_kb()
        )
    elif status_val == 'banned':
        return
    else:
        await message.answer("Ты сейчас не в игре.", reply_markup=get_main_menu())


# --- ЖАЛОБА НА СОБЕСЕДНИКА ВО ВРЕМЯ ИГРЫ (ТОКСИЧНОСТЬ) ---
@router.message(F.text == "🚨 Пожаловаться на собеседника")
async def report_player_chat(message: Message):
    user_id = message.from_user.id
    async with db_conn.execute("SELECT status_enc, current_opponent FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()

    status_val = safe_decrypt(res[0], "report_player_chat.status") if res else None

    if not res or status_val != 'playing':
        return await message.answer("Эта функция доступна только во время игры.")

    opponent_id = res[1]
    time_limit = (datetime.now() - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")

    async with db_conn.execute("""
        SELECT sender_id, text, timestamp FROM chat_history
        WHERE ((sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?))
          AND timestamp >= ?
        ORDER BY timestamp ASC
    """, (user_id, opponent_id, opponent_id, user_id, time_limit)) as cursor:
        history = await cursor.fetchall()

    lines = []
    for sender, txt, ts in history:
        name = "Нарушитель" if sender == opponent_id else "Жалующийся"
        time_str = ts.split(" ")[1] if " " in ts else ts

        decrypted_txt = safe_decrypt(txt, "report_player_chat.text")

        safe_txt = html.escape(decrypted_txt) if decrypted_txt else "[Медиа/Пусто]"
        lines.append(f"[{time_str}] {name}: {safe_txt}")

    if not lines:
        history_text = "<i>Сообщений за последние 20 минут в текстовом виде нет.</i>"
    else:
        history_text = "\n".join(lines)
        if len(history_text) > 3000:
            trimmed, total = [], 0
            for line in reversed(lines):
                total += len(line) + 1
                if total > 3000:
                    break
                trimmed.insert(0, line)
            history_text = "...\n" + "\n".join(trimmed)

    report_msg = (
        f"🚨 <b>ЖАЛОБА НА ОБЩЕНИЕ (Токсичность / Спам)</b>\n\n"
        f"Нарушитель ID: <code>{opponent_id}</code>\n"
        f"Жалуется ID: <code>{user_id}</code>\n\n"
        f"<b>История чата за последние 20 минут:</b>\n"
        f"{history_text}"
    )

    try:
        await bot.send_message(
            MODERATION_CHAT_ID,
            report_msg,
            reply_markup=get_moderation_kb(opponent_id),
            parse_mode="HTML"
        )
        await message.answer("🚨 Твоя жалоба и история чата успешно отправлены модераторам. Спасибо!")
    except Exception as e:
        logging.error(f"Ошибка при отправке логов чата модераторам: {e}")
        await message.answer("❌ Произошла ошибка при отправке репорта. Свяжитесь с администрацией.")

# --- ОБРАБОТКА ПОДТВЕРЖДЕНИЯ ВЫХОДА ---
@router.callback_query(F.data.startswith("confirm_exit_"))
async def process_confirm_exit(callback: CallbackQuery):
    user_id = callback.from_user.id
    action = callback.data.split("_")[-1]

    if action == "no":
        await callback.message.edit_text("✅ Игра продолжается!")
        await callback.answer()
        return

    async with db_conn.execute("SELECT current_opponent FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()
    if not res or not res[0]:
        await callback.answer("Ты не в игре.", show_alert=True)
        return

    opponent_id = res[0]

    await db_conn.execute(
        "DELETE FROM chat_history "
        "WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)",
        (user_id, opponent_id, opponent_id, user_id)
    )

    await db_conn.execute(
        "UPDATE users SET status_enc = ?, status_idx = ?, current_opponent = NULL, current_match_cats = 0 WHERE user_id IN (?, ?)",
        (encrypt_data('idle'), blind_index('idle'), user_id, opponent_id)
    )
    await db_conn.commit()

    await callback.message.edit_text("🏁 Игра завершена.")
    await callback.answer()

    try:
        await bot.send_message(
            opponent_id,
            "⚠️ Твой собеседник завершил игру.",
            reply_markup=get_main_menu()
        )
    except Exception as e:
        logging.warning(f"Не удалось сообщить оппоненту {opponent_id}: {e}")

# --- ТАБЛИЦА ЛИДЕРОВ ---
@router.message(F.text == "🏆 Таблица лидеров")
async def show_leaderboard(message: Message):
    async with db_conn.execute("SELECT status_enc FROM users WHERE user_id = ?", (message.from_user.id,)) as cursor:
        res = await cursor.fetchone()
    if res and safe_decrypt(res[0], "show_leaderboard.status") == 'banned':
        return

    async with db_conn.execute("SELECT username, total_cats FROM users ORDER BY total_cats DESC LIMIT 10") as cursor:
        leaders = await cursor.fetchall()

    if not leaders:
        await message.answer("🏆 Пока в таблице лидеров пусто.")
        return

    text = "<b>🏆 ТОП-10 Котоловов:</b>\n\n"
    for i, (username_blob, count) in enumerate(leaders, 1):
        username = safe_decrypt(username_blob, "show_leaderboard.username")

        display_name = f"@{html.escape(username)}" if username and not username.startswith("User_") else "Игрок"

        if i == 1: emoji = "🥇"
        elif i == 2: emoji = "🥈"
        elif i == 3: emoji = "🥉"
        else: emoji = "🔹"

        text += f"{emoji} {display_name} — 🐈 <b>{count}</b>\n"

    await message.answer(text, parse_mode="HTML")


# --- ОБРАБОТКА ФОТО ВЕРИФИКАЦИИ (И ЖАЛОБ НА ФОТО) ---
@router.callback_query(F.data.startswith("check_cat:"))
async def process_cat_check(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) < 4:
        await callback.answer("Ошибка данных кнопки", show_alert=True)
        return

    action = parts[1]
    sender_id = int(parts[2])
    photo_record_id = int(parts[3])

    async with db_conn.execute(
        "SELECT file_id, file_unique_id_idx, user_id FROM cat_photos WHERE id = ?",
        (photo_record_id,)
    ) as cursor:
        row = await cursor.fetchone()

    if not row:
        await callback.answer("Фото не найдено в базе", show_alert=True)
        return

    file_id_blob, file_unique_id_idx, stored_user_id = row
    file_id = safe_decrypt(file_id_blob, "process_cat_check.file_id")

    if stored_user_id != sender_id:
        await callback.answer("Это не твоё фото!", show_alert=True)
        return

    if action == "report":
        if MODERATION_CHAT_ID and file_id:
            try:
                await bot.send_photo(
                    MODERATION_CHAT_ID,
                    file_id,
                    caption=(f"🚨 <b>ЖАЛОБА НА ФОТО</b>\n"
                             f"Нарушитель: <code>{stored_user_id}</code>\n"
                             f"Жалуется: <code>{callback.from_user.id}</code>"),
                    reply_markup=get_moderation_kb(stored_user_id),
                    parse_mode="HTML"
                )
                await callback.message.answer("🚨 Жалоба успешно отправлена администратору.")
            except Exception as e:
                logging.error(f"Ошибка отправки жалобы: {e}")
                await callback.answer("Не удалось отправить жалобу.", show_alert=True)
        else:
            await callback.answer("Нет чата модерации.", show_alert=True)

    elif action == "yes":
        async with db_conn.execute(
            "SELECT id FROM cat_photos WHERE file_unique_id_idx = ? AND user_id != ?",
            (file_unique_id_idx, sender_id)
        ) as cursor:
            if await cursor.fetchone():
                await callback.answer("❌ Это фото уже использовалось другим игроком!", show_alert=True)
                return

        await db_conn.execute(
            """
            UPDATE users
            SET current_match_cats = current_match_cats + 1,
                total_cats = total_cats + 1
            WHERE user_id = ?
            """,
            (sender_id,)
        )
        await db_conn.commit()

        async with db_conn.execute("SELECT current_match_cats FROM users WHERE user_id = ?", (sender_id,)) as cursor:
            row = await cursor.fetchone()
            sender_score = row[0] if row else 0

        async with db_conn.execute("SELECT current_match_cats FROM users WHERE user_id = ?", (callback.from_user.id,)) as cursor:
            row = await cursor.fetchone()
            my_score = row[0] if row else 0

        await bot.send_message(sender_id, f"🎉 Собеседник подтвердил котика! Твой счёт: {sender_score}")
        await callback.message.answer(f"✅ Засчитано! У собеседника теперь {sender_score} 🐈\nТвой счёт: {my_score}")

    elif action == "no":
        await bot.send_message(sender_id, "📸 Собеседник отметил твоё фото как обычный снимок.")
        await callback.message.answer("Принято!")

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as e:
        logging.debug(f"Не удалось убрать клавиатуру подтверждения фото: {e}")


# --- ОБРАБОТКА ЧАТА И КАРТИНОК ---
@router.message()
async def handle_chat_and_media(message: Message):
    user_id = message.from_user.id

    async with db_conn.execute("SELECT status_enc, current_opponent FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()

    status_val = safe_decrypt(res[0], "handle_chat_and_media.status") if res else None

    if not res or status_val == 'banned':
        return

    if status_val != 'playing':
        if message.photo:
            await message.answer("❌ Ты не можешь отправлять котиков просто так! Сначала нажми «🔍 Найти игрока».")
        elif message.text not in ["🔍 Найти игрока", "🏆 Таблица лидеров", "⚙️ Изменить профиль"]:
            await message.answer("Воспользуйся кнопками меню!", reply_markup=get_main_menu())
        return

    opponent_id = res[1]
    if not opponent_id:
        await db_conn.execute(
            "UPDATE users SET status_enc = ?, status_idx = ? WHERE user_id = ?",
            (encrypt_data('idle'), blind_index('idle'), user_id)
        )
        await db_conn.commit()
        await message.answer("⚠️ Ошибка: собеседник потерян. Пожалуйста, начни поиск заново.", reply_markup=get_main_menu())
        return

    if message.photo:
        photo = message.photo[-1]
        file_id = photo.file_id
        file_unique_id = photo.file_unique_id

        cursor = await db_conn.execute(
            """
            INSERT INTO cat_photos (file_unique_id_enc, file_unique_id_idx, file_id, user_id, uploaded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (encrypt_data(file_unique_id), blind_index(file_unique_id), encrypt_data(file_id), user_id, datetime.now())
        )
        photo_record_id = cursor.lastrowid
        await db_conn.commit()

        verify_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🐱 Да, это кот!", callback_data=f"check_cat:yes:{user_id}:{photo_record_id}"),
                InlineKeyboardButton(text="📸 Просто фото", callback_data=f"check_cat:no:{user_id}:{photo_record_id}")
            ],
            [
                InlineKeyboardButton(text="🚨 Пожаловаться (НСФВ/Спам)", callback_data=f"check_cat:report:{user_id}:{photo_record_id}")
            ]
        ])

        await message.answer("⏳ Отправил фото собеседнику на подтверждение...")

        try:
            await bot.send_photo(
                opponent_id,
                photo.file_id,
                caption="<b>[Фото от собеседника]</b>\nЭто котик? Подтверди, чтобы ему засчитался балл! 👇",
                reply_markup=verify_kb,
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Не удалось отправить фото оппоненту {opponent_id}: {e}")
            await db_conn.execute("DELETE FROM cat_photos WHERE id = ?", (photo_record_id,))
            await db_conn.commit()
            await message.answer("❌ Не удалось доставить фото собеседнику. Попробуй ещё раз позже.")
        return

    if message.text:
        await db_conn.execute("INSERT INTO chat_history (sender_id, receiver_id, text) VALUES (?, ?, ?)",
                             (user_id, opponent_id, encrypt_data(message.text)))
        await db_conn.commit()

        try:
            await bot.send_message(opponent_id, message.text)
        except Exception:
            await message.answer("Не удалось доставить сообщение собеседнику.")
    else:
        await message.answer("В этом режиме поддерживаются только текстовые сообщения и фото котиков.")


async def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    global db_conn, bot, dp

    if os.getenv("DEBUG") == "1":
        _crypto_selftest()

    await init_db()
    await cleanup_old_data()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    dp.update.middleware(BanCheckMiddleware())
    dp.include_router(router)

    cleanup_task = asyncio.create_task(periodic_cleanup())

    logging.info("Бот запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        if db_conn is not None:
            await db_conn.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот выключен")
