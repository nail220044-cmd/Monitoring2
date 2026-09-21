import asyncio
import json
import os
import sys
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ------------------- ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ -------------------
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
SECRET_PASSWORD = os.getenv("SECRET_PASSWORD")

if not TOKEN or not SECRET_PASSWORD:
    print("❌ ОШИБКА: Переменные BOT_TOKEN или SECRET_PASSWORD не найдены в файле .env!")
    sys.exit(1)

DATA_FILE = "data.json"
CURRENCIES = ["EGP", "ARS", "UZS", "AUD", "AZN", "KGS", "MNT"]

# ------------------- СЛОВАРЬ ШАБЛОНОВ (RU / EN) -------------------
TEMPLATES = {
    "std": {
        "RU": "⚠️ **[{curr}] ({provider})** Коллеги, на стороне банка наблюдаются технические трудности, из-за чего могут происходить отмены и задержки платежей. На нашей стороне всё работает штатно, трафик приостанавливать не требуется. Мы сообщим вам о восстановлении.",
        "EN": "⚠️ **[{curr}] ({provider})** Colleagues, technical issues are currently observed on the bank's side, which may cause failed transactions and delays. Systems on our side are operating normally; traffic does not need to be stopped. We'll let you know once restored."
    },
    "stop": {
        "RU": "⚠️ **[{curr}] ({provider})** Коллеги, на стороне банка ведутся технические работы, в связи с чем могут быть отмены и снижение конверсии.\n🛑 **Просим временно остановить трафик по данной валюте.**\nМы сообщим вам о восстановлении.",
        "EN": "⚠️ **[{curr}] ({provider})** Colleagues, technical maintenance is undergoing on the bank's side, which may result in higher failure rates and lower conversion.\n🛑 **Please temporarily stop processing traffic for this currency.**\nWe will let you know once restored."
    },
    "resolve": {
        "RU": "✅ **[{curr}] ({provider})** Коллеги, сервис работает в штатном режиме. Технические работы завершены.",
        "EN": "✅ **[{curr}] ({provider})** Colleagues, the service is fully operational. Maintenance resolved."
    }
}


# ------------------- ХРАНИЛИЩЕ ДАННЫХ (JSON) -------------------
def load_data():
    if not os.path.exists(DATA_FILE):
        return {"chats": {}, "active_incidents": [], "authorized_users": []}

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except Exception:
            data = {"chats": {}, "active_incidents": [], "authorized_users": []}

        if "authorized_users" not in data:
            data["authorized_users"] = []
        if "chats" not in data:
            data["chats"] = {}
        if "active_incidents" not in data or isinstance(data["active_incidents"], dict):
            data["active_incidents"] = []
        return data


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def is_authorized(user_id: int) -> bool:
    data = load_data()
    return user_id in data.get("authorized_users", [])


def authorize_user(user_id: int):
    data = load_data()
    if user_id not in data["authorized_users"]:
        data["authorized_users"].append(user_id)
        save_data(data)


def parse_chat_id(raw_id: str):
    """Разбирает ID вида '-10044442718018_1' на chat_id (-10044442718018) и thread_id (1)"""
    if "_" in raw_id:
        parts = raw_id.split("_")
        return int(parts[0]), int(parts[1])
    return int(raw_id), None


# ------------------- ИНИЦИАЛИЗАЦИЯ -------------------
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class AuthState(StatesGroup):
    waiting_for_password = State()


class IncidentState(StatesGroup):
    waiting_for_currency = State()
    waiting_for_provider = State()
    selecting_chats = State()
    waiting_for_type = State()
    waiting_for_custom_text = State()


class ResolveState(StatesGroup):
    selecting_resolve_chats = State()


