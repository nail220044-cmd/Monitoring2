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
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError

# ------------------- ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ -------------------
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
SECRET_PASSWORD = os.getenv("SECRET_PASSWORD")

if not TOKEN or not SECRET_PASSWORD:
    print("❌ ОШИБКА: Переменные BOT_TOKEN или SECRET_PASSWORD не найдены в файле .env!")
    sys.exit(1)

# Персистентная папка на ботхосте (сохраняется между пересборками контейнера)
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = "."  # локальный запуск / нет прав на /app/data - сохраняем рядом со скриптом

DATA_FILE = os.path.join(DATA_DIR, "data.json")
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
        return {"chats": {}, "active_incidents": [], "authorized_users": [], "next_incident_id": 1}

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except Exception:
            data = {"chats": {}, "active_incidents": [], "authorized_users": [], "next_incident_id": 1}

        if "authorized_users" not in data:
            data["authorized_users"] = []
        if "chats" not in data:
            data["chats"] = {}
        if "active_incidents" not in data or isinstance(data["active_incidents"], dict):
            data["active_incidents"] = []
        if "next_incident_id" not in data:
            # миграция со старой схемы (id = len(active_incidents)+1): берём максимум уже
            # выданных id среди активных инцидентов + 1, чтобы не выдать повторный номер
            existing_ids = [inc.get("id", 0) for inc in data["active_incidents"]]
            data["next_incident_id"] = (max(existing_ids) + 1) if existing_ids else 1
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


def escape_md(text: str) -> str:
    """Экранирует спецсимволы legacy Markdown (_, *, `, [), чтобы они не ломали парсинг
    (например подчёркивания в тегах вида @team_24h_monitoring)."""
    if not text:
        return text
    for ch in ("\\", "_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def parse_chat_id(raw_id: str):
    """Разбирает ID вида '-10044442718018_1' на chat_id (-10044442718018) и thread_id (1)"""
    if "_" in raw_id:
        parts = raw_id.split("_")
        return int(parts[0]), int(parts[1])
    return int(raw_id), None


# ------------------- ЛОГ ОШИБОК В ФАЙЛ (раз на ботхосте нет консоли) -------------------
LOG_FILE = os.path.join(DATA_DIR, "errors.log")


def log_error(text: str):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


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
    waiting_for_custom_text_ru = State()
    waiting_for_custom_text_en = State()


class ResolveState(StatesGroup):
    selecting_resolve_chats = State()


# ------------------- КЛАВИАТУРЫ -------------------
MENU_BUTTON_TEXTS = {
    "🚨 Оповестить о просадке",
    "✅ Зафиксировать восстановление",
    "📊 Показать имеющиеся просадки",
}


def is_menu_button(text: str) -> bool:
    return (text or "").strip() in MENU_BUTTON_TEXTS


def is_reserved_input(text: str) -> bool:
    """Считает 'зарезервированным' любой текст, который не должен восприниматься как ввод -
    кнопки меню и любые команды (начинаются с /). Нужно, чтобы завис<ший диалог не проглатывал
    команды вроде /force_resolve, воспринимая их как обычный текст."""
    t = (text or "").strip()
    return t in MENU_BUTTON_TEXTS or t.startswith("/")


def main_keyboard():
    kb = [
        [KeyboardButton(text="🚨 Оповестить о просадке")],
        [KeyboardButton(text="✅ Зафиксировать восстановление")],
        [KeyboardButton(text="📊 Показать имеющиеся просадки")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, selective=True)


def cancel_button_row():
    return [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow")]


def currencies_keyboard():
    buttons = [[InlineKeyboardButton(text=curr, callback_data=f"curr_{curr}")] for curr in CURRENCIES]
    buttons.append(cancel_button_row())
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
    buttons.append(cancel_button_row())
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cancel_only_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[cancel_button_row()])


@dp.callback_query(F.data == "cancel_flow")
async def cancel_flow(callback: types.CallbackQuery, state: FSMContext):
    """Универсальная отмена - работает на любом шаге сценария оповещения/восстановления."""
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.edit_text("❌ Действие отменено.")
    except Exception:
        pass
    await callback.message.answer("Главное меню:", reply_markup=main_keyboard())


@dp.message(Command("cancel"), F.chat.type == "private")
async def cmd_cancel(message: types.Message, state: FSMContext):
    """Текстовый аналог кнопки отмены - на случай, если инлайн-кнопки не видно (старое сообщение и т.п.)."""
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=main_keyboard())


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

        tags_str = ", ".join(escape_md(t) for t in tags) if tags else "нет"
        thread_info = f" (Ветка ID: {thread_id})" if thread_id else ""
        await message.answer(
            f"✅ Чат `{chat_key}`{thread_info} (**{escape_md(merchant_name)}**) зарегистрирован!\n"
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
            "```",
            parse_mode="Markdown"
        )

    lines = raw_text[1].strip().split("\n")
    data = load_data()
    added_count = 0
    errors = []

    for idx, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) < 2:
            errors.append(f"Строка {idx}: недостаточно данных")
            continue

        try:
            chat_key = parts[0].strip()
            chat_id, thread_id = parse_chat_id(chat_key)
            currencies = [c.strip().upper() for c in parts[1].split(",") if c.strip()]

            lang = "RU"
            rem_args = parts[2:]

            if rem_args and rem_args[0].upper() in ["RU", "EN"]:
                lang = rem_args[0].upper()
                rem_args = rem_args[1:]

            tags = []
            merchant_name = "Без названия"

            if rem_args:
                if "@" in rem_args[-1]:
                    tags = [t.strip() for t in rem_args[-1].split(",") if t.strip().startswith("@")]
                    merchant_name = " ".join(rem_args[:-1]) if len(rem_args) > 1 else merchant_name
                else:
                    merchant_name = " ".join(rem_args)

            data["chats"][chat_key] = {
                "chat_id": chat_id,
                "thread_id": thread_id,
                "name": merchant_name,
                "currencies": currencies,
                "lang": lang,
                "tags": tags,
                "is_active": True
            }
            added_count += 1
        except Exception:
            errors.append(f"Строка {idx}: ошибка формата ID (`{parts[0]}`)")

    save_data(data)

    report = f"🎉 **Успешно заведено чатов: {added_count}**\n"
    if errors:
        report += "\n⚠️ **Ошибки в строках:**\n" + "\n".join(errors)

    await message.answer(report, parse_mode="Markdown")


@dp.message(Command("del_chat"), F.chat.type == "private")
async def cmd_del_chat(message: types.Message):
    if not is_authorized(message.from_user.id):
        return await message.answer("🛑 Введите пароль через /start")

    try:
        raw_args = message.text.split()[1:]
        chat_key = raw_args[0].strip()

        data = load_data()
        if chat_key in data["chats"]:
            name = data["chats"][chat_key].get("name", "Без названия")
            del data["chats"][chat_key]
            save_data(data)
            await message.answer(f"🗑 Чат `{chat_key}` ({escape_md(name)}) успешно удален из базы.", parse_mode="Markdown")
        else:
            await message.answer(f"❌ Чат с ID `{chat_key}` не найден в базе.", parse_mode="Markdown")
    except Exception:
        await message.answer("⚠️ Формат команды: `/del_chat <chat_id>`", parse_mode="Markdown")


@dp.message(Command("clear_all_chats"), F.chat.type == "private")
async def cmd_clear_all_chats(message: types.Message):
    if not is_authorized(message.from_user.id):
        return await message.answer("🛑 Введите пароль через /start")

    args = message.text.split()[1:]
    if not args or args[0] != "confirm":
        return await message.answer(
            "⚠️ **ВНИМАНИЕ! Эта команда полностью удалит ВСЕ чаты из базы!**\n\n"
            "Чтобы подтвердить полное удаление, отправьте:\n"
            "`/clear_all_chats confirm`",
            parse_mode="Markdown"
        )

    data = load_data()
    count = len(data.get("chats", {}))
    data["chats"] = {}
    save_data(data)

    await message.answer(f"💥 **База полностью очищена!** Удалено чатов: **{count}**.", parse_mode="Markdown")


@dp.message(Command("list_chats"), F.chat.type == "private")
async def cmd_list_chats(message: types.Message):
    if not is_authorized(message.from_user.id):
        return await message.answer("🛑 Введите пароль через /start")

    data = load_data()
    chats = data.get("chats", {})

    if not chats:
        return await message.answer("📋 Список зарегистрированных чатов пуст.")

    text = f"📋 **Зарегистрированные чаты (всего {len(chats)}):**\n\n"
    for cid, info in chats.items():
        tags = info.get("tags", [])
        safe_tags = [escape_md(t) for t in tags]
        tags_str = f" | cc: {' '.join(safe_tags)}" if safe_tags else ""
        safe_name = escape_md(info['name'])
        text += f"• `{cid}` | **{safe_name}** | [{info.get('lang', 'RU')}] | Валюты: {', '.join(info['currencies'])}{tags_str}\n"

    for chunk_start in range(0, len(text), 3500):
        await message.answer(text[chunk_start:chunk_start + 3500], parse_mode="Markdown")


