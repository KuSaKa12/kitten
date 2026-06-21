Вот твой полный, готовый к деплою код. Я объединил всё: структуру, работу с токеном, базы данных, клавиатуры, регистрацию, игру и функцию отмены поиска.

Просто скопируй этот текст целиком, замени содержимое файла kotiki.py и сделай коммит на GitHub.

Python
import socket
import sys
import asyncio
import os
import logging
import sqlite3
import random
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (Message, ReplyKeyboardMarkup, KeyboardButton, 
                          ReplyKeyboardRemove, InlineKeyboardMarkup, 
                          InlineKeyboardButton, CallbackQuery)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# Настройки для Windows (для локальной разработки)
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

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# Подключение к БД
conn = sqlite3.connect("cat_game.db", check_same_thread=False)
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
    waiting_for_age = State()
    waiting_for_gender = State()
    waiting_for_target_gender = State()

# --- КЛАВИАТУРЫ ---
def get_age_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Меньше 10 лет"), KeyboardButton(text="10 - 12 лет")],
        [KeyboardButton(text="13 - 15 лет"), KeyboardButton(text="16 лет и старше")]
    ], resize_keyboard=True, one_time_keyboard=True)

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

def get_searching_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="❌ Отменить поиск")]
    ], resize_keyboard=True)

def get_confirm_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, выйти", callback_data="confirm_exit_yes"),
            InlineKeyboardButton(text="❌ Нет, играем дальше", callback_data="confirm_exit_no")
        ]
    ])

# --- РЕГИСТРАЦИЯ ---
async def start_registration_flow(message: Message, state: FSMContext, text_prefix=""):
    await message.answer(f"{text_prefix}Давай настроим профиль. Укажи возраст:", reply_markup=get_age_kb())
    await state.set_state(Registration.waiting_for_age)

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    cursor.execute("SELECT age_category FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()

    if res and res[0] is not None:
        cursor.execute("UPDATE users SET status = 'idle', current_opponent = NULL, current_match_cats = 0 WHERE user_id = ?", (user_id,))
        conn.commit()
        await message.answer("Привет снова! Готов к поиску?", reply_markup=get_main_menu())
        return
    await start_registration_flow(message, state, "Привет! Добро пожаловать в бота знакомств! 🐾\n")

# --- ОТМЕНА ПОИСКА ---
@router.message(F.text == "❌ Отменить поиск")
async def cancel_search(message: Message):
    user_id = message.from_user.id
    cursor.execute("UPDATE users SET status = 'idle' WHERE user_id = ?", (user_id,))
    conn.commit()
    await message.answer("Поиск отменен. Ты вернулся в меню.", reply_markup=get_main_menu())

# --- ПОИСК ИГРОКА ---
@router.message(F.text == "🔍 Найти игрока")
async def find_player(message: Message):
    user_id = message.from_user.id
    cursor.execute("SELECT status, age_category, gender, target_gender FROM users WHERE user_id = ?", (user_id,))
    user_info = cursor.fetchone()

    if not user_info:
        await message.answer("Сначала пройди регистрацию /start")
        return

    status, my_age, my_gender, my_target = user_info
    if status == 'searching':
        await message.answer("Ты уже ищешь!")
        return
    if status == 'playing':
        await message.answer("Ты уже в игре!")
        return

    cursor.execute("UPDATE users SET status = 'searching' WHERE user_id = ?", (user_id,))
    conn.commit()

    query = 'SELECT user_id FROM users WHERE status = "searching" AND user_id != ? AND age_category = ?'
    params = [user_id, my_age]
    if my_target != 'any':
        query += " AND gender = ?"
        params.append(my_target)
    query += " AND (target_gender = 'any' OR target_gender = ?)"
    params.append(my_gender)

    cursor.execute(query, tuple(params))
    opponents = cursor.fetchall()

    if opponents:
        opponent_id = random.choice(opponents)[0]
        cursor.execute("UPDATE users SET status = 'playing', current_opponent = ?, current_match_cats = 0 WHERE user_id = ?", (opponent_id, user_id))
        cursor.execute("UPDATE users SET status = 'playing', current_opponent = ?, current_match_cats = 0 WHERE user_id = ?", (user_id, opponent_id))
        conn.commit()
        msg = "🎮 Пара найдена! Присылай фото котиков!"
        await message.answer(msg, reply_markup=get_game_menu())
        await bot.send_message(opponent_id, msg, reply_markup=get_game_menu())
    else:
        await message.answer("⏳ Ищу соперника...", reply_markup=get_searching_menu())

# --- ОБРАБОТКА ИГРЫ ---
@router.message(F.text == "🏁 Завершить игру")
async def ask_end_game(message: Message):
    user_id = message.from_user.id
    cursor.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    if res and res[0] == 'playing':
        await message.answer("Уверен?", reply_markup=get_confirm_inline_kb())
    else:
        await message.answer("Ты не в игре.", reply_markup=get_main_menu())

@router.callback_query(F.data.startswith("confirm_exit_"))
async def process_confirm_exit(callback: CallbackQuery):
    user_id = callback.from_user.id
    action = callback.data.split("_")[-1]
    await callback.message.delete()
    
    if action == "yes":
        cursor.execute("SELECT current_opponent, current_match_cats FROM users WHERE user_id = ?", (user_id,))
        res = cursor.fetchone()
        if res:
            opponent_id = res[0]
            cursor.execute("UPDATE users SET status = 'idle', current_opponent = NULL, current_match_cats = 0 WHERE user_id IN (?, ?)", (user_id, opponent_id))
            conn.commit()
            await bot.send_message(user_id, "Игра завершена.", reply_markup=get_main_menu())
            await bot.send_message(opponent_id, "Соперник завершил игру.", reply_markup=get_main_menu())

@router.message()
async def handle_chat_and_media(message: Message):
    user_id = message.from_user.id
    cursor.execute("SELECT status, current_opponent FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    if not res or res[0] != 'playing':
        await message.answer("Воспользуйся меню!", reply_markup=get_main_menu())
        return
    
    opponent_id = res[1]
    if message.photo:
        photo = message.photo[-1]
        cursor.execute("INSERT INTO cat_photos (file_unique_id, user_id) VALUES (?, ?)", (photo.file_unique_id, user_id))
        cursor.execute("UPDATE users SET current_match_cats = current_match_cats + 1, total_cats = total_cats + 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        await bot.send_message(opponent_id, "Соперник прислал котика!")
        await bot.send_photo(opponent_id, photo.file_id)
    elif message.text:
        await bot.send_message(opponent_id, message.text)

async def main():
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
