import asyncio
import json
import os
import sys
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, \
    ReplyKeyboardRemove
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
        "RU": "✅ **[{curr}] ({provider})** Коллеги, сервис работает в штатном режиме. Технические работы/просадка завершены.",
        "EN": "✅ **[{curr}] ({provider})** Colleagues, the service is fully operational. Maintenance/degradation resolved."
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
    print(f"\n🎯 НАСТОЯЩИЙ ID ЧАТА '{message.chat.title}': {message.chat.id}\n")


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

        chat_id = str(int(raw_args[0].strip()))
        currencies = [c.strip().upper() for c in raw_args[1].split(",")]

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
        data["chats"][chat_id] = {
            "name": merchant_name,
            "currencies": currencies,
            "lang": lang,
            "tags": tags,
            "is_active": True
        }
        save_data(data)

        tags_str = ", ".join(tags) if tags else "нет"
        await message.answer(
            f"✅ Чат `{chat_id}` (**{merchant_name}**) зарегистрирован!\n"
            f"• Валюты: **{', '.join(currencies)}**\n"
            f"• Язык общения: **{lang}**\n"
            f"• Теги ответственных: **{tags_str}**",
            parse_mode="Markdown"
        )
    except Exception:
        await message.answer(
            "⚠️ **Ошибка формата.**\nИспользуйте: `/add_chat <chat_id> <валюты> <RU/EN> <Имя_мерча> [теги]`\n"
            "*Пример без тегов:* `/add_chat -1005459446650 KGS RU PINCO`\n"
            "*Пример с тегами:* `/add_chat -1005459446650 KGS RU PINCO @alex,@john`",
            parse_mode="Markdown"
        )


@dp.message(Command("bulk_add"), F.chat.type == "private")
async def cmd_bulk_add_chats(message: types.Message):
    if not is_authorized(message.from_user.id):
        return await message.answer("🛑 Введите пароль через /start")

    raw_text = message.text.split(maxsplit=1)
    if len(raw_text) < 2:
        return await message.answer(
            "⚠️ **Формат массового импорта (без разделителей):**\n\n"
            "Отправьте команду `/bulk_add` и со следующей строки список чатов через пробел:\n\n"
            "```text\n"
            "/bulk_add\n"
            "-1005459446650 KGS,UZS RU PINCO @alex\n"
            "-1009876543210 EGP EN Mostbet @john,@kate\n"
            "-1001122334455 ARS RU 1xbet\n"
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
            chat_id = str(int(parts[0]))
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

            data["chats"][chat_id] = {
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
        chat_id = str(int(raw_args[0].strip()))

        data = load_data()
        if chat_id in data["chats"]:
            name = data["chats"][chat_id].get("name", "Без названия")
            del data["chats"][chat_id]
            save_data(data)
            await message.answer(f"🗑 Чат `{chat_id}` ({name}) успешно удален из базы.", parse_mode="Markdown")
        else:
            await message.answer(f"❌ Чат с ID `{chat_id}` не найден в базе.", parse_mode="Markdown")
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
        tags_str = f" | cc: {' '.join(tags)}" if tags else ""
        text += f"• `{cid}` | **{info['name']}** | [{info.get('lang', 'RU')}] | Валюты: {', '.join(info['currencies'])}{tags_str}\n"

    await message.answer(text, parse_mode="Markdown")


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
        parse_mode="Markdown"
    )


@dp.message(IncidentState.waiting_for_provider, F.chat.type == "private")
async def process_provider(message: types.Message, state: FSMContext):
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
        f"Валюта: **{currency}** | Источник: **{provider}**.\nОтметьте чаты, в которые нужно отправить сообщение:",
        reply_markup=kb,
        parse_mode="Markdown"
    )


@dp.callback_query(IncidentState.selecting_chats, F.data.startswith("togglechat_"))
async def toggle_chat_selection(callback: types.CallbackQuery, state: FSMContext):
    chat_id = str(callback.data.split("_")[1])
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
        [InlineKeyboardButton(text="✏️ Ввести свой текст", callback_data="type_custom")]
    ])

    await callback.message.edit_text(
        f"Валюта: **{currency}** | Источник: **{provider}** (чатов: **{len(selected_chat_ids)}**).\nВыберите тип оповещения:",
        reply_markup=kb,
        parse_mode="Markdown"
    )