# ------------------- ДИАГНОСТИКА: ПРОВЕРИТЬ ДОСТУПНОСТЬ ЧАТОВ -------------------
@dp.message(Command("check_chats"), F.chat.type == "private")
async def cmd_check_chats(message: types.Message):
    """Проходит по всем зарегистрированным чатам и проверяет, может ли бот туда писать."""
    if not is_authorized(message.from_user.id):
        return await message.answer("🛑 Введите пароль через /start")

    data = load_data()
    chats = data.get("chats", {})

    if not chats:
        return await message.answer("📋 Список зарегистрированных чатов пуст.")

    await message.answer(f"🔎 Проверяю {len(chats)} чат(ов), это может занять время...")

    ok_list = []
    fail_list = []

    for cid_str, info in chats.items():
        chat_id = info.get("chat_id")
        thread_id = info.get("thread_id")
        if not chat_id:
            chat_id, thread_id = parse_chat_id(cid_str)

        try:
            member = await bot.get_chat_member(chat_id, bot.id)
            status = member.status
            if status in ("kicked", "left"):
                fail_list.append(f"`{cid_str}` ({escape_md(info.get('name','?'))}) — бот статус: **{status}**")
            else:
                can_send = getattr(member, "can_post_messages", True)
                if status == "restricted" and not can_send:
                    fail_list.append(f"`{cid_str}` ({escape_md(info.get('name','?'))}) — бот **restricted**, нет прав писать")
                else:
                    ok_list.append(f"`{cid_str}` ({escape_md(info.get('name','?'))}) — ok ({status})")
        except TelegramAPIError as e:
            fail_list.append(f"`{cid_str}` ({escape_md(info.get('name','?'))}) — ошибка: `{e}`")
        except Exception as e:
            fail_list.append(f"`{cid_str}` ({escape_md(info.get('name','?'))}) — ошибка: `{e}`")

        await asyncio.sleep(0.1)

    report = f"✅ **Доступны:** {len(ok_list)}\n❌ **Проблемные:** {len(fail_list)}\n\n"
    if fail_list:
        report += "**Проблемные чаты:**\n" + "\n".join(fail_list)

    # Разбивка на части, если отчёт слишком длинный для одного сообщения
    for chunk_start in range(0, len(report), 3500):
        await message.answer(report[chunk_start:chunk_start + 3500], parse_mode="Markdown")


# ------------------- ПРИНУДИТЕЛЬНОЕ ЗАКРЫТИЕ ЗАВИСШЕГО ЧАТА В ИНЦИДЕНТЕ -------------------
@dp.message(Command("force_resolve"), F.chat.type == "private")
async def cmd_force_resolve(message: types.Message):
    """Убирает конкретный чат из активного инцидента без попытки реально отправить сообщение -
    для случаев, когда чат навсегда недоступен (бота кикнули, ветка не восстановится)."""
    if not is_authorized(message.from_user.id):
        return await message.answer("🛑 Введите пароль через /start")

    args = message.text.split()[1:]
    if len(args) < 2:
        return await message.answer(
            "⚠️ Формат: `/force_resolve <inc_id> <chat_id>`\n"
            "Узнать inc_id и chat_id можно из сообщения «📊 Показать имеющиеся просадки» "
            "или из отчёта об ошибке отправки (там chat_id уже указан).",
            parse_mode="Markdown"
        )

    try:
        inc_id = int(args[0])
    except ValueError:
        return await message.answer("⚠️ inc_id должен быть числом.")

    target_cid = args[1].strip()

    data = load_data()
    incident = next((x for x in data.get("active_incidents", []) if x["id"] == inc_id), None)

    if not incident:
        return await message.answer(f"❌ Инцидент с id `{inc_id}` не найден (возможно, уже закрыт).", parse_mode="Markdown")

    before_count = len(incident.get("messages", []))
    incident["messages"] = [
        m for m in incident.get("messages", [])
        if str(m.get("chat_key", m.get("chat_id"))) != target_cid
    ]
    after_count = len(incident["messages"])

    if before_count == after_count:
        return await message.answer(
            f"❌ Чат `{target_cid}` не найден в инциденте `{inc_id}` (уже закрыт или ID указан неверно).",
            parse_mode="Markdown"
        )

    if not incident["messages"]:
        data["active_incidents"] = [x for x in data["active_incidents"] if x["id"] != inc_id]
        save_data(data)
        await message.answer(
            f"✅ Чат `{target_cid}` принудительно закрыт.\n🟢 Это был последний чат — просадка **{escape_md(incident['currency'])} ({escape_md(incident['provider'])})** полностью закрыта.",
            parse_mode="Markdown"
        )
    else:
        save_data(data)
        await message.answer(
            f"✅ Чат `{target_cid}` принудительно закрыт (реальное сообщение о восстановлении НЕ отправлялось).\n⚠️ Осталось чатов в просадке: **{len(incident['messages'])}**.",
            parse_mode="Markdown"
        )


# ------------------- УДАЛЕНИЕ ОШИБОЧНО ОТПРАВЛЕННОГО ОПОВЕЩЕНИЯ ВО ВСЕХ ЧАТАХ -------------------
@dp.message(Command("delete_alert"), F.chat.type == "private")
async def cmd_delete_alert(message: types.Message):
    """Удаляет сообщения инцидента из всех чатов, куда они были отправлены (по chat_id + message_id,
    сохранённым при рассылке). Полезно для отзыва ошибочно отправленного оповещения.
    Ограничение Telegram: удалить можно только сообщение младше 48 часов и там, где у бота есть права."""
    if not is_authorized(message.from_user.id):
        return await message.answer("🛑 Введите пароль через /start")

    args = message.text.split()[1:]
    if not args:
        return await message.answer(
            "⚠️ Формат: `/delete_alert <inc_id>`\n"
            "inc_id возьми из «📊 Показать имеющиеся просадки».\n"
            "⚠️ Удалит сообщение ВО ВСЕХ чатах этого инцидента без возможности отмены.",
            parse_mode="Markdown"
        )

    try:
        inc_id = int(args[0])
    except ValueError:
        return await message.answer("⚠️ inc_id должен быть числом.")

    data = load_data()
    incident = next((x for x in data.get("active_incidents", []) if x["id"] == inc_id), None)

    if not incident:
        return await message.answer(f"❌ Инцидент с id `{inc_id}` не найден.", parse_mode="Markdown")

    deleted = []
    failed = []

    for item in incident.get("messages", []):
        cid_str = str(item.get("chat_key", item.get("chat_id")))
        chat_id = item.get("chat_id")
        message_id = item.get("message_id")
        name = data.get("chats", {}).get(cid_str, {}).get("name", cid_str)

        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            deleted.append(f"• `{cid_str}` ({escape_md(name)})")
        except Exception as e:
            log_error(f"[delete_alert] {cid_str} ({name}): {e}")
            failed.append(f"• `{cid_str}` ({escape_md(name)}): {escape_md(str(e))}")

    # инцидент закрываем полностью - сообщения либо удалены, либо их всё равно больше не восстановить штатно
    data["active_incidents"] = [x for x in data["active_incidents"] if x["id"] != inc_id]
    save_data(data)

    report = f"🗑 Удаление сообщений инцидента `{inc_id}` завершено.\n\n✅ **Удалено ({len(deleted)}):**\n" + ("\n".join(deleted) if deleted else "—")
    if failed:
        report += f"\n\n❌ **Не удалось удалить ({len(failed)}) - удали вручную в самом чате:**\n" + "\n".join(failed)

    for chunk_start in range(0, len(report), 3500):
        await message.answer(report[chunk_start:chunk_start + 3500], parse_mode="Markdown")


# ------------------- ХЕНДЛЕР 1: ОПОВЕСТИТЬ О ПРОСАДКЕ -------------------
@dp.message(F.text == "🚨 Оповестить о просадке", F.chat.type == "private")
async def start_incident(message: types.Message, state: FSMContext):
    if not is_authorized(message.from_user.id):
        return await message.answer("🛑 Введите пароль через /start")

    await state.set_state(IncidentState.waiting_for_currency)
    await message.answer("Выберите валюту, по которой возникла просадка:", reply_markup=currencies_keyboard())


@dp.callback_query(IncidentState.waiting_for_currency, F.data.startswith("curr_"))
async def process_currency(callback: types.CallbackQuery, state: FSMContext):
    currency = callback.data.split("_")[1]
    data = load_data()

    target_chats = {str(cid): info for cid, info in data["chats"].items() if currency in info["currencies"]}

    if not target_chats:
        await state.clear()
        return await callback.message.edit_text(f"❌ Нет чатов, привязанных к валюте **{currency}**.",
                                                parse_mode="Markdown")

    await state.update_data(selected_currency=currency, target_chats=target_chats)
    await state.set_state(IncidentState.waiting_for_provider)

    await callback.message.edit_text(
        f"Валюта: **{currency}**.\nУкажите банк / провайдера (например: *Kapitalbank* или *P2P Gateway*):",
        parse_mode="Markdown",
        reply_markup=cancel_only_keyboard()
    )


@dp.message(IncidentState.waiting_for_provider, F.chat.type == "private")
async def process_provider(message: types.Message, state: FSMContext):
    if is_reserved_input(message.text):
        await state.clear()
        return await message.answer(
            "⚠️ Действие отменено (получена кнопка меню или команда вместо текста).\n"
            "Если это была команда - отправьте её ещё раз, теперь она сработает.",
            reply_markup=main_keyboard()
        )

    provider = message.text.strip()
    user_data = await state.get_data()

    target_chats = user_data["target_chats"]
    currency = user_data["selected_currency"]
    selected_chat_ids = [str(cid) for cid in target_chats.keys()]

    await state.update_data(
        provider=provider,
        selected_chat_ids=selected_chat_ids
    )
    await state.set_state(IncidentState.selecting_chats)

    kb = build_chats_selection_keyboard(target_chats, selected_chat_ids, action_type="alert")
    await message.answer(
        f"Валюта: **{currency}** | Источник: **{escape_md(provider)}**.\nОтметьте чаты, в которые нужно отправить сообщение:",
        reply_markup=kb,
        parse_mode="Markdown"
    )


