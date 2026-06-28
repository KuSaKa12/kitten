import aiosqlite
import socket
import sys
import asyncio
import os
import logging
import sqlite3
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, \
    InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

CHANNEL_ID = "@ITkaktusik"
db_path = "/app/data/cat_game.db"
os.makedirs(os.path.dirname(db_path), exist_ok=True)

# Глобальная переменная для базы
# Подключение к БД
db_path = "/app/data/cat_game.db"
os.makedirs(os.path.dirname(db_path), exist_ok=True)

# Глобальная переменная для базы
db_conn = None

async def init_db():
    global db_conn
    # Открываем базу асинхронно
    db_conn = await aiosqlite.connect(db_path)
    await db_conn.execute('''
    CREATE TABLE IF NOT EXISTS banned_users (
        user_id INTEGER PRIMARY KEY
    )''')
    # Создаем таблицы (все сразу здесь)
    await db_conn.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        age_category TEXT,
        gender TEXT,
        target_gender TEXT,
        status TEXT DEFAULT 'idle', 
        current_match_cats INTEGER DEFAULT 0, 
        total_cats INTEGER DEFAULT 0,         
        current_opponent INTEGER DEFAULT NULL
    )''')
    
    await db_conn.execute('''
    CREATE TABLE IF NOT EXISTS cat_photos (
        file_unique_id TEXT PRIMARY KEY,
        user_id INTEGER
    )''')
    
    await db_conn.execute('''
    CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER,
        receiver_id INTEGER,
        text TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    
    await db_conn.commit()

# Настройки для Windows
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    old_getaddrinfo = socket.getaddrinfo

    def new_getaddrinfo(*args, **kwargs):
        responses = old_getaddrinfo(*args, **kwargs)
        return [r for r in responses if r[0] == socket.AF_INET]

    socket.getaddrinfo = new_getaddrinfo

logging.basicConfig(level=logging.INFO)
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID") # Для системы репортов (баги/предложения)
# ID чата модерации (группы), куда будут приходить жалобы на игроков. 
# Если не указан, упадет в личку ADMIN_ID
MODERATION_CHAT_ID = os.getenv("MODERATION_CHAT_ID", ADMIN_ID) 

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
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
        [KeyboardButton(text="Меньше 12 лет"), KeyboardButton(text="12 - 14 лет")],
        [KeyboardButton(text="15 - 17 лет"), KeyboardButton(text="18 лет и старше")]
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

# Клавиатура для модерации жалоб
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
    # 1. Сначала узнаем ID оппонента
    async with db_conn.execute("SELECT current_opponent FROM users WHERE user_id = ?", (target_id,)) as cursor:
        res = await cursor.fetchone()
    
    if res and res[0]:
        opp_id = res[0]
        # Используем await db_conn.execute для изменения данных
        await db_conn.execute("UPDATE users SET status = 'idle', current_opponent = NULL WHERE user_id = ?", (opp_id,))
        # ... (код отправки сообщения оппоненту)
        try:
            await bot.send_message(
                opp_id, 
                "🚨 Твой собеседник был заблокирован администратором за нарушение правил. Игра завершена.", 
                reply_markup=get_main_menu()
            )
        except:
            pass

    # Выдаем бан
    await db_conn.execute("UPDATE users SET status = 'banned', current_opponent = NULL WHERE user_id = ?", (target_id,))
    await db_conn.commit() # Обязательно await!

    # Уведомляем самого нарушителя
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

# Обработка нажатия на кнопки удаления
@router.callback_query(F.data.startswith("delete_confirm_"))
async def process_delete_confirm(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    action = callback.data.split("_")[-1]

    if action == "no":
        await callback.message.edit_text("✅ Отмена. Твои данные остались в безопасности.")
        await callback.answer()
        return

    # 1. Получаем ID оппонента ДО удаления
    async with db_conn.execute("SELECT current_opponent FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()
    opp_id = res[0] if res else None

    # 2. Выполняем очистку базы
    try:
        # Сначала сбрасываем состояние оппонента, если он есть
        if opp_id:
            await db_conn.execute(
                "UPDATE users SET status = 'idle', current_opponent = NULL, current_match_cats = 0 WHERE user_id = ?", 
                (opp_id,)
            )

        # Удаляем пользователя и его данные
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

    # 3. Уведомляем оппонента (внешнее взаимодействие)
    if opp_id:
        try:
            await callback.bot.send_message(
                opp_id, 
                "⚠️ Твой собеседник удалил свой профиль. Игра завершена.", 
                reply_markup=get_main_menu()
            )
        except Exception as e:
            logging.warning(f"Не удалось уведомить оппонента {opp_id} об удалении: {e}")

    # 4. Завершаем работу
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
    except:
        pass
    await callback.answer("Пользователь заблокирован.")

@router.callback_query(F.data.startswith("mod_dismiss:"))
async def mod_dismiss_handler(callback: CallbackQuery):
    target_id = int(callback.data.split(":")[1])
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply(f"❌ <b>Решение принято:</b> Жалоба на пользователя <code>{target_id}</code> ОТКЛОНЕНА.", parse_mode="HTML")
    except:
        pass
    await callback.answer("Жалоба отклонена.")


# --- ФУНКЦИЯ СТАРТА РЕГИСТРАЦИИ ---
async def start_registration_flow(message: Message, state: FSMContext, text_prefix=""):
    await message.answer(
        f"{text_prefix}Давай настроим твой профиль. Укажи свой возраст:",
        reply_markup=get_age_kb()
    )
    await state.set_state(Registration.waiting_for_age)




# --- СИСТЕМА ОБЫЧНЫХ РЕПОРТОВ (БАГИ, ПРЕДЛОЖЕНИЯ РАЗРАБУ) ---
@router.message(Command("report"))
async def cmd_report(message: Message, state: FSMContext):
    async with db_conn.execute("SELECT status FROM users WHERE user_id = ?", (message.from_user.id,)) as cursor:
        res = cursor.fetchone()
    if res and res[0] == 'banned':
        return

    await message.answer("📝 Напиши своё предложение или баг-репорт (можно прикрепить скриншот) <b>одним сообщением</b>, и я передам его создателю бота!", parse_mode="HTML")
    await state.set_state(ReportState.waiting_for_text)

@router.message(ReportState.waiting_for_text)
async def process_report(message: Message, state: FSMContext):
    user_id = message.from_user.id
    
    async with db_conn.execute("SELECT current_opponent FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()
    opp_id_text = f"Последний собеседник ID: <code>{res[0]}</code>" if res and res[0] else "Собеседника нет"

    user_info = f"От: @{message.from_user.username or 'без_юзернейма'} (ID: <code>{user_id}</code>)\n{opp_id_text}"
    report_text = message.text or message.caption or "<i>Без текста</i>"
    
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

    # 1. ПРОВЕРКА НА БАН (по новой таблице)
    async with db_conn.execute("SELECT user_id FROM banned_users WHERE user_id = ?", (user_id,)) as cursor:
        if await cursor.fetchone():
            await message.answer("❌ Вы навсегда заблокированы за нарушение правил сервиса.")
            return

    # 2. Обычная проверка статуса
    async with db_conn.execute("SELECT age_category, status FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()

    # (Опционально: можно оставить проверку статуса, если бан еще есть в таблице users)
    if res and res[1] == 'banned':
        await message.answer("❌ Вы навсегда заблокированы за нарушение правил сервиса.")
        return

    # 3. Сброс игры, если пользователь уже зарегистрирован
    if res and res[0] is not None:
        await db_conn.execute(
            "UPDATE users SET status = 'idle', current_opponent = NULL, current_match_cats = 0 WHERE user_id = ?",
            (user_id,)
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
        "👇 <i>Нажимая кнопку ниже, ты подтверждаешь, что принимаешь <a href='ССЫЛКА_НА_СОГЛАШЕНИЕ'>Пользовательское соглашение</a> и <a href='ССЫЛКА_НА_ПОЛИТИКУ'>Политику конфиденциальности</a>, и готов(а) соблюдать правила сервиса!</i>"
    )
    await message.answer(
        rules_text, 
        reply_markup=get_rules_inline_kb(), 
        parse_mode="HTML"
    )
    await state.set_state(Registration.waiting_for_rules)


# --- КНОПКА: ИЗМЕНИТЬ ПРОФИЛЬ ---
@router.message(F.text == "⚙️ Изменить профиль")
async def change_profile(message: Message, state: FSMContext):
    user_id = message.from_user.id
    
    # Теперь правильно: res находится ВНУТРИ блока
    async with db_conn.execute("SELECT status FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()

    if not res:
        await cmd_start(message, state)
        return

    if res[0] == 'banned':
        await message.answer("❌ Вы навсегда заблокированы за нарушение правил сервиса.")
        return

    if res[0] == 'playing':
        await message.answer("Вы не можете изменить профиль во время общения! Сначала завершите текущий диалог.")
        return

    if res[0] == 'searching':
        # ИСПРАВЛЕНИЕ: используем асинхронный вызов и await
        await db_conn.execute("UPDATE users SET status = 'idle' WHERE user_id = ?", (user_id,))
        await db_conn.commit() # Обязательно с await!
    await start_registration_flow(message, state, "🔄 Сброс настроек анкеты.\n")


@router.callback_query(Registration.waiting_for_rules, F.data == "start_registration")
async def process_rules_callback(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.message.delete()
    except:
        pass
    await callback.message.answer(
        "Отлично! Приступаем к настройке профиля.\nУкажи свой возраст:", 
        reply_markup=get_age_kb()
    )
    await state.set_state(Registration.waiting_for_age)
    await callback.answer()


# --- ПРОЦЕСС РЕГИСТРАЦИИ ---
@router.message(Registration.waiting_for_age)
async def process_age(message: Message, state: FSMContext):
    valid_ages = ["Меньше 12 лет", "12 - 14 лет", "15 - 17 лет", "18 лет и старше"]
    
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

    target_val = "any"
    if message.text == "Парня":
        target_val = "male"
    elif message.text == "Девушку":
        target_val = "female"

    user_data = await state.get_data()
    user_id = message.from_user.id
    username = message.from_user.username or f"User_{user_id}"

    async with db_conn.execute("SELECT total_cats FROM users WHERE user_id = ?", (user_id,)) as cursor:
        old_total = await cursor.fetchone()
    total_cats_to_save = old_total[0] if old_total else 0

    await db_conn.execute('''
        INSERT OR REPLACE INTO users (user_id, username, age_category, gender, target_gender, status, current_match_cats, total_cats)
        VALUES (?, ?, ?, ?, ?, 'idle', 0, ?)
    ''', (user_id, username, user_data['age_category'], user_data['gender'], target_val, total_cats_to_save))
    await db_conn.commit()

    await state.clear()
    await message.answer(
        "🎉 Профиль успешно обновлен!\n"
        "Нажми «🔍 Найти игрока», чтобы начать соревнование по новым критериям.",
        reply_markup=get_main_menu()
    )


# --- ПОИСК ИГРОКА ---
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

    # ЧТЕНИЕ (SELECT) - используем async with
    async with db_conn.execute("SELECT status, gender, target_gender FROM users WHERE user_id = ?", (user_id,)) as cursor:
        u_data = await cursor.fetchone()
    
    if not u_data:
        await cmd_start(message, state)
        return

    current_status, u_gender, u_target = u_data

    if current_status == 'banned':
        await message.answer("❌ Вы навсегда заблокированы за нарушение правил сервиса.")
        return

    if current_status == 'playing':
        await message.answer("Ты уже находишься в активной игре!", reply_markup=get_game_menu())
        return

    query = """
        UPDATE users 
        SET status = 'playing', current_opponent = ?, current_match_cats = 0
        WHERE user_id = (
            SELECT user_id FROM users 
            WHERE status = 'searching' 
              AND user_id != ? 
              AND (target_gender = 'any' OR target_gender = ?)
              AND (? = 'any' OR gender = ?)
            LIMIT 1
        )
        RETURNING user_id
    """
    
    # ЧТЕНИЕ (UPDATE ... RETURNING работает как SELECT, он возвращает данные)
    async with db_conn.execute(query, (user_id, user_id, u_gender, u_target, u_target)) as cursor:
        opponent = await cursor.fetchone()

    if opponent:
        opponent_id = opponent[0]
        
        # ЗАПИСЬ (UPDATE) - просто выполняем запрос и делаем commit
        await db_conn.execute(
            "UPDATE users SET status = 'playing', current_opponent = ?, current_match_cats = 0 WHERE user_id = ?", 
            (opponent_id, user_id)
        )
        await db_conn.commit()

        await message.answer("🎉 Собеседник найден! Начинаем общение. Присылай фото котиков!", reply_markup=get_game_menu())
        await bot.send_message(opponent_id, "🎉 Собеседник найден! Начинаем общение. Присылай фото котиков!", reply_markup=get_game_menu())
    else:
        # ЗАПИСЬ (UPDATE) - встаем в поиск
        await db_conn.execute("UPDATE users SET status = 'searching', current_opponent = NULL WHERE user_id = ?", (user_id,))
        await db_conn.commit()
        await message.answer("🔍 Ищем собеседника... Пожалуйста, подожди.", reply_markup=get_search_menu())


# --- ЗАПРОС НА ЗАВЕРШЕНИЕ ИГРЫ ---
@router.message(F.text == "🏁 Завершить игру")
async def ask_end_game(message: Message):
    user_id = message.from_user.id
    async with db_conn.execute("SELECT status FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()

    if res and res[0] == 'playing':
        await message.answer(
            "⚠️ Ты уверен, что хочешь завершить диалог?\n"
            "Твой счётчик котов в этой игре сбросится!",
            reply_markup=get_confirm_inline_kb()
        )
    elif res and res[0] == 'banned':
        return
    else:
        await message.answer("Ты сейчас не в игре.", reply_markup=get_main_menu())


# --- ЖАЛОБА НА СОБЕСЕДНИКА ВО ВРЕМЯ ИГРЫ (ТОКСИЧНОСТЬ) ---
@router.message(F.text == "🚨 Пожаловаться на собеседника")
async def report_player_chat(message: Message):
    user_id = message.from_user.id
    async with db_conn.execute("SELECT status, current_opponent FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()

    if not res or res[0] != 'playing':
        return await message.answer("Эта функция доступна только во время игры.")

    opponent_id = res[1]
    time_limit = (datetime.now() - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    
    # ИСПОЛЬЗУЕМ async with cursor КАК В ДРУГИХ МЕСТАХ
    async with db_conn.execute("""
        SELECT sender_id, text, timestamp FROM chat_history
        WHERE ((sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?))
          AND timestamp >= ?
        ORDER BY timestamp ASC
    """, (user_id, opponent_id, opponent_id, user_id, time_limit)) as cursor:
        history = await cursor.fetchall()
    
    history_text = ""
    for sender, txt, ts in history:
        name = "Нарушитель" if sender == opponent_id else "Жалующийся"
        time_str = ts.split(" ")[1] if " " in ts else ts
        history_text += f"[{time_str}] {name}: {txt}\n"

    if not history_text:
        history_text = "<i>Сообщений за последние 20 минут в текстовом виде нет.</i>"
    elif len(history_text) > 3000:
        history_text = history_text[-3000:] 

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
        await message.answer("❌ Произошла ошибка при отправке репорта.")


# --- ОБРАБОТКА ПОДТВЕРЖДЕНИЯ ВЫХОДА ---
@router.callback_query(F.data.startswith("confirm_exit_"))
async def process_confirm_exit(callback: CallbackQuery):
    user_id = callback.from_user.id
    action = callback.data.split("_")[-1]

    await callback.answer()

    try:
        await callback.message.delete()
    except Exception:
        pass

    async with db_conn.execute("SELECT status, current_opponent FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()

    if not res or res[0] != 'playing':
        await bot.send_message(user_id, "Вы уже не находитесь в игре.", reply_markup=get_main_menu())
        return

    if action == "no":
        await bot.send_message(user_id, "Отлично, продолжаем игру! Жду фото котиков.")
        return

    if action == "yes":
        opponent_id = res[1]
        
        async with db_conn.execute("SELECT current_match_cats FROM users WHERE user_id = ?", (user_id,)) as cursor:
            my_score = await cursor.fetchone()[0]

        async with db_conn.execute("SELECT current_match_cats FROM users WHERE user_id = ?", (opponent_id,)) as cursor:
            opp_score_data = await cursor.fetchone()
        opp_score = opp_score_data[0] if opp_score_data else 0

        await db_conn.execute(
            "UPDATE users SET status = 'idle', current_opponent = NULL, current_match_cats = 0 WHERE user_id IN (?, ?)",
            (user_id, opponent_id)
        )
        await db_conn.commit()

        text_for_me = f"Игра завершена!\n📊 Твой счет в этом раунде: 🐈 {my_score}\n📊 Счет собеседника: 🐈 {opp_score}"
        text_for_opp = f"⚠️ Собеседник завершил игру.\n\nИгра завершена!\n📊 Твой счет в этом раунде: 🐈 {opp_score}\n📊 Счет собеседника: 🐈 {my_score}"

        await bot.send_message(user_id, text_for_me + "\n\nВозвращаемся в главное меню.", reply_markup=get_main_menu())
        await bot.send_message(opponent_id, text_for_opp + "\n\nВозвращаемся в главное меню.", reply_markup=get_main_menu())


# --- ТАБЛИЦА ЛИДЕРОВ ---
@router.message(F.text == "🏆 Таблица лидеров")
async def show_leaderboard(message: Message):
    async with db_conn.execute("SELECT status FROM users WHERE user_id = ?", (message.from_user.id,)) as cursor:
        res = await cursor.fetchone()
    if res and res[0] == 'banned':
        return

    async with db_conn.execute("SELECT username, total_cats FROM users ORDER BY total_cats DESC LIMIT 10") as cursor:
        leaders = await cursor.fetchall()
    
    if not leaders:
        await message.answer("🏆 Пока в таблице лидеров пусто.")
        return

    text = "<b>🏆 ТОП-10 Котоловов:</b>\n\n"
    for i, (username, count) in enumerate(leaders, 1):
        display_name = f"@{username}" if username and not username.startswith("User_") else "Игрок"
        
        if i == 1: emoji = "🥇"
        elif i == 2: emoji = "🥈"
        elif i == 3: emoji = "🥉"
        else: emoji = "🔹"
        
        text += f"{emoji} {display_name} — 🐈 <b>{count}</b>\n"
    
    await message.answer(text, parse_mode="HTML")


# --- ОБРАБОТКА ФОТО ВЕРИФИКАЦИИ (И ЖАЛОБ НА ФОТО) ---
@router.callback_query(F.data.startswith("check_cat:"))
async def verify_cat_photo(callback: CallbackQuery):
    data_parts = callback.data.split(":")
    action = data_parts[1]
    sender_id = int(data_parts[2])
    file_unique_id = data_parts[3]
    
    # Добавляем получение file_id, если он есть в данных
    file_id = data_parts[4] if len(data_parts) > 4 else None
    
    # ... (дальше идет твой код) ...

    elif action == "report":
        # ...
        if MODERATION_CHAT_ID and file_id: # Используем наш новый file_id
            try:
                await bot.send_photo(
                    MODERATION_CHAT_ID,
                    file_id, # ВОТ СЮДА ВСТАВЛЯЕМ ЕГО
                    caption=f"🚨 <b>ЖАЛОБА НА ФОТО...</b>",
                    # ...
                )

    await callback.answer()
    
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass

    # Используем асинхронный контекстный менеджер для всех операций
    async with db_conn.execute("SELECT status, current_opponent FROM users WHERE user_id = ?", (sender_id,)) as cursor:
        sender_data = await cursor.fetchone()
    
    if not sender_data or sender_data[0] != 'playing':
        if action != "report":
            await callback.message.answer("⚠️ Эта игра уже завершена.")
            return

    if action == "yes":
        # Проверка на повторное использование
        async with db_conn.execute("SELECT user_id FROM cat_photos WHERE file_unique_id = ?", (file_unique_id,)) as cursor:
            if await cursor.fetchone():
                await bot.send_message(sender_id, "❌ Это фото кота уже использовалось в игре! Балл не засчитан.")
                await callback.message.answer("Это фото уже было засчитано ранее.")
                return

        # Запись фото и начисление баллов (в одной транзакции)
        await db_conn.execute("INSERT INTO cat_photos (file_unique_id, user_id) VALUES (?, ?)", (file_unique_id, sender_id))
        await db_conn.execute(
            "UPDATE users SET current_match_cats = current_match_cats + 1, total_cats = total_cats + 1 WHERE user_id = ?",
            (sender_id,)
        )
        await db_conn.commit()

        # Получение обновленных баллов
        async with db_conn.execute("SELECT current_match_cats FROM users WHERE user_id = ?", (sender_id,)) as cursor:
            s_score = await cursor.fetchone()
            sender_score = s_score[0] if s_score else 0

        async with db_conn.execute("SELECT current_match_cats FROM users WHERE user_id = ?", (callback.from_user.id,)) as cursor:
            m_score = await cursor.fetchone()
            my_score = m_score[0] if m_score else 0

        await bot.send_message(sender_id, f"🎉 Собеседник подтвердил твоего котика! Твой счет: {sender_score}")
        await callback.message.answer(f"✅ Засчитано! У собеседника теперь {sender_score} 🐈\nТвой счет: {my_score}")

    elif action == "no":
        await bot.send_message(sender_id, "📸 Собеседник отметил твое фото как обычный снимок.")
        await callback.message.answer("Принято!")

    elif action == "report":
        await bot.send_message(sender_id, "⚠️ На ваше фото поступила жалоба. Ожидайте решения модератора.")
        await callback.message.answer("🚨 Жалоба успешно отправлена администратору. Спасибо за бдительность!")

        if MODERATION_CHAT_ID and file_id: # Проверяем, что ID есть
            try:
                await bot.send_photo(
                    MODERATION_CHAT_ID,
                    file_id,  # <--- ИСПОЛЬЗУЕМ ПЕРЕДАННЫЙ ID
                    caption=(
                        f"🚨 <b>ЖАЛОБА НА ФОТО (НСФВ/Спам)</b>\n"
                        f"Нарушитель ID: <code>{sender_id}</code>\n"
                        f"Жалуется ID: <code>{callback.from_user.id}</code>\n"
                    ),
                    reply_markup=get_moderation_kb(sender_id),
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Ошибка отправки жалобы на фото: {e}")


# --- ОБРАБОТКА ЧАТА И КАРТИНОК ---
@router.message()
async def handle_chat_and_media(message: Message):
    user_id = message.from_user.id
    
    # Получаем данные пользователя
    async with db_conn.execute("SELECT status, current_opponent FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()

    if not res or res[0] == 'banned':
        return

    # Логика для тех, кто не в игре
    if res[0] != 'playing':
        if message.photo:
            await message.answer("❌ Ты не можешь отправлять котиков просто так! Сначала нажми «🔍 Найти игрока».")
        elif message.text not in ["🔍 Найти игрока", "🏆 Таблица лидеров", "⚙️ Изменить профиль"]:
            await message.answer("Воспользуйся кнопками меню!", reply_markup=get_main_menu())
        return

    opponent_id = res[1]
    
    # Если оппонент потерян
    if not opponent_id:
        await db_conn.execute("UPDATE users SET status = 'idle' WHERE user_id = ?", (user_id,))
        await db_conn.commit()
        await message.answer("⚠️ Ошибка: собеседник потерян. Пожалуйста, начни поиск заново.", reply_markup=get_main_menu())
        return

    # Обработка фото
    if message.photo:
        photo = message.photo[-1]
        file_unique_id = photo.file_unique_id

        file_id = photo.file_id 

        verify_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🐱 Да, это кот!", callback_data=f"check_cat:yes:{user_id}:{file_unique_id}"),
                InlineKeyboardButton(text="📸 Просто фото", callback_data=f"check_cat:no:{user_id}:{file_unique_id}")
            ],
            [
                # Сюда добавляем :file_id в конец строки
                InlineKeyboardButton(text="🚨 Пожаловаться (НСФВ/Спам)", callback_data=f"check_cat:report:{user_id}:{file_unique_id}:{file_id}")
            ]
        ])

        await message.answer("⏳ Отправил фото собеседнику на подтверждение...")
        
        await bot.send_photo(
            opponent_id, 
            photo.file_id, 
            caption="<b>[Фото от собеседника]</b>\nЭто котик? Подтверди, чтобы ему засчитался балл! 👇", 
            reply_markup=verify_kb,
            parse_mode="HTML"
        )
        return

   if message.text:
        # Игнорируем кнопки, если они случайно проскочили
        if message.text in ["🔍 Найти игрока", "🏆 Таблица лидеров", "⚙️ Изменить профиль", "🏁 Завершить игру", "🚨 Пожаловаться на собеседника"]:
            return
        
        # АСИНХРОННАЯ ЗАПИСЬ
        await db_conn.execute("INSERT INTO chat_history (sender_id, receiver_id, text) VALUES (?, ?, ?)", 
                             (user_id, opponent_id, message.text))
        await db_conn.commit()

        try:
            await bot.send_message(opponent_id, message.text)
        except Exception:
            await message.answer("Не удалось доставить сообщение собеседнику.")


async def main():
    await init_db()  # <--- Бот сначала создаст базу
    dp.include_router(router)
    await dp.start_polling(bot) # <--- Потом начнет работать

if __name__ == "__main__":
    asyncio.run(main())