# ------------------- КЛАВИАТУРЫ -------------------
def main_keyboard():
    kb = [
        [KeyboardButton(text="🚨 Оповестить о просадке")],
        [KeyboardButton(text="✅ Зафиксировать восстановление")],
        [KeyboardButton(text="📊 Показать имеющиеся просадки")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, selective=True)


def currencies_keyboard():
    buttons = [[InlineKeyboardButton(text=curr, callback_data=f"curr_{curr}")] for curr in CURRENCIES]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_chats_selection_keyboard(target_chats: dict, selected_chat_ids: list, action_type="alert"):
    buttons = []
    selected_chat_ids_str = [str(x) for x in selected_chat_ids]

    for cid_str, info in target_chats.items():
        is_selected = cid_str in selected_chat_ids_str
        icon = "✅" if is_selected else "❌"
        btn_text = f"{icon} {info.get('name', cid_str)} ({info.get('lang', 'RU')})"

        prefix = "togglechat_" if action_type == "alert" else "toggleresolve_"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"{prefix}{cid_str}")])

    done_btn_text = "➡️ ПРОДОЛЖИТЬ" if action_type == "alert" else "➡️ ПОДТВЕРДИТЬ ВОССТАНОВЛЕНИЕ"
    done_callback = "chats_done" if action_type == "alert" else "finish_resolve_done"

    buttons.append([InlineKeyboardButton(text=done_btn_text, callback_data=done_callback)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ------------------- БЕСШУМНАЯ ОЧИСТКА КЛАВИАТУРЫ -------------------
@dp.message(Command("clean_kb"))
async def clean_keyboard(message: types.Message):
    try:
        msg = await message.answer(".", reply_markup=ReplyKeyboardRemove())
        await message.delete()
        await msg.delete()
    except Exception:
        pass


@dp.message(F.chat.type != "private")
async def catch_chat_id(message: types.Message):
    thread_suffix = f"_{message.message_thread_id}" if message.message_thread_id else ""
    full_id = f"{message.chat.id}{thread_suffix}"
    print(f"\n🎯 НАСТОЯЩИЙ ID ЧАТА '{message.chat.title}': {full_id}\n")


# ------------------- АВТОРИЗАЦИЯ -------------------
@dp.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: types.Message, state: FSMContext):
    if is_authorized(message.from_user.id):
        await message.answer("Добро пожаловать! Бот готов к работе.", reply_markup=main_keyboard())
    else:
        await state.set_state(AuthState.waiting_for_password)
        await message.answer("🔒 Доступ ограничен. Пожалуйста, введите пароль для доступа к боту:")


@dp.message(Command("start"))
async def cmd_start_group(message: types.Message):
    pass


@dp.message(AuthState.waiting_for_password, F.chat.type == "private")
async def process_password(message: types.Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass

    if message.text.strip() == SECRET_PASSWORD:
        authorize_user(message.from_user.id)
        await state.clear()
        await message.answer("🔑 Пароль верен! Доступ предоставлен.", reply_markup=main_keyboard())
    else:
        await message.answer("❌ Неверный пароль! Попробуйте ввести снова:")


# ------------------- УПРАВЛЕНИЕ ЧАТАМИ -------------------
@dp.message(Command("add_chat"), F.chat.type == "private")
async def cmd_add_chat(message: types.Message):
    if not is_authorized(message.from_user.id):
        return await message.answer("🛑 Введите пароль через /start")

    try:
        raw_args = message.text.split()[1:]

        if len(raw_args) < 2:
            raise ValueError("Недостаточно аргументов")

        chat_key = raw_args[0].strip()
        chat_id, thread_id = parse_chat_id(chat_key)
        currencies = [c.strip().upper() for c in raw_args[1].split(",") if c.strip()]

        lang = "RU"
        merchant_name = "Без названия"
        tags = []

        if len(raw_args) >= 3 and raw_args[2].upper() in ["RU", "EN"]:
            lang = raw_args[2].upper()
            rem_args = raw_args[3:]
        else:
            rem_args = raw_args[2:]

        if rem_args:
            if "@" in rem_args[-1]:
                tags = [t.strip() for t in rem_args[-1].split(",") if t.strip().startswith("@")]
                merchant_name = " ".join(rem_args[:-1]) if len(rem_args) > 1 else merchant_name
            else:
                merchant_name = " ".join(rem_args)

        data = load_data()
        data["chats"][chat_key] = {
            "chat_id": chat_id,
            "thread_id": thread_id,
            "name": merchant_name,
            "currencies": currencies,
            "lang": lang,
            "tags": tags,
            "is_active": True
        }
        save_data(data)

        tags_str = ", ".join(tags) if tags else "нет"
        thread_info = f" (Ветка ID: {thread_id})" if thread_id else ""
        await message.answer(
            f"✅ Чат `{chat_key}`{thread_info} (**{merchant_name}**) зарегистрирован!\n"
            f"• Валюты: **{', '.join(currencies)}**\n"
            f"• Язык общения: **{lang}**\n"
            f"• Теги ответственных: **{tags_str}**",
            parse_mode="Markdown"
        )
    except Exception:
        await message.answer(
            "⚠️ **Ошибка формата.**\nИспользуйте: `/add_chat <chat_id> <валюты> <RU/EN> <Имя_мерча> [теги]`\n"
            "*Обычный чат:* `/add_chat -1005459446650 KGS,UZS RU PINCO @alex`\n"
            "*Чат с веткой:* `/add_chat -10044442718018_1 KGS,UZS RU PINCO @alex`",
            parse_mode="Markdown"
        )


@dp.message(Command("bulk_add"), F.chat.type == "private")
async def cmd_bulk_add_chats(message: types.Message):
    if not is_authorized(message.from_user.id):
        return await message.answer("🛑 Введите пароль через /start")

    raw_text = message.text.split(maxsplit=1)
    if len(raw_text) < 2:
        return await message.answer(
            "⚠️ **Формат массового импорта:**\n\n"
            "```text\n"
            "/bulk_add\n"
            "-1005459446650 KGS,UZS RU PINCO @alex\n"
            "-10044442718018_1 EGP EN Mostbet @john\n"
            "