@dp.callback_query(IncidentState.selecting_chats, F.data.startswith("togglechat_"))
async def toggle_chat_selection(callback: types.CallbackQuery, state: FSMContext):
    chat_id = str(callback.data.split("togglechat_")[1])
    user_data = await state.get_data()

    target_chats = user_data["target_chats"]
    selected_chat_ids = [str(x) for x in user_data["selected_chat_ids"]]

    if chat_id in selected_chat_ids:
        selected_chat_ids.remove(chat_id)
    else:
        selected_chat_ids.append(chat_id)

    await state.update_data(selected_chat_ids=selected_chat_ids)

    kb = build_chats_selection_keyboard(target_chats, selected_chat_ids, action_type="alert")
    await callback.message.edit_reply_markup(reply_markup=kb)


@dp.callback_query(IncidentState.selecting_chats, F.data == "chats_done")
async def finish_chat_selection(callback: types.CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    selected_chat_ids = user_data["selected_chat_ids"]
    currency = user_data["selected_currency"]
    provider = user_data["provider"]

    if not selected_chat_ids:
        return await callback.answer("⚠️ Выберите хотя бы один чат!", show_alert=True)

    await state.set_state(IncidentState.waiting_for_type)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Стандарт (Работы на стороне банка)", callback_data="type_std")],
        [InlineKeyboardButton(text="🛑 Стандарт + СТОП трафик", callback_data="type_stop")],
        [InlineKeyboardButton(text="✏️ Ввести свой текст", callback_data="type_custom")],
        cancel_button_row()
    ])

    await callback.message.edit_text(
        f"Валюта: **{currency}** | Источник: **{escape_md(provider)}** (чатов: **{len(selected_chat_ids)}**).\nВыберите тип оповещения:",
        reply_markup=kb,
        parse_mode="Markdown"
    )


@dp.callback_query(IncidentState.waiting_for_type, F.data.startswith("type_"))
async def process_type(callback: types.CallbackQuery, state: FSMContext):
    msg_type = callback.data.split("_")[1]

    if msg_type == "custom":
        await state.set_state(IncidentState.waiting_for_custom_text_ru)
        await callback.message.edit_text(
            "✏️ Введите текст на **русском** (уйдёт в чаты с языком RU, отправляется 1 в 1):",
            parse_mode="Markdown",
            reply_markup=cancel_only_keyboard()
        )
        return

    user_data = await state.get_data()
    await send_alert(
        target_msg=callback.message,
        currency=user_data["selected_currency"],
        provider=user_data["provider"],
        target_chats=user_data["target_chats"],
        selected_chat_ids=user_data["selected_chat_ids"],
        template_key=msg_type
    )
    await state.clear()


@dp.message(IncidentState.waiting_for_custom_text_ru, F.chat.type == "private")
async def process_custom_text_ru(message: types.Message, state: FSMContext):
    if is_reserved_input(message.text):
        await state.clear()
        return await message.answer(
            "⚠️ Действие отменено (получена кнопка меню или команда вместо текста).\n"
            "Если это была команда - отправьте её ещё раз, теперь она сработает.",
            reply_markup=main_keyboard()
        )

    await state.update_data(custom_text_ru=message.text)
    user_data = await state.get_data()

    target_chats = user_data["target_chats"]
    selected_chat_ids = [str(x) for x in user_data["selected_chat_ids"]]
    has_en_chat = any(target_chats.get(cid, {}).get("lang", "RU") == "EN" for cid in selected_chat_ids)

    if not has_en_chat:
        # среди выбранных чатов нет ни одного EN — не отвлекаем вторым вопросом
        await send_alert(
            target_msg=message,
            currency=user_data["selected_currency"],
            provider=user_data["provider"],
            target_chats=target_chats,
            selected_chat_ids=selected_chat_ids,
            custom_texts={"RU": message.text, "EN": message.text}
        )
        await state.clear()
        return

    await state.set_state(IncidentState.waiting_for_custom_text_en)
    await message.answer(
        "✏️ Теперь введите текст на **английском** (уйдёт в чаты с языком EN, отправляется 1 в 1):",
        parse_mode="Markdown",
        reply_markup=cancel_only_keyboard()
    )