@dp.callback_query(IncidentState.waiting_for_type, F.data.startswith("type_"))
async def process_type(callback: types.CallbackQuery, state: FSMContext):
    msg_type = callback.data.split("_")[1]

    if msg_type == "custom":
        await state.set_state(IncidentState.waiting_for_custom_text)
        await callback.message.edit_text("Введите ваш произвольный текст сообщения (он будет отправлен 1 в 1):")
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


@dp.message(IncidentState.waiting_for_custom_text, F.chat.type == "private")
async def process_custom_text(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    await send_alert(
        target_msg=message,
        currency=user_data["selected_currency"],
        provider=user_data["provider"],
        target_chats=user_data["target_chats"],
        selected_chat_ids=user_data["selected_chat_ids"],
        custom_text=message.text
    )
    await state.clear()


async def send_alert(target_msg: types.Message, currency: str, provider: str, target_chats: dict, selected_chat_ids: list,
                     template_key: str = None, custom_text: str = None):
    data = load_data()
    sent_messages = []
    selected_chat_ids_str = [str(x) for x in selected_chat_ids]

    for cid in selected_chat_ids_str:
        info = target_chats.get(cid, {})
        lang = info.get("lang", "RU")
        tags = info.get("tags", [])

        # 1. Формируем тело сообщения
        if custom_text:
            alert_text = custom_text
        else:
            alert_text = TEMPLATES[template_key][lang].format(curr=currency, provider=provider)

        # 2. Обязательно добавляем теги ответственных (cc:) к ЛЮБОМУ типу сообщения
        if tags:
            alert_text += f"\n\n📌 **cc:** {' '.join(tags)}"

        try:
            msg = await bot.send_message(chat_id=int(cid), text=alert_text, parse_mode="Markdown")
            sent_messages.append({"chat_id": str(cid), "message_id": msg.message_id, "lang": lang})
        except Exception as e:
            print(f"Ошибка отправки в {cid}: {e}")

    incident_id = len(data["active_incidents"]) + 1
    data["active_incidents"].append({
        "id": incident_id,
        "currency": currency,
        "provider": provider,
        "messages": sent_messages
    })
    save_data(data)

    await target_msg.answer(
        f"✅ Оповещение по **{currency} ({provider})** отправлено в {len(sent_messages)} чат(ов)!",
        parse_mode="Markdown"
    )


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

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
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
        cid_str = str(msg_info["chat_id"])
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
    chat_id = str(callback.data.split("_")[1])
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
    remaining_messages = []
    currency = incident["currency"]
    provider = incident["provider"]

    for item in incident["messages"]:
        cid_str = str(item["chat_id"])

        if cid_str in selected_resolve_ids:
            lang = item.get("lang", "RU")
            tags = data.get("chats", {}).get(cid_str, {}).get("tags", [])

            resolve_text = TEMPLATES["resolve"][lang].format(curr=currency, provider=provider)
            if tags:
                resolve_text += f"\n\n📌 **cc:** {' '.join(tags)}"

            try:
                await bot.send_message(
                    chat_id=int(cid_str),
                    text=resolve_text,
                    reply_to_message_id=item["message_id"],
                    parse_mode="Markdown"
                )
                resolved_count += 1
            except Exception as e:
                print(f"Не удалось ответить ответом в чат {cid_str}: {e}")
                try:
                    await bot.send_message(chat_id=int(cid_str), text=resolve_text, parse_mode="Markdown")
                    resolved_count += 1
                except Exception as ex:
                    print(f"Сбой отправки в {cid_str}: {ex}")
        else:
            remaining_messages.append(item)

    if remaining_messages:
        incident["messages"] = remaining_messages
        status_msg = f"🟢 Восстановление по **{currency} ({provider})** зафиксировано в {resolved_count} чат(ах).\n⚠️ Осталось чатов в этой просадке: **{len(remaining_messages)}**."
    else:
        data["active_incidents"] = [x for x in data["active_incidents"] if x["id"] != inc_id]
        status_msg = f"🟢 Просадка по **{currency} ({provider})** полностью закрыта!"

    save_data(data)
    await state.clear()
    await callback.message.edit_text(status_msg, parse_mode="Markdown")


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
        text += f"• **{inc['currency']}** ({inc['provider']}) — активна в {msg_count} чат(ах)\n"

    await message.answer(text, parse_mode="Markdown")


# ------------------- ЗАПУСК -------------------
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    print("Старые обновления сброшены. Бот готов к работе!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())