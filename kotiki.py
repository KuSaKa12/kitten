import socket
import sys
import asyncio
CHANNEL_ID = "@ITkaktusik"

# Настройки для Windows
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    old_getaddrinfo = socket.getaddrinfo

    def new_getaddrinfo(*args, **kwargs):
        responses = old_getaddrinfo(*args, **kwargs)
        return [r for r in responses if r[0] == socket.AF_INET]

    socket.getaddrinfo = new_getaddrinfo

import os
from dotenv import load_dotenv
import logging
import sqlite3
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, \
    InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

logging.basicConfig(level=logging.INFO)
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# Подключение к БД
db_path = "/app/data/cat_game.db"
os.makedirs(os.path.dirname(db_path), exist_ok=True)
conn = sqlite3.connect(db_path, check_same_thread=False)
cursor = conn.cursor()

# Таблица пользователей
cursor.execute('''
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
cursor.execute('''
CREATE TABLE IF NOT EXISTS cat_photos (
    file_unique_id TEXT PRIMARY KEY,
    user_id INTEGER
)''')
conn.commit()


class Registration(StatesGroup):
    waiting_for_rules = State()
    waiting_for_age = State()
    waiting_for_gender = State()
    waiting_for_target_gender = State()


# --- КЛАВИАТУРЫ ---
def get_age_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Меньше 10 лет"), KeyboardButton(text="10 - 12 лет")],
        [KeyboardButton(text="13 - 15 лет"), KeyboardButton(text="16 лет и старше")]
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


def get_game_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🏁 Завершить игру")]
    ], resize_keyboard=True)


# Инлайн-кнопки подтверждения выхода
def get_confirm_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, выйти", callback_data="confirm_exit_yes"),
            InlineKeyboardButton(text="❌ Нет, играем дальше", callback_data="confirm_exit_no")
        ]
    ])


# --- ФУНКЦИЯ СТАРТА РЕГИСТРАЦИИ ---
async def start_registration_flow(message: Message, state: FSMContext, text_prefix=""):
    await message.answer(
        f"{text_prefix}Давай настроим твой профиль. Укажи свой возраст:",
        reply_markup=get_age_kb()
    )
    await state.set_state(Registration.waiting_for_age)

@router.message(F.text == "/debug_data")
async def debug_data(message: Message):
    cursor.execute("SELECT user_id, username, total_cats FROM users")
    all_users = cursor.fetchall()
    if not all_users:
        await message.answer("БД пуста!")
    else:
        text = "Данные в БД:\n"
        for u in all_users:
            text += f"ID: {u[0]} | Name: {u[1]} | Cats: {u[2]}\n"
        await message.answer(text)

# --- СТАРТ ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id

    cursor.execute("SELECT age_category FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()

    if res and res[0] is not None:
        cursor.execute(
            "UPDATE users SET status = 'idle', current_opponent = NULL, current_match_cats = 0 WHERE user_id = ?",
            (user_id,))
        conn.commit()
        await message.answer("Привет снова! Готов к поиску?", reply_markup=get_main_menu())
        return

    rules_text = (
        "🐾 Добро пожаловать в игру «Котолов»!\n\n"
        "Этот бот создан для весьма необычного знакомства! Ищите друга, вторую половику или просто собеседника! Общайтесь, гуляйте и делайте фотки милых котеек на улице!\n\n"
        "Чтобы игра приносила радость всем, пожалуйста, соблюдай правила:\n\n"
        "1️⃣ Будь честен: Не используй фото из интернета. Мы здесь, чтобы делиться живыми эмоциями!\n"
        "2️⃣ Свежие фото: Присылай снимки, которые сделал сам прямо сейчас.\n"
        "3️⃣ Разнообразие: Не спамь одним и тем же котиком 100 раз. Один ракурс — один котик!\n"
        "4️⃣ Только котики: Отправляй в чат только фотографии кошек.\n\n"
        "Нажимая кнопку ниже, ты подтверждаешь, что готов играть честно! 🐱"
    )
    await message.answer(
        rules_text, 
        reply_markup=get_rules_inline_kb(), 
        parse_mode="Markdown"
    )
    await state.set_state(Registration.waiting_for_rules)

# --- КНОПКА: ИЗМЕНИТЬ ПРОФИЛЬ ---
@router.message(F.text == "⚙️ Изменить профиль")
async def change_profile(message: Message, state: FSMContext):
    user_id = message.from_user.id
    cursor.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()

    # Защита от незарегистрированных юзеров
    if not res:
        await cmd_start(message, state)
        return

    if res[0] == 'playing':
        await message.answer("Вы не можете изменить профиль во время игры! Сначала завершите текущий матч.")
        return

    if res[0] == 'searching':
        cursor.execute("UPDATE users SET status = 'idle' WHERE user_id = ?", (user_id,))
        conn.commit()

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
    if message.text not in ["Меньше 10 лет", "10 - 12 лет", "13 - 15 лет", "16 лет и старше"]:
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

    cursor.execute("SELECT total_cats FROM users WHERE user_id = ?", (user_id,))
    old_total = cursor.fetchone()
    total_cats_to_save = old_total[0] if old_total else 0

    cursor.execute('''
        INSERT OR REPLACE INTO users (user_id, username, age_category, gender, target_gender, status, current_match_cats, total_cats)
        VALUES (?, ?, ?, ?, ?, 'idle', 0, ?)
    ''', (user_id, username, user_data['age_category'], user_data['gender'], target_val, total_cats_to_save))
    conn.commit()

    await state.clear()
    await message.answer(
        "🎉 Профиль успешно обновлен!\n"
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

    cursor.execute("SELECT status, gender, target_gender FROM users WHERE user_id = ?", (user_id,))
    u_data = cursor.fetchone()
    
    # 1. Защита от юзеров, которых нет в БД (предотвращает создание "невидимок")
    if not u_data:
        await cmd_start(message, state)
        return

    current_status, u_gender, u_target = u_data

    if current_status == 'playing':
        await message.answer("Ты уже находишься в активной игре!", reply_markup=get_game_menu())
        return

    # 2. Ищем соперника, у которого статус 'searching' И который подходит по полу
    query = """
        SELECT user_id FROM users 
        WHERE status = 'searching' 
          AND user_id != ? 
          AND (target_gender = 'any' OR target_gender = ?)
          AND (? = 'any' OR gender = ?)
        LIMIT 1
    """
    cursor.execute(query, (user_id, u_gender, u_target, u_target))
    opponent = cursor.fetchone()

    if opponent:
        opponent_id = opponent[0]
        
        # Обнуляем match_cats и связываем игроков
        cursor.execute("UPDATE users SET status = 'playing', current_opponent = ?, current_match_cats = 0 WHERE user_id = ?", (opponent_id, user_id))
        cursor.execute("UPDATE users SET status = 'playing', current_opponent = ?, current_match_cats = 0 WHERE user_id = ?", (user_id, opponent_id))
        conn.commit()

        await message.answer("🎉 Соперник найден! Начинаем игру. Присылай фото котиков!", reply_markup=get_game_menu())
        await bot.send_message(opponent_id, "🎉 Соперник найден! Начинаем игру. Присылай фото котиков!", reply_markup=get_game_menu())
    else:
        cursor.execute("UPDATE users SET status = 'searching', current_opponent = NULL WHERE user_id = ?", (user_id,))
        conn.commit()
        await message.answer("🔍 Ищем соперника... Пожалуйста, подожди.")


# --- ЗАПРОС НА ЗАВЕРШЕНИЕ ИГРЫ ---
@router.message(F.text == "🏁 Завершить игру")
async def ask_end_game(message: Message):
    user_id = message.from_user.id
    cursor.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()

    if res and res[0] == 'playing':
        await message.answer(
            "⚠️ Ты уверен, что хочешь завершить игру?\n"
            "Твой счётчик котов в этой игре сбросится!",
            reply_markup=get_confirm_inline_kb()
        )
    else:
        await message.answer("Ты сейчас не в игре.", reply_markup=get_main_menu())


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

    cursor.execute("SELECT status, current_opponent FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()

    if not res or res[0] != 'playing':
        await bot.send_message(user_id, "Вы уже не находитесь в игре.", reply_markup=get_main_menu())
        return

    if action == "no":
        await bot.send_message(user_id, "Отлично, продолжаем игру! Жду фото котиков.")
        return

    if action == "yes":
        opponent_id = res[1]
        
        cursor.execute("SELECT current_match_cats FROM users WHERE user_id = ?", (user_id,))
        my_score = cursor.fetchone()[0]

        cursor.execute("SELECT current_match_cats FROM users WHERE user_id = ?", (opponent_id,))
        opp_score_data = cursor.fetchone()
        opp_score = opp_score_data[0] if opp_score_data else 0

        # Очищаем статусы в БД перед выводом сообщений
        cursor.execute(
            "UPDATE users SET status = 'idle', current_opponent = NULL, current_match_cats = 0 WHERE user_id IN (?, ?)",
            (user_id, opponent_id))
        conn.commit()

        text_for_me = f"Игра завершена!\n📊 Твой счет в этом раунде: 🐈 {my_score}\n📊 Счет соперника: 🐈 {opp_score}"
        text_for_opp = f"⚠️ Соперник завершил игру.\n\nИгра завершена!\n📊 Твой счет в этом раунде: 🐈 {opp_score}\n📊 Счет соперника: 🐈 {my_score}"

        await bot.send_message(user_id, text_for_me + "\n\nВозвращаемся в главное меню.", reply_markup=get_main_menu())
        await bot.send_message(opponent_id, text_for_opp + "\n\nВозвращаемся в главное меню.", reply_markup=get_main_menu())

async def check_subscription(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        if member.status in ['creator', 'administrator', 'member']:
            return True
        return False
    except Exception as e:
        # Если бот НЕ добавлен в администраторы канала, Telegram выдаст ошибку.
        # Временно возвращаем True, чтобы бот не ломался при тестировании функционала.
        logging.error(f"Ошибка проверки подписки (возможно бот не админ): {e}")
        return True 


# --- ТАБЛИЦА ЛИДЕРОВ ---
@router.message(F.text == "🏆 Таблица лидеров")
async def show_leaderboard(message: Message):
    cursor.execute("SELECT username, total_cats FROM users ORDER BY total_cats DESC LIMIT 10")
    leaders = cursor.fetchall()
    
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


# --- ОБРАБОТКА ЧАТА И КАРТИНОК ---
@router.message()
async def handle_chat_and_media(message: Message):
    user_id = message.from_user.id
    cursor.execute("SELECT status, current_opponent FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()

    # Если юзер не в базе, не даем спамить
    if not res:
        return

    if res[0] != 'playing':
        if message.photo:
            await message.answer(
                "❌ Ты не можешь отправлять котиков просто так! Сначала нажми «🔍 Найти игрока» и найди соперника.")
        else:
            if message.text in ["🔍 Найти игрока", "🏆 Таблица лидеров", "⚙️ Изменить профиль"]:
                return
            await message.answer("Воспользуйся кнопками меню!", reply_markup=get_main_menu())
        return

    opponent_id = res[1]
    
    # 3. Защита от потери оппонента (если БД рассинхронизировалась)
    if not opponent_id:
        cursor.execute("UPDATE users SET status = 'idle' WHERE user_id = ?", (user_id,))
        conn.commit()
        await message.answer("⚠️ Ошибка: соперник потерян. Пожалуйста, начни поиск заново.", reply_markup=get_main_menu())
        return

    if message.photo:
        photo = message.photo[-1]
        file_unique_id = photo.file_unique_id

        cursor.execute("SELECT user_id FROM cat_photos WHERE file_unique_id = ?", (file_unique_id,))
        if cursor.fetchone():
            await message.answer("❌ Это фото уже загружалось в игру! Отправь другое фото.")
            return

        cursor.execute("INSERT INTO cat_photos (file_unique_id, user_id) VALUES (?, ?)", (file_unique_id, user_id))
        cursor.execute(
            "UPDATE users SET current_match_cats = current_match_cats + 1, total_cats = total_cats + 1 WHERE user_id = ?",
            (user_id,))
        conn.commit()

        cursor.execute("SELECT current_match_cats FROM users WHERE user_id = ?", (user_id,))
        my_match_count = cursor.fetchone()[0]

        cursor.execute("SELECT current_match_cats FROM users WHERE user_id = ?", (opponent_id,))
        opp_match_count = cursor.fetchone()[0]

        await message.answer(f"🎉 Котик засчитан! У тебя в этой игре: {my_match_count}")
        
        await bot.send_message(
            opponent_id, 
            f"💥 Соперник нашел котика! У него теперь {my_match_count} шт.\n"
            f"Твой счет в этой игре: {opp_match_count}. Поторопись!"
        )
        await bot.send_photo(opponent_id, photo.file_id, caption="[Фото кота от соперника!]")
        return

    if message.text:
        if message.text in ["🔍 Найти игрока", "🏆 Таблица лидеров", "⚙️ Изменить профиль", "🏁 Завершить игру"]:
            return
        try:
            await bot.send_message(opponent_id, message.text)
        except Exception:
            await message.answer("Не удалось доставить сообщение собеседнику.")


async def main():
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