@dp.message(IncidentState.waiting_for_custom_text_en, F.chat.type == "private")
async def process_custom_text_en(message: types.Message, state: FSMContext):
    if is_reserved_input(message.text):
        await state.clear()
        return await message.answer(
            "⚠️ Действие отменено (получена кнопка меню или команда вместо текста).\n"
            "Если это была команда - отправьте её ещё раз, теперь она сработает.",
            reply_markup=main_keyboard()
        )

    user_data = await state.get_data()
    await send_alert(
        target_msg=message,
        currency=user_data["selected_currency"],
        provider=user_data["provider"],
        target_chats=user_data["target_chats"],
        selected_chat_ids=user_data["selected_chat_ids"],
        custom_texts={"RU": user_data["custom_text_ru"], "EN": message.text}
    )
    await state.clear()


async def safe_send(chat_id, thread_id, text, reply_to_message_id=None, retries=2):
    """Отправка с ретраем на TelegramRetryAfter (flood control)."""
    attempt = 0
    while True:
        try:
            return await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=text,
                reply_to_message_id=reply_to_message_id,
                parse_mode="Markdown"
            )
        except TelegramRetryAfter as e:
            attempt += 1
            if attempt > retries:
                raise
            await asyncio.sleep(e.retry_after + 0.5)


async def send_with_thread_fallback(chat_id, thread_id, text, reply_to_message_id=None):
    """Отправляет сообщение; если Telegram отвечает "ветка не найдена" (топик удалён/закрыт),
    автоматически повторяет без привязки к ветке - сообщение уйдёт в General.
    Возвращает (msg, использованный_thread_id, произошёл_ли_fallback)."""
    try:
        msg = await safe_send(chat_id, thread_id, text, reply_to_message_id=reply_to_message_id)
        return msg, thread_id, False
    except Exception as e:
        thread_missing = thread_id is not None and "thread" in str(e).lower()
        if not thread_missing:
            raise
        msg = await safe_send(chat_id, None, text, reply_to_message_id=reply_to_message_id)
        return msg, None, True


async def send_alert(target_msg: types.Message, currency: str, provider: str, target_chats: dict, selected_chat_ids: list,
                     template_key: str = None, custom_texts: dict = None):
    data = load_data()
    sent_messages = []
    failed = []
    warnings = []
    selected_chat_ids_str = [str(x) for x in selected_chat_ids]

    for cid_str in selected_chat_ids_str:
        info = target_chats.get(cid_str, {})
        lang = info.get("lang", "RU")
        tags = info.get("tags", [])
        name = info.get("name", cid_str)

        chat_id = info.get("chat_id")
        thread_id = info.get("thread_id")

        if not chat_id:
            chat_id, thread_id = parse_chat_id(cid_str)

        if custom_texts:
            # берём версию под язык конкретного чата; если её нет - используем RU как запасной вариант
            raw_text = custom_texts.get(lang, custom_texts.get("RU", ""))
            alert_text = escape_md(raw_text)
        else:
            alert_text = TEMPLATES[template_key][lang].format(curr=escape_md(currency), provider=escape_md(provider))

        if tags:
            safe_tags = [escape_md(t) for t in tags]
            alert_text += f"\n\n📌 **cc:** {' '.join(safe_tags)}"

        fell_back_note = ""
        try:
            msg, used_thread_id, fell_back = await send_with_thread_fallback(chat_id, thread_id, alert_text)
            if fell_back:
                fell_back_note = "ветка недоступна, сообщение ушло в General"
            sent_messages.append({"chat_key": cid_str, "chat_id": chat_id, "thread_id": used_thread_id, "message_id": msg.message_id, "lang": lang})
        except Exception as e:
            err_str = str(e)
            log_error(f"[send_alert] {cid_str} ({name}): {err_str}")
            failed.append(f"• `{cid_str}` ({escape_md(name)}): {escape_md(err_str)}")
            continue
        if fell_back_note:
            warnings.append(f"• `{cid_str}` ({escape_md(name)}): {escape_md(fell_back_note)}")

    incident_id = data.get("next_incident_id", 1)
    data["next_incident_id"] = incident_id + 1
    data["active_incidents"].append({
        "id": incident_id,
        "currency": currency,
        "provider": provider,
        "messages": sent_messages
    })
    save_data(data)

    report = f"✅ Оповещение по **{escape_md(currency)} ({escape_md(provider)})** отправлено в {len(sent_messages)} чат(ов)!"
    if warnings:
        report += f"\n\n⚠️ **Автоматически перенаправлено в General ({len(warnings)}):**\n" + "\n".join(warnings)
    if failed:
        report += f"\n\n❌ **Не отправлено в {len(failed)} чат(ов):**\n" + "\n".join(failed)

    for chunk_start in range(0, len(report), 3500):
        await target_msg.answer(report[chunk_start:chunk_start + 3500], parse_mode="Markdown")


# ------------------- ХЕНДЛЕР 2: ВОССТАНОВЛЕНИЕ С ВЫБОРОМ ИНЦИДЕНТА -------------------
@dp.message(F.text == "✅ Зафиксировать восстановление", F.chat.type == "private")
async def resolve_incident_start(message: types.Message):
    if not is_authorized(message.from_user.id):
        return await message.answer("🛑 Введите пароль через /start")

    data = load_data()
    active = data.get("active_incidents", [])

    if not active:
        return await message.answer("🟢 На данный момент нет активных просадок.")

    buttons = []
    for inc in active:
        inc_id = inc["id"]
        curr = inc["currency"]
        prov = inc["provider"]
        chat_count = len(inc.get("messages", []))
        btn_text = f"{curr} — {prov} ({chat_count} чат)"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"resolveinc_{inc_id}")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons + [cancel_button_row()])
    await message.answer("Выберите конкретную просадку для восстановления:", reply_markup=kb)


@dp.callback_query(F.data.startswith("resolveinc_"))
async def resolve_incident_select_chats(callback: types.CallbackQuery, state: FSMContext):
    inc_id = int(callback.data.split("_")[1])
    data = load_data()

    incident = next((x for x in data.get("active_incidents", []) if x["id"] == inc_id), None)

    if not incident or not incident.get("messages"):
        return await callback.message.edit_text("❌ Ошибка: Инцидент не найден или уже закрыт.")

    active_incident_chats = {}
    for msg_info in incident["messages"]:
        cid_str = str(msg_info.get("chat_key", msg_info["chat_id"]))
        if cid_str in data["chats"]:
            chat_meta = data["chats"][cid_str]
        else:
            chat_meta = {"name": f"Чат {cid_str}", "lang": msg_info.get("lang", "RU")}

        active_incident_chats[cid_str] = chat_meta

    selected_resolve_ids = [str(x) for x in active_incident_chats.keys()]

    await state.update_data(
        resolve_inc_id=inc_id,
        resolve_target_chats=active_incident_chats,
        selected_resolve_ids=selected_resolve_ids
    )
    await state.set_state(ResolveState.selecting_resolve_chats)

    kb = build_chats_selection_keyboard(active_incident_chats, selected_resolve_ids, action_type="resolve")
    await callback.message.edit_text(
        f"Восстановление: **{incident['currency']}** (**{incident['provider']}**).\nОтметьте чаты, в которых нужно зафиксировать восстановление:",
        reply_markup=kb,
        parse_mode="Markdown"
    )


@dp.callback_query(ResolveState.selecting_resolve_chats, F.data.startswith("toggleresolve_"))
async def toggle_resolve_chat(callback: types.CallbackQuery, state: FSMContext):
    chat_id = str(callback.data.split("toggleresolve_")[1])
    user_data = await state.get_data()

    resolve_target_chats = user_data["resolve_target_chats"]
    selected_resolve_ids = [str(x) for x in user_data["selected_resolve_ids"]]

    if chat_id in selected_resolve_ids:
        selected_resolve_ids.remove(chat_id)
    else:
        selected_resolve_ids.append(chat_id)

    await state.update_data(selected_resolve_ids=selected_resolve_ids)

    kb = build_chats_selection_keyboard(resolve_target_chats, selected_resolve_ids, action_type="resolve")
    await callback.message.edit_reply_markup(reply_markup=kb)


@dp.callback_query(ResolveState.selecting_resolve_chats, F.data == "finish_resolve_done")
async def finish_resolve_process(callback: types.CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    inc_id = user_data["resolve_inc_id"]
    selected_resolve_ids = [str(x) for x in user_data["selected_resolve_ids"]]

    if not selected_resolve_ids:
        return await callback.answer("⚠️ Выберите хотя бы один чат для восстановления!", show_alert=True)

    data = load_data()
    incident = next((x for x in data.get("active_incidents", []) if x["id"] == inc_id), None)

    if not incident or not incident.get("messages"):
        await state.clear()
        return await callback.message.edit_text("❌ Ошибка: Инцидент уже закрыт.")

    resolved_count = 0
    failed = []
    warnings = []
    remaining_messages = []
    currency = incident["currency"]
    provider = incident["provider"]

    for item in incident["messages"]:
        cid_str = str(item.get("chat_key", item["chat_id"]))

        if cid_str in selected_resolve_ids:
            lang = item.get("lang", "RU")
            name = data.get("chats", {}).get(cid_str, {}).get("name", cid_str)
            tags = data.get("chats", {}).get(cid_str, {}).get("tags", [])

            chat_id = item.get("chat_id")
            thread_id = item.get("thread_id")
            if not chat_id:
                chat_id, thread_id = parse_chat_id(cid_str)

            resolve_text = TEMPLATES["resolve"][lang].format(curr=escape_md(currency), provider=escape_md(provider))
            if tags:
                safe_tags = [escape_md(t) for t in tags]
                resolve_text += f"\n\n📌 **cc:** {' '.join(safe_tags)}"

            try:
                _, _, fell_back = await send_with_thread_fallback(chat_id, thread_id, resolve_text, reply_to_message_id=item["message_id"])
                resolved_count += 1
                if fell_back:
                    warnings.append(f"• `{cid_str}` ({escape_md(name)}): ветка недоступна, ушло в General")
            except Exception as e:
                log_error(f"[resolve reply] {cid_str} ({name}): {e}")
                try:
                    _, _, fell_back = await send_with_thread_fallback(chat_id, thread_id, resolve_text)
                    resolved_count += 1
                    if fell_back:
                        warnings.append(f"• `{cid_str}` ({escape_md(name)}): ветка недоступна, ушло в General")
                except Exception as ex:
                    log_error(f"[resolve plain] {cid_str} ({name}): {ex}")
                    failed.append(f"• `{cid_str}` ({escape_md(name)}): {escape_md(str(ex))}")
                    # чат не восстановлен - оставляем его в активном инциденте
                    remaining_messages.append(item)
                    continue
        else:
            remaining_messages.append(item)

    if remaining_messages:
        incident["messages"] = remaining_messages
        status_msg = f"🟢 Восстановление по **{escape_md(currency)} ({escape_md(provider)})** зафиксировано в {resolved_count} чат(ах).\n⚠️ Осталось чатов в этой просадке: **{len(remaining_messages)}**."
    else:
        data["active_incidents"] = [x for x in data["active_incidents"] if x["id"] != inc_id]
        status_msg = f"🟢 Просадка по **{escape_md(currency)} ({escape_md(provider)})** полностью закрыта!"

    if failed:
        status_msg += f"\n\n❌ **Не удалось отправить восстановление в {len(failed)} чат(ов):**\n" + "\n".join(failed)
    if warnings:
        status_msg += f"\n\n⚠️ **Автоматически перенаправлено в General ({len(warnings)}):**\n" + "\n".join(warnings)

    save_data(data)
    await state.clear()

    for chunk_start in range(0, len(status_msg), 3500):
        if chunk_start == 0:
            await callback.message.edit_text(status_msg[:3500], parse_mode="Markdown")
        else:
            await callback.message.answer(status_msg[chunk_start:chunk_start + 3500], parse_mode="Markdown")


# ------------------- ХЕНДЛЕР 3: ПОКАЗАТЬ ИМЕЮЩИЕСЯ ПРОСАДКИ -------------------
@dp.message(F.text == "📊 Показать имеющиеся просадки", F.chat.type == "private")
async def show_incidents(message: types.Message):
    if not is_authorized(message.from_user.id):
        return await message.answer("🛑 Введите пароль через /start")

    data = load_data()
    active = data.get("active_incidents", [])

    if not active:
        return await message.answer("🟢 **Все системы работают штатно.** Активных просадок нет.", parse_mode="Markdown")

    text = "🔴 **Активные просадки в данный момент:**\n\n"
    for inc in active:
        msg_count = len(inc.get("messages", []))
        text += f"• `id {inc['id']}` **{escape_md(inc['currency'])}** ({escape_md(inc['provider'])}) — активна в {msg_count} чат(ах)\n"

    buttons = [
        [InlineKeyboardButton(
            text=f"🗑 Удалить id {inc['id']} ({inc['currency']} · {inc['provider']}) без уведомления",
            callback_data=f"silentremove_{inc['id']}"
        )]
        for inc in active
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("silentremove_"))
async def silent_remove_incident(callback: types.CallbackQuery):
    """Убирает инцидент целиком из active_incidents БЕЗ отправки сообщения о восстановлении -
    для случаев, когда мерчантов уже оповестили по другому каналу, но фиксировать это в боте не нужно."""
    if not is_authorized(callback.from_user.id):
        return await callback.answer("🛑 Нет доступа", show_alert=True)

    inc_id = int(callback.data.split("_")[1])
    data = load_data()
    incident = next((x for x in data.get("active_incidents", []) if x["id"] == inc_id), None)

    if not incident:
        return await callback.answer("Уже закрыт или не найден", show_alert=True)

    data["active_incidents"] = [x for x in data["active_incidents"] if x["id"] != inc_id]
    save_data(data)

    await callback.answer("Удалено без уведомления чатов")
    await callback.message.edit_text(
        f"🗑 Просадка **{escape_md(incident['currency'])} ({escape_md(incident['provider'])})** удалена из списка активных.\n"
        f"Сообщения о восстановлении никуда не отправлялись.",
        parse_mode="Markdown"
    )


# ------------------- ЗАПУСК -------------------
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    print("Старые обновления сброшены. Бот готов к работе!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
