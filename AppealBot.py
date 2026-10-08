import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
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
# Базовый список кнопок валют. Поверх него кнопки автоматически добавляются для любых валют, которые указаны
# у заведённых мерчантов (см. get_currency_list), так что новую валюту можно завести просто через /add_chat.
CURRENCIES = ["EGP", "ARS", "UZS", "AUD", "AZN", "KGS", "MNT",
              "INR", "PKR", "AED", "TRY", "MAD", "JOD", "USD", "TND", "DZD", "KZT"]
CURRENCY_RE = re.compile(r"[A-Z]{3}")  # код валюты - ровно 3 латинские буквы

# Для этих валют после выбора валюты обязательно выбирается метод - он попадает в текст оповещения мерчантам.
CURRENCY_METHODS = {
    "EGP": [("instapay", "InstaPay"), ("orange", "Orange Cash"), ("vodafone", "Vodafone Cash")],
}

# Смещение для отображения времени в "🔍 Подробнее" (по умолчанию МСК, UTC+3). Можно задать TZ_OFFSET_HOURS в .env
try:
    TZ_OFFSET_HOURS = float(os.getenv("TZ_OFFSET_HOURS", "3"))
except ValueError:
    TZ_OFFSET_HOURS = 3.0
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))

# ------------------- КАНАЛЫ (ПРИЁМНЫЙ / ВЫПЛАТНОЙ) -------------------
CHANNEL_DEPOSIT = "deposit"
CHANNEL_PAYOUT = "payout"
CHANNEL_ORDER = [CHANNEL_DEPOSIT, CHANNEL_PAYOUT]

CHANNEL_BUTTON_LABELS = {
    CHANNEL_DEPOSIT: "📥 Приёмный канал",
    CHANNEL_PAYOUT: "📤 Выплатной канал",
}
CHANNEL_ICONS = {CHANNEL_DEPOSIT: "📥", CHANNEL_PAYOUT: "📤"}
CHANNEL_ADMIN_NAMES = {CHANNEL_DEPOSIT: "📥 Приёмный", CHANNEL_PAYOUT: "📤 Выплатной"}

# Фразы, которые подставляются в тексты для мерчантов. Ключ - какие каналы затронуты:
# deposit / payout / both / none (none - запасной вариант, если каналы не заданы).
CHANNEL_PHRASES = {
    "RU": {
        "on": {
            "deposit": "по приёмному каналу",
            "payout": "по выплатному каналу",
            "both": "по приёмному и выплатному каналам",
            "none": "по данной валюте",
        },
        "affecting": {
            "deposit": "по приёмному каналу",
            "payout": "по выплатному каналу",
            "both": "по приёмному и выплатному каналам",
            "none": "по данной валюте",
        },
        "restored": {
            "deposit": "приёмный канал восстановлен",
            "payout": "выплатной канал восстановлен",
            "both": "приёмный и выплатной каналы восстановлены",
            "none": "сервис восстановлен",
        },
        # фраза про НЕзатронутый канал - чтобы мерчант не отключал всё подряд (только когда затронут один канал)
        "unaffected": {
            "deposit": " По выплатному каналу ограничений нет.",
            "payout": " По приёмному каналу ограничений нет.",
            "both": "",
            "none": "",
        },
    },
    "EN": {
        "on": {
            "deposit": "on the deposit channel",
            "payout": "on the payout channel",
            "both": "on the deposit and payout channels",
            "none": "for this currency",
        },
        "affecting": {
            "deposit": "affecting the deposit channel",
            "payout": "affecting the payout channel",
            "both": "affecting the deposit and payout channels",
            "none": "affecting this currency",
        },
        "restored": {
            "deposit": "the deposit channel has been restored",
            "payout": "the payout channel has been restored",
            "both": "the deposit and payout channels have been restored",
            "none": "the service has been restored",
        },
        "unaffected": {
            "deposit": " There are no restrictions on the payout channel.",
            "payout": " There are no restrictions on the deposit channel.",
            "both": "",
            "none": "",
        },
    },
}

CHANNEL_PHRASES["RU"]["nom"] = {
    "deposit": "приёмный канал",
    "payout": "выплатной канал",
    "both": "приёмный и выплатной каналы",
    "none": "сервис",
}
CHANNEL_PHRASES["EN"]["nom"] = {
    "deposit": "the deposit channel",
    "payout": "the payout channel",
    "both": "the deposit and payout channels",
    "none": "the service",
}

# ------------------- СЛОВАРЬ ШАБЛОНОВ (RU / EN) -------------------
# Плейсхолдеры: {curr}, {provider_part} (банк в скобках или пусто), {affecting}/{channels_on} (каналы),
# {unaffected} (фраза про незатронутый канал), {restored}/{remaining_on} (для восстановления).
TEMPLATES = {
    "std": {
        "RU": "⚠️ **[{curr}]{provider_part}** Коллеги, на стороне банка наблюдаются технические трудности {affecting}, из-за чего могут происходить отмены и задержки платежей. На нашей стороне всё работает штатно, трафик приостанавливать не требуется. Мы сообщим вам о восстановлении.",
        "EN": "⚠️ **[{curr}]{provider_part}** Colleagues, technical issues are currently observed on the bank's side {affecting}, which may cause failed transactions and delays. Systems on our side are operating normally; traffic does not need to be stopped. We'll let you know once restored."
    },
    "stop": {
        "RU": "⚠️ **[{curr}]{provider_part}** Коллеги, на стороне банка ведутся технические работы {affecting}, в связи с чем могут быть отмены и снижение конверсии.\n🛑 **Просим временно остановить трафик {channels_on}.**{unaffected}\nМы сообщим вам о восстановлении.",
        "EN": "⚠️ **[{curr}]{provider_part}** Colleagues, technical maintenance is undergoing on the bank's side {affecting}, which may result in higher failure rates and lower conversion.\n🛑 **Please temporarily stop processing traffic {channels_on}.**{unaffected}\nWe will let you know once restored."
    },
    "short_std": {
        "RU": "⚠️ **[{curr}]{provider_part}** Коллеги, в данный момент наблюдаются временные технические трудности {affecting}, из-за чего возможны отмены и задержки платежей. Трафик приостанавливать не требуется. О восстановлении сообщим дополнительно.",
        "EN": "⚠️ **[{curr}]{provider_part}** Colleagues, we are currently experiencing temporary technical issues {affecting}, which may cause failed transactions and delays. Traffic does not need to be paused. We will notify you once resolved."
    },
    "short_stop": {
        "RU": "⚠️ **[{curr}]{provider_part}** Коллеги, в данный момент наблюдаются временные технические трудности {affecting}, в связи с чем возможны отмены платежей и снижение конверсии.\n🛑 **Просим временно приостановить трафик {channels_on}.**{unaffected}\nО восстановлении сообщим дополнительно.",
        "EN": "⚠️ **[{curr}]{provider_part}** Colleagues, we are currently experiencing temporary technical issues {affecting}, which may result in failed transactions and lower conversion.\n🛑 **Please temporarily pause traffic {channels_on}.**{unaffected}\nWe will notify you once resolved."
    },
    # восстановление без каналов (старые просадки, созданные до появления каналов)
    "resolve": {
        "RU": "✅ **[{curr}]{provider_part}** Коллеги, сервис работает в штатном режиме. Технические работы завершены.",
        "EN": "✅ **[{curr}]{provider_part}** Colleagues, the service is fully operational. Maintenance resolved."
    },
    # восстановление: все затронутые каналы восстановлены
    "resolve_channels": {
        "RU": "✅ **[{curr}]{provider_part}** Коллеги, {restored}, сервис работает в штатном режиме. Технические работы завершены.",
        "EN": "✅ **[{curr}]{provider_part}** Colleagues, {restored}, and the service is fully operational. Maintenance resolved."
    },
    # восстановление: вернулся один канал, по второму ограничения сохраняются
    "resolve_partial": {
        "RU": "✅ **[{curr}]{provider_part}** Коллеги, {restored}.\n⚠️ Ограничения {remaining_on} сохраняются, о восстановлении сообщим дополнительно.",
        "EN": "✅ **[{curr}]{provider_part}** Colleagues, {restored}.\n⚠️ Restrictions {remaining_on} remain in place; we will notify you once resolved."
    },
}

TEMPLATES["resolve_methods"] = {
    "RU": "✅ **[{curr}]{provider_part}** Коллеги, работа {restored} восстановлена, трафик можно возобновить.",
    "EN": "✅ **[{curr}]{provider_part}** Colleagues, operations {restored} have been restored, traffic can be resumed.",
}
TEMPLATES["resolve_methods_partial"] = {
    "RU": ("✅ **[{curr}]{provider_part}** Коллеги, работа {restored} восстановлена, трафик можно возобновить.\n"
           "⚠️ Ограничения сохраняются: {remaining}. О восстановлении сообщим дополнительно."),
    "EN": ("✅ **[{curr}]{provider_part}** Colleagues, operations {restored} have been restored, traffic can be resumed.\n"
           "⚠️ Restrictions remain in place: {remaining}. We will notify you once resolved."),
}

TYPE_LABELS = {
    "std": "⚠️ Стандарт (проблема банка)",
    "stop": "🛑 Стандарт (банк) + СТОП трафик",
    "short_std": "⚠️ Стандарт (временные трудности)",
    "short_stop": "🛑 Стандарт (временные трудности) + СТОП",
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


def provider_part(provider: str) -> str:
    """Для текста, уходящего мерчантам: ' (Provider)' если провайдер указан, иначе пусто -
    чтобы не оставались пустые скобки '()' в сообщении, если шаг с провайдером пропустили."""
    p = (provider or "").strip()
    return f" ({escape_md(p)})" if p else ""


def provider_display(provider: str) -> str:
    """Для служебных сообщений/отчётов бота: показываем название или пометку, что не указан."""
    p = (provider or "").strip()
    return escape_md(p) if p else "не указан"


def provider_paren_display(provider: str) -> str:
    """То же самое, но в скобках, для мест вида 'CURR (Provider)' в отчётах - пустых скобок не будет."""
    p = (provider or "").strip()
    return f" ({escape_md(p)})" if p else ""


def normalize_channels(channels):
    """Приводит список каналов к каноничному виду (только известные, в фиксированном порядке)."""
    return [c for c in CHANNEL_ORDER if c in (channels or [])]


def channel_phrase(lang: str, kind: str, channels) -> str:
    ch = normalize_channels(channels)
    key = "both" if len(ch) >= 2 else (ch[0] if ch else "none")
    table = CHANNEL_PHRASES.get(lang, CHANNEL_PHRASES["RU"])
    return table[kind][key]


def normalize_methods(methods, currency=None) -> list:
    """Строка / список / None -> список названий методов (для валюты с известными методами - в порядке из CURRENCY_METHODS)."""
    if not methods:
        return []
    if isinstance(methods, str):
        methods = [m.strip() for m in methods.split(",") if m.strip()]
    methods = list(dict.fromkeys(methods))
    order = [name for _, name in CURRENCY_METHODS.get(currency, [])] if currency else []
    if order:
        methods = [m for m in order if m in methods] + [m for m in methods if m not in order]
    return methods


def curr_display(currency: str, methods="") -> str:
    """'EGP' или 'EGP · InstaPay' или 'EGP · InstaPay, Vodafone Cash' (без экранирования - для заголовков и подписей)."""
    ms = normalize_methods(methods, currency)
    return f"{currency} · {', '.join(ms)}" if ms else currency


def method_unaffected(lang: str, currency: str, methods) -> str:
    """Фраза про остальные методы валюты - чтобы мерчант не останавливал трафик по всей валюте.
    Если затронуты ВСЕ методы валюты, фраза не нужна."""
    ms = normalize_methods(methods, currency)
    if not ms:
        return ""
    all_names = [name for _, name in CURRENCY_METHODS.get(currency, [])]
    if all_names and set(all_names) <= set(ms):
        return ""
    if lang == "EN":
        return f" There are no restrictions on other {currency} methods."
    return f" По остальным методам {currency} ограничений нет."


def render_template(key: str, lang: str, currency: str, provider: str, channels=None, restored=None, remaining=None,
                    methods=None) -> str:
    """Собирает текст для мерчанта из шаблона: валюта (и методы), банк в скобках (если указан), каналы."""
    lang = lang if lang in ("RU", "EN") else "RU"
    unaffected = channel_phrase(lang, "unaffected", channels) + method_unaffected(lang, currency, methods)
    return TEMPLATES[key][lang].format(
        curr=escape_md(curr_display(currency, methods)),
        provider_part=provider_part(provider),
        channels_on=channel_phrase(lang, "on", channels),
        affecting=channel_phrase(lang, "affecting", channels),
        unaffected=unaffected,
        restored=channel_phrase(lang, "restored", restored),
        remaining_on=channel_phrase(lang, "on", remaining),
    )


def render_method_resolve(lang: str, currency: str, provider: str, restored_pairs: dict, remaining_pairs: dict) -> str:
    """Восстановление по методам: 'по приёмному каналу (InstaPay); ...' - сколько методов вернулось и что ещё ограничено."""
    lang = lang if lang in ("RU", "EN") else "RU"
    key = "resolve_methods_partial" if remaining_pairs else "resolve_methods"
    return TEMPLATES[key][lang].format(
        curr=escape_md(currency),
        provider_part=provider_part(provider),
        restored=outage_group_phrase(lang, restored_pairs, "on"),
        remaining=outage_group_phrase(lang, remaining_pairs, "nom") if remaining_pairs else "",
    )


def pairs_keys(incident: dict, selected_ids=None, order=None) -> list:
    """Ключи (валюты сбоя или методы), по которым ещё есть ограничения. Порядок - как в order, остальные по алфавиту."""
    found = set()
    for m in incident.get("messages", []):
        cid = str(m.get("chat_key", m.get("chat_id")))
        if selected_ids is not None and cid not in selected_ids:
            continue
        found.update((m.get("pairs") or {}).keys())
    order = order or []
    return [k for k in order if k in found] + sorted(k for k in found if k not in order)


def has_method_pairs(incident: dict) -> bool:
    """Обычная просадка по валюте с методами (EGP), у чатов которой ограничения хранятся по методам."""
    return incident.get("kind") != "outage" and any(m.get("pairs") for m in incident.get("messages", []))


def incident_methods(incident: dict) -> list:
    """Методы просадки: новые просадки хранят список 'methods', старые - строку 'method'."""
    return normalize_methods(incident.get("methods") or incident.get("method"), incident.get("currency"))


def incident_currency_label(incident: dict) -> str:
    """Как показывать валюту просадки админу: 'UZS', 'EGP · InstaPay, Vodafone Cash' или (сбой на площадке) 'UZS, KGS'."""
    if incident.get("kind") == "outage":
        return incident.get("currency") or ", ".join(incident.get("currencies", []))
    currency = incident.get("currency", "")
    if has_method_pairs(incident):
        # показываем только методы, по которым ограничения ещё действуют
        order = [name for _, name in CURRENCY_METHODS.get(currency, [])]
        return curr_display(currency, pairs_keys(incident, None, order))
    return curr_display(currency, incident_methods(incident))


def get_currency_list(data=None) -> list:
    """Кнопки валют: базовый список + любые валюты, которые указаны у мерчантов (новые - в конце по алфавиту)."""
    data = data if data is not None else load_data()
    extras = set()
    for info in data.get("chats", {}).values():
        for c in info.get("currencies", []):
            c = (c or "").strip().upper()
            if CURRENCY_RE.fullmatch(c) and c not in CURRENCIES:
                extras.add(c)
    return list(CURRENCIES) + sorted(extras)


def active_channels(incident: dict) -> list:
    """Каналы, по которым просадка ещё активна (объединение по всем чатам просадки)."""
    found = set()
    for m in incident.get("messages", []):
        found.update(m.get("channels") or [])
        for chs in (m.get("pairs") or {}).values():
            found.update(chs)
    return normalize_channels(found)


def channels_of_selected(incident: dict, selected_ids) -> list:
    found = set()
    for m in incident.get("messages", []):
        cid = str(m.get("chat_key", m.get("chat_id")))
        if cid in selected_ids:
            found.update(m.get("channels") or [])
    return normalize_channels(found)


def channel_icons(channels) -> str:
    return "".join(CHANNEL_ICONS[c] for c in normalize_channels(channels))


def channel_names_admin(channels) -> str:
    return ", ".join(CHANNEL_ADMIN_NAMES[c] for c in normalize_channels(channels))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_local_time(iso_str):
    """ISO-время (UTC) -> '29.09.2026 05:12 (UTC+3)'. Если времени нет или оно битое - None."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(LOCAL_TZ)
    except Exception:
        return None
    sign = "+" if TZ_OFFSET_HOURS >= 0 else "-"
    return local.strftime("%d.%m.%Y %H:%M") + f" (UTC{sign}{abs(TZ_OFFSET_HOURS):g})"


def notified_merchants(incident: dict, chats_registry: dict) -> list:
    """Названия мерчантов, которых оповестили. Для старых просадок берём из текущих чатов."""
    names = incident.get("notified_names")
    if names:
        return list(names)
    result = []
    for m in incident.get("messages", []):
        name = m.get("name")
        if not name:
            cid = str(m.get("chat_key", m.get("chat_id")))
            name = chats_registry.get(cid, {}).get("name", cid)
        result.append(name)
    return result


def build_incident_details(incident: dict, chats_registry: dict) -> str:
    """Текст карточки '🔍 Подробнее': название, валюта/метод, каналы, время, мерчанты, тип и сам текст оповещения."""
    is_outage = incident.get("kind") == "outage"
    is_methods = has_method_pairs(incident)
    label = incident.get("label") or incident.get("provider") or "без названия"
    provider = (incident.get("provider") or "").strip()
    provider_shown = escape_md(provider) if provider else "ничего (без скобок)"
    type_label = incident.get("type_label", "неизвестно (создана старой версией бота)")
    created = format_local_time(incident.get("created_at")) or "неизвестно (создана старой версией бота)"
    initial_ch = normalize_channels(incident.get("channels"))
    now_ch = active_channels(incident)

    lines = [
        f"🔍 **Подробности просадки id {incident['id']}**",
        "",
        f"Название: **{escape_md(label)}**",
    ]
    if is_outage:
        lines.append("Вид: **🛠 Сбой на площадке**")
        lines.append(f"Валюты при создании: **{escape_md(', '.join(incident.get('currencies', [])))}**")
    else:
        lines.append(f"Валюта: **{escape_md(incident_currency_label(incident))}**")
        if is_methods:
            lines.append(f"Методы при создании: **{escape_md(', '.join(incident_methods(incident)))}**")
    if initial_ch:
        if now_ch == initial_ch:
            lines.append(f"Каналы: **{channel_names_admin(now_ch)}**")
        else:
            lines.append(f"Каналы сейчас: **{channel_names_admin(now_ch) or '—'}** (изначально: {channel_names_admin(initial_ch)})")
    if not is_outage:
        lines.append(f"Мерчантам показано: **{provider_shown}**")
    lines += [
        f"Тип оповещения: **{escape_md(type_label)}**",
        f"Создана: **{created}**",
    ]

    names = notified_merchants(incident, chats_registry)
    limit = 30
    shown = ", ".join(escape_md(n) for n in names[:limit])
    if len(names) > limit:
        shown += f" и ещё {len(names) - limit}"
    lines.append(f"Оповещены ({len(names)}): {shown if shown else '—'}")
    lines.append(f"Сейчас активна в: **{len(incident.get('messages', []))}** чат(ах)")

    if is_methods:
        lines.append("Активно сейчас по методам:")
        order = [name for _, name in CURRENCY_METHODS.get(incident.get("currency"), [])]
        for method in pairs_keys(incident, None, order):
            chats_n = 0
            chans = set()
            for m in incident.get("messages", []):
                pairs = m.get("pairs") or {}
                if method in pairs:
                    chats_n += 1
                    chans.update(pairs[method])
            lines.append(f"• {method} — {chats_n} чат(ах) {channel_icons(chans)}")

    if is_outage:
        lines.append("Активно сейчас по валютам:")
        for cur in outage_active_currencies(incident):
            chats_n = 0
            chans = set()
            for m in incident.get("messages", []):
                pairs = m.get("pairs") or {}
                if cur in pairs:
                    chats_n += 1
                    chans.update(pairs[cur])
            lines.append(f"• {cur} — {chats_n} чат(ах) {channel_icons(chans)}")

    text_ru = incident.get("text_ru")
    text_en = incident.get("text_en")
    sample_note = " (пример - у каждого мерчанта свой список валют)" if is_outage else ""
    details = "\n".join(lines)
    if text_ru:
        details += f"\n\n**Текст (RU){sample_note}:**\n{text_ru}"
    if text_en and text_en != text_ru:
        details += f"\n\n**Текст (EN){sample_note}:**\n{text_en}"
    if not text_ru and not text_en:
        details += "\n\n_Текст не сохранён - просадка создана до этой функции._"
    return details


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
    selecting_method = State()
    waiting_for_label = State()
    waiting_for_provider = State()
    selecting_chats = State()
    waiting_for_type = State()
    selecting_channels = State()
    waiting_for_custom_text_ru = State()
    waiting_for_custom_text_en = State()


class ResolveState(StatesGroup):
    selecting_resolve_chats = State()
    selecting_resolve_channels = State()
    selecting_outage_currencies = State()
    selecting_outage_channels = State()
    selecting_methods = State()
    selecting_method_channels = State()


class OutageState(StatesGroup):
    selecting_currencies = State()
    selecting_chats = State()
    selecting_channels = State()
    waiting_for_type = State()
    waiting_for_custom_ru = State()
    waiting_for_custom_en = State()
    confirming = State()


# ------------------- КЛАВИАТУРЫ -------------------
MENU_BUTTON_TEXTS = {
    "🚨 Оповестить о просадке",
    "🛠 Сбой на площадке",
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
        [KeyboardButton(text="🛠 Сбой на площадке")],
        [KeyboardButton(text="✅ Зафиксировать восстановление")],
        [KeyboardButton(text="📊 Показать имеющиеся просадки")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, selective=True)


def cancel_button_row():
    return [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow")]


def currencies_keyboard(currencies=None):
    currencies = currencies or get_currency_list()
    buttons = [InlineKeyboardButton(text=curr, callback_data=f"curr_{curr}") for curr in currencies]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]  # по 3 кнопки в ряд
    rows.append(cancel_button_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


def build_channels_keyboard(selected, available=None, mode="alert"):
    """Кнопки выбора каналов. mode='alert' - при создании оповещения, mode='resolve' - при восстановлении."""
    selected = set(normalize_channels(selected))
    shown = normalize_channels(available) if available else list(CHANNEL_ORDER)
    toggle_prefix = "togglech_" if mode == "alert" else "toggleresch_"

    rows = []
    for ch in shown:
        icon = "✅" if ch in selected else "❌"
        rows.append([InlineKeyboardButton(text=f"{icon} {CHANNEL_BUTTON_LABELS[ch]}", callback_data=f"{toggle_prefix}{ch}")])

    if mode == "alert":
        rows.append([InlineKeyboardButton(text="➡️ ПРОДОЛЖИТЬ", callback_data="channels_done")])
    else:
        rows.append([InlineKeyboardButton(text="➡️ ПОДТВЕРДИТЬ ВЫБРАННОЕ", callback_data="resch_done")])
        rows.append([InlineKeyboardButton(text="✅ Всё восстановилось", callback_data="resch_all")])
    rows.append(cancel_button_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


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

        bad_codes = [c for c in currencies if not CURRENCY_RE.fullmatch(c)]
        if not currencies or bad_codes:
            return await message.answer(
                f"⚠️ Некорректный код валюты: `{', '.join(bad_codes) if bad_codes else raw_args[1]}`\n"
                "Код валюты — ровно 3 латинские буквы (например `UZS`). Несколько валют — через запятую без пробелов: `UZS,KGS`.",
                parse_mode="Markdown"
            )

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
        known_currencies = set(get_currency_list(data))  # какие кнопки валют были до этого мерчанта
        new_currencies = [c for c in currencies if c not in known_currencies]
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
        answer_text = (
            f"✅ Чат `{chat_key}`{thread_info} (**{escape_md(merchant_name)}**) зарегистрирован!\n"
            f"• Валюты: **{', '.join(currencies)}**\n"
            f"• Язык общения: **{lang}**\n"
            f"• Теги ответственных: **{tags_str}**"
        )
        if new_currencies:
            answer_text += (
                f"\n\n🆕 Новая валюта: **{', '.join(new_currencies)}** — кнопка появится автоматически. "
                "Если это опечатка, отправьте /add_chat для этого чата ещё раз с верным кодом."
            )
        await message.answer(answer_text, parse_mode="Markdown")
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
    known_currencies = set(get_currency_list(data))  # какие кнопки валют были до этой загрузки
    new_currencies = []
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

            bad_codes = [c for c in currencies if not CURRENCY_RE.fullmatch(c)]
            if not currencies or bad_codes:
                errors.append(f"Строка {idx}: некорректный код валюты (`{', '.join(bad_codes) if bad_codes else parts[1]}`) — нужно 3 латинские буквы")
                continue

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
            for c in currencies:
                if c not in known_currencies and c not in new_currencies:
                    new_currencies.append(c)
        except Exception:
            errors.append(f"Строка {idx}: ошибка формата ID (`{parts[0]}`)")

    save_data(data)

    report = f"🎉 **Успешно заведено чатов: {added_count}**\n"
    if new_currencies:
        report += f"\n🆕 Новые валюты (кнопки появятся автоматически): **{', '.join(new_currencies)}**\n"
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
            f"✅ Чат `{target_cid}` принудительно закрыт.\n🟢 Это был последний чат — просадка **{escape_md(incident_currency_label(incident))} ({escape_md(incident.get('label') or incident.get('provider') or 'без названия')})** полностью закрыта.",
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

    await state.update_data(selected_currency=currency, target_chats=target_chats, methods=[])

    if currency in CURRENCY_METHODS:
        # для этой валюты метод обязателен - спрашиваем сразу после выбора валюты (можно несколько)
        await state.set_state(IncidentState.selecting_method)
        await callback.message.edit_text(
            f"Валюта: **{currency}**.\nВыберите метод - можно отметить несколько (по умолчанию ничего не выбрано). "
            "Методы попадут в текст оповещения:",
            parse_mode="Markdown",
            reply_markup=build_methods_keyboard(currency, [])
        )
        return

    await ask_label(callback.message, state, currency, [])


def build_methods_keyboard(currency: str, selected):
    sel = set(normalize_methods(selected, currency))
    rows = [[InlineKeyboardButton(text=f"{'✅' if name in sel else '❌'} {name}", callback_data=f"methodtg_{key}")]
            for key, name in CURRENCY_METHODS[currency]]
    rows.append([InlineKeyboardButton(text="➡️ ПРОДОЛЖИТЬ", callback_data="method_done")])
    rows.append([InlineKeyboardButton(text="✅ Все методы (сразу дальше)", callback_data="method_all")])
    rows.append(cancel_button_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def ask_label(target_msg, state: FSMContext, currency: str, method: str):
    """Шаг 'как назовём просадку' (после выбора валюты и, если нужно, метода)."""
    await state.set_state(IncidentState.waiting_for_label)
    await target_msg.edit_text(
        f"Валюта: **{escape_md(curr_display(currency, method))}**.\nКак назовём эту просадку? (для внутренней истории, мерчанты этого не увидят):",
        parse_mode="Markdown",
        reply_markup=cancel_only_keyboard()
    )


@dp.callback_query(IncidentState.selecting_method, F.data.startswith("methodtg_"))
async def toggle_method(callback: types.CallbackQuery, state: FSMContext):
    key = callback.data[len("methodtg_"):]
    d = await state.get_data()
    currency = d["selected_currency"]
    name = next((n for k, n in CURRENCY_METHODS.get(currency, []) if k == key), None)
    if not name:
        return await callback.answer()
    selected = normalize_methods(d.get("methods", []), currency)
    if name in selected:
        selected.remove(name)
    else:
        selected.append(name)
    selected = normalize_methods(selected, currency)
    await state.update_data(methods=selected)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=build_methods_keyboard(currency, selected))


@dp.callback_query(IncidentState.selecting_method, F.data == "method_done")
async def methods_done(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    currency = d["selected_currency"]
    selected = normalize_methods(d.get("methods", []), currency)
    if not selected:
        return await callback.answer("⚠️ Выберите хотя бы один метод!", show_alert=True)
    await callback.answer()
    await ask_label(callback.message, state, currency, selected)


@dp.callback_query(IncidentState.selecting_method, F.data == "method_all")
async def methods_all(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    currency = d["selected_currency"]
    names = [n for _, n in CURRENCY_METHODS.get(currency, [])]
    await state.update_data(methods=names)
    await callback.answer()
    await ask_label(callback.message, state, currency, names)


@dp.message(IncidentState.waiting_for_label, F.chat.type == "private")
async def process_label(message: types.Message, state: FSMContext):
    if is_reserved_input(message.text):
        await state.clear()
        return await message.answer(
            "⚠️ Действие отменено (получена кнопка меню или команда вместо текста).\n"
            "Если это была команда - отправьте её ещё раз, теперь она сработает.",
            reply_markup=main_keyboard()
        )

    label = message.text.strip()
    if not label:
        return await message.answer(
            "⚠️ Название не может быть пустым. Введите текст ещё раз:",
            reply_markup=cancel_only_keyboard()
        )

    await state.update_data(label=label)
    await state.set_state(IncidentState.waiting_for_provider)

    provider_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Пропустить (не показывать мерчантам)", callback_data="skip_provider")],
        cancel_button_row()
    ])
    await message.answer(
        f"Просадка: **{escape_md(label)}**.\nЧто показать мерчантам в скобках? (например банк) - можно пропустить, тогда скобок в сообщении не будет вообще:",
        parse_mode="Markdown",
        reply_markup=provider_kb
    )


async def proceed_to_chat_selection(answer_target, state: FSMContext, provider: str):
    """Общий переход от 'указан/пропущен провайдер (для мерчантов)' к выбору чатов - используется и для
    текстового ввода, и для нажатия кнопки 'Пропустить'."""
    user_data = await state.get_data()
    target_chats = user_data["target_chats"]
    currency = curr_display(user_data["selected_currency"], user_data.get("methods", []))
    label = user_data["label"]
    selected_chat_ids = [str(cid) for cid in target_chats.keys()]

    await state.update_data(
        provider=provider,
        selected_chat_ids=selected_chat_ids
    )
    await state.set_state(IncidentState.selecting_chats)

    kb = build_chats_selection_keyboard(target_chats, selected_chat_ids, action_type="alert")
    provider_shown = escape_md(provider) if provider.strip() else "ничего (скобок не будет)"
    await answer_target.answer(
        f"Просадка: **{escape_md(label)}** | Валюта: **{escape_md(currency)}**\nМерчантам покажем: **{provider_shown}**\nОтметьте чаты, в которые нужно отправить сообщение:",
        reply_markup=kb,
        parse_mode="Markdown"
    )


@dp.callback_query(IncidentState.waiting_for_provider, F.data == "skip_provider")
async def skip_provider(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await proceed_to_chat_selection(callback.message, state, "")


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
    await proceed_to_chat_selection(message, state, provider)


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
    currency = curr_display(user_data["selected_currency"], user_data.get("methods", []))
    provider = user_data["provider"]
    label = user_data["label"]

    if not selected_chat_ids:
        return await callback.answer("⚠️ Выберите хотя бы один чат!", show_alert=True)

    await state.set_state(IncidentState.waiting_for_type)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Стандарт (проблема банка)", callback_data="type_std")],
        [InlineKeyboardButton(text="🛑 Стандарт (банк) + СТОП трафик", callback_data="type_stop")],
        [InlineKeyboardButton(text="⚠️ Стандарт (временные трудности)", callback_data="type_short_std")],
        [InlineKeyboardButton(text="🛑 Стандарт (временные трудности) + СТОП", callback_data="type_short_stop")],
        [InlineKeyboardButton(text="✏️ Ввести свой текст", callback_data="type_custom")],
        cancel_button_row()
    ])

    provider_shown = escape_md(provider) if provider.strip() else "ничего (скобок не будет)"
    await callback.message.edit_text(
        f"Просадка: **{escape_md(label)}** | Валюта: **{escape_md(currency)}**\nМерчантам покажем: **{provider_shown}**\nЧатов: **{len(selected_chat_ids)}**.\nВыберите тип оповещения:",
        reply_markup=kb,
        parse_mode="Markdown"
    )


@dp.callback_query(IncidentState.waiting_for_type, F.data.startswith("type_"))
async def process_type(callback: types.CallbackQuery, state: FSMContext):
    msg_type = callback.data[len("type_"):]  # всё после "type_" - чтобы не ломалось на "short_std"/"short_stop"

    # после выбора типа - выбор затронутых каналов (по умолчанию ничего не выбрано, нужно выбрать осознанно)
    await state.update_data(alert_type=msg_type, channels=[])
    await state.set_state(IncidentState.selecting_channels)
    await callback.answer()
    await callback.message.edit_text(
        "Какие каналы затронуты? Отметьте один или оба - это попадёт в текст оповещения, "
        "чтобы мерчанты понимали, по какому направлению трудности:",
        reply_markup=build_channels_keyboard([])
    )


@dp.callback_query(IncidentState.selecting_channels, F.data.startswith("togglech_"))
async def toggle_channel(callback: types.CallbackQuery, state: FSMContext):
    ch = callback.data[len("togglech_"):]
    if ch not in CHANNEL_ORDER:
        return await callback.answer()

    user_data = await state.get_data()
    selected = normalize_channels(user_data.get("channels", []))
    if ch in selected:
        selected.remove(ch)
    else:
        selected.append(ch)
    selected = normalize_channels(selected)

    await state.update_data(channels=selected)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=build_channels_keyboard(selected))


@dp.callback_query(IncidentState.selecting_channels, F.data == "channels_done")
async def finish_channel_selection(callback: types.CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    selected = normalize_channels(user_data.get("channels", []))

    if not selected:
        return await callback.answer("⚠️ Выберите хотя бы один канал!", show_alert=True)

    alert_type = user_data["alert_type"]

    if alert_type == "custom":
        await state.set_state(IncidentState.waiting_for_custom_text_ru)
        await callback.answer()
        await callback.message.edit_text(
            "✏️ Введите текст на **русском** (уйдёт в чаты с языком RU, отправляется 1 в 1).\n"
            "ℹ️ В свой текст ничего не подставляется автоматически (ни банк, ни каналы) - впишите нужное сами.",
            parse_mode="Markdown",
            reply_markup=cancel_only_keyboard()
        )
        return

    await callback.answer()
    await send_alert(
        target_msg=callback.message,
        currency=user_data["selected_currency"],
        provider=user_data["provider"],
        label=user_data["label"],
        target_chats=user_data["target_chats"],
        selected_chat_ids=user_data["selected_chat_ids"],
        template_key=alert_type,
        channels=selected,
        methods=user_data.get("methods", [])
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
            label=user_data["label"],
            target_chats=target_chats,
            selected_chat_ids=selected_chat_ids,
            custom_texts={"RU": message.text, "EN": message.text},
            channels=user_data.get("channels", []),
            methods=user_data.get("methods", [])
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
        label=user_data["label"],
        target_chats=user_data["target_chats"],
        selected_chat_ids=user_data["selected_chat_ids"],
        custom_texts={"RU": user_data["custom_text_ru"], "EN": message.text},
        channels=user_data.get("channels", []),
        methods=user_data.get("methods", [])
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


async def send_alert(target_msg: types.Message, currency: str, provider: str, label: str, target_chats: dict, selected_chat_ids: list,
                     template_key: str = None, custom_texts: dict = None, channels: list = None, methods: list = None):
    data = load_data()
    channels = normalize_channels(channels)
    methods = normalize_methods(methods, currency)
    sent_messages = []
    failed = []
    warnings = []
    selected_chat_ids_str = [str(x) for x in selected_chat_ids]

    # базовый текст (без тегов cc, которые у каждого чата свои) - для отображения в "🔍 Подробнее"
    if custom_texts:
        text_ru = escape_md(custom_texts.get("RU", ""))
        text_en = escape_md(custom_texts.get("EN", custom_texts.get("RU", "")))
        type_label = "Свой текст"
    else:
        text_ru = render_template(template_key, "RU", currency, provider, channels, methods=methods)
        text_en = render_template(template_key, "EN", currency, provider, channels, methods=methods)
        type_label = TYPE_LABELS.get(template_key, template_key)

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
            alert_text = render_template(template_key, lang, currency, provider, channels, methods=methods)

        if tags:
            safe_tags = [escape_md(t) for t in tags]
            alert_text += f"\n\n📌 **cc:** {' '.join(safe_tags)}"

        fell_back_note = ""
        try:
            msg, used_thread_id, fell_back = await send_with_thread_fallback(chat_id, thread_id, alert_text)
            if fell_back:
                fell_back_note = "ветка недоступна, сообщение ушло в General"
            item = {
                "chat_key": cid_str, "chat_id": chat_id, "thread_id": used_thread_id,
                "message_id": msg.message_id, "lang": lang, "name": name
            }
            if methods:
                # валюта с методами (EGP): ограничения хранятся по методам, чтобы восстанавливать каждый отдельно
                item["pairs"] = {m: list(channels) for m in methods}
            else:
                item["channels"] = list(channels)
            sent_messages.append(item)
        except Exception as e:
            err_str = str(e)
            log_error(f"[send_alert] {cid_str} ({name}): {err_str}")
            failed.append(f"• `{cid_str}` ({escape_md(name)}): {escape_md(err_str)}")
            continue
        if fell_back_note:
            warnings.append(f"• `{cid_str}` ({escape_md(name)}): {escape_md(fell_back_note)}")

    label_shown = escape_md(label or provider or "без названия")
    icons = channel_icons(channels)
    icons_part = f" {icons}" if icons else ""

    if sent_messages:
        data = load_data()  # свежие данные: пока шла рассылка, файл мог измениться
        incident_id = data.get("next_incident_id", 1)
        data["next_incident_id"] = incident_id + 1
        data["active_incidents"].append({
            "id": incident_id,
            "currency": currency,
            "method": ", ".join(methods),
            "methods": methods,
            "provider": provider,
            "label": label,
            "channels": list(channels),
            "created_at": now_iso(),
            "notified_names": [m["name"] for m in sent_messages],
            "type_label": type_label,
            "text_ru": text_ru,
            "text_en": text_en,
            "messages": sent_messages
        })
        save_data(data)
        report = f"✅ Оповещение по **{escape_md(curr_display(currency, methods))} ({label_shown})**{icons_part} отправлено в {len(sent_messages)} чат(ов)!"
    else:
        # ни одно сообщение не ушло - пустую просадку не создаём (её нельзя было бы нормально восстановить)
        report = f"❌ Оповещение по **{escape_md(curr_display(currency, methods))} ({label_shown})**{icons_part} не отправлено ни в один чат - просадка не создана."

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
        label = inc.get("label") or inc.get("provider") or "без названия"
        chat_count = len(inc.get("messages", []))
        icons = channel_icons(active_channels(inc))
        icons_prefix = f"{icons} " if icons else ""
        if inc.get("kind") == "outage":
            btn_text = f"{icons_prefix}🛠 {label} ({chat_count} чат)"
        else:
            btn_text = f"{icons_prefix}{incident_currency_label(inc)} — {label} ({chat_count} чат)"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"resolveinc_{inc['id']}")])

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

    if incident.get("kind") == "outage" and len(active_incident_chats) > MAX_CHAT_BUTTONS:
        # слишком много чатов для одного сообщения с кнопками (лимит Telegram - 100) - выбор чатов пропускаем,
        # восстановление применяется ко всем чатам просадки, а сузить можно выбором валют и каналов
        await callback.answer()
        return await start_outage_resolve(callback, state, incident, selected_resolve_ids)

    kb = build_chats_selection_keyboard(active_incident_chats, selected_resolve_ids, action_type="resolve")
    await callback.message.edit_text(
        f"Восстановление: **{escape_md(incident_currency_label(incident))} ({escape_md(incident.get('label') or incident.get('provider') or 'без названия')})**.\nОтметьте чаты, в которых нужно зафиксировать восстановление:",
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

    if incident.get("kind") == "outage":
        # сбой на площадке восстанавливается по валютам и каналам - отдельный сценарий
        await callback.answer()
        return await start_outage_resolve(callback, state, incident, selected_resolve_ids)

    if has_method_pairs(incident):
        # просадка по методам (EGP) восстанавливается по методам и каналам
        await callback.answer()
        return await start_method_resolve(callback, state, incident, selected_resolve_ids)

    # какие каналы ещё активны в выбранных чатах
    union = channels_of_selected(incident, selected_resolve_ids)

    await callback.answer()
    if len(union) >= 2:
        # активны оба канала - спрашиваем, что именно восстановилось
        await state.update_data(resolve_available_channels=union, selected_resolve_channels=[])
        await state.set_state(ResolveState.selecting_resolve_channels)
        await callback.message.edit_text(
            "Какие каналы восстановились? Отметьте то, что реально заработало "
            "(или нажмите «Всё восстановилось»):",
            reply_markup=build_channels_keyboard([], available=union, mode="resolve")
        )
        return

    # активен один канал (или это старая просадка без каналов) - спрашивать нечего
    await run_resolve(callback, state, restored=union)


@dp.callback_query(ResolveState.selecting_resolve_channels, F.data.startswith("toggleresch_"))
async def toggle_resolve_channel(callback: types.CallbackQuery, state: FSMContext):
    ch = callback.data[len("toggleresch_"):]
    user_data = await state.get_data()
    available = normalize_channels(user_data.get("resolve_available_channels", []))
    if ch not in available:
        return await callback.answer()

    selected = normalize_channels(user_data.get("selected_resolve_channels", []))
    if ch in selected:
        selected.remove(ch)
    else:
        selected.append(ch)
    selected = normalize_channels(selected)

    await state.update_data(selected_resolve_channels=selected)
    await callback.answer()
    await callback.message.edit_reply_markup(
        reply_markup=build_channels_keyboard(selected, available=available, mode="resolve")
    )


@dp.callback_query(ResolveState.selecting_resolve_channels, F.data == "resch_done")
async def confirm_resolve_channels(callback: types.CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    selected = normalize_channels(user_data.get("selected_resolve_channels", []))
    if not selected:
        return await callback.answer("⚠️ Отметьте хотя бы один восстановившийся канал!", show_alert=True)
    await callback.answer()
    await run_resolve(callback, state, restored=selected)


@dp.callback_query(ResolveState.selecting_resolve_channels, F.data == "resch_all")
async def resolve_all_channels(callback: types.CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    available = normalize_channels(user_data.get("resolve_available_channels", []))
    await callback.answer()
    await run_resolve(callback, state, restored=available)


async def run_resolve(callback: types.CallbackQuery, state: FSMContext, restored: list):
    """Отправляет сообщения о восстановлении в выбранные чаты. restored - какие каналы восстановились
    (пусто - старая просадка без каналов). Если в чате остался невосстановленный канал, чат остаётся
    в просадке только с этим каналом."""
    user_data = await state.get_data()
    inc_id = user_data["resolve_inc_id"]
    selected_resolve_ids = [str(x) for x in user_data["selected_resolve_ids"]]
    restored = normalize_channels(restored)

    data = load_data()
    incident = next((x for x in data.get("active_incidents", []) if x["id"] == inc_id), None)

    if not incident or not incident.get("messages"):
        await state.clear()
        return await callback.message.edit_text("❌ Ошибка: Инцидент уже закрыт.")

    resolved_count = 0
    partial_count = 0
    skipped_count = 0
    failed = []
    warnings = []
    remaining_messages = []
    currency = incident["currency"]
    provider = incident["provider"]
    label = incident.get("label") or provider or "без названия"

    for item in incident["messages"]:
        cid_str = str(item.get("chat_key", item["chat_id"]))

        if cid_str not in selected_resolve_ids:
            remaining_messages.append(item)
            continue

        item_channels = normalize_channels(item.get("channels"))
        if item_channels:
            restored_here = [c for c in item_channels if c in restored]
            if not restored_here:
                # выбранные восстановившиеся каналы к этому чату не относятся - оставляем как есть
                skipped_count += 1
                remaining_messages.append(item)
                continue
            remaining_here = [c for c in item_channels if c not in restored_here]
            template_key = "resolve_partial" if remaining_here else "resolve_channels"
        else:
            restored_here, remaining_here = [], []
            template_key = "resolve"

        lang = item.get("lang", "RU")
        name = item.get("name") or data.get("chats", {}).get(cid_str, {}).get("name", cid_str)
        tags = data.get("chats", {}).get(cid_str, {}).get("tags", [])

        chat_id = item.get("chat_id")
        thread_id = item.get("thread_id")
        if not chat_id:
            chat_id, thread_id = parse_chat_id(cid_str)

        resolve_text = render_template(template_key, lang, currency, provider, restored=restored_here, remaining=remaining_here,
                                       methods=incident_methods(incident))
        if tags:
            safe_tags = [escape_md(t) for t in tags]
            resolve_text += f"\n\n📌 **cc:** {' '.join(safe_tags)}"

        sent_ok = False
        try:
            _, _, fell_back = await send_with_thread_fallback(chat_id, thread_id, resolve_text, reply_to_message_id=item["message_id"])
            sent_ok = True
        except Exception as e:
            log_error(f"[resolve reply] {cid_str} ({name}): {e}")
            try:
                _, _, fell_back = await send_with_thread_fallback(chat_id, thread_id, resolve_text)
                sent_ok = True
            except Exception as ex:
                log_error(f"[resolve plain] {cid_str} ({name}): {ex}")
                failed.append(f"• `{cid_str}` ({escape_md(name)}): {escape_md(str(ex))}")

        if not sent_ok:
            # чат не восстановлен - оставляем его в активном инциденте как был
            remaining_messages.append(item)
            continue

        resolved_count += 1
        if fell_back:
            warnings.append(f"• `{cid_str}` ({escape_md(name)}): ветка недоступна, ушло в General")
        if remaining_here:
            # восстановился только один канал - чат остаётся в просадке с оставшимся каналом
            item["channels"] = remaining_here
            remaining_messages.append(item)
            partial_count += 1

    head = f"**{escape_md(incident_currency_label(incident))} ({escape_md(label)})**"
    if remaining_messages:
        incident["messages"] = remaining_messages
        status_msg = f"🟢 Восстановление по {head} зафиксировано в {resolved_count} чат(ах)."
        if partial_count:
            status_msg += f"\n📌 Из них частично (один канал ещё не восстановлен): **{partial_count}**."
        if skipped_count:
            status_msg += f"\nℹ️ Не затронуты выбранным каналом и остались как были: **{skipped_count}**."
        status_msg += f"\n⚠️ Осталось чатов в этой просадке: **{len(remaining_messages)}**."
        still = active_channels(incident)
        if still:
            status_msg += f"\nАктивные каналы: {channel_names_admin(still)}."
    else:
        data["active_incidents"] = [x for x in data["active_incidents"] if x["id"] != inc_id]
        status_msg = f"🟢 Просадка по {head} полностью закрыта!"

    if failed:
        status_msg += f"\n\n❌ **Не удалось отправить восстановление в {len(failed)} чат(ов):**\n" + "\n".join(failed)
    if warnings:
        status_msg += f"\n\n⚠️ **Автоматически перенаправлено в General ({len(warnings)}):**\n" + "\n".join(warnings)

    commit_incident_state(inc_id, incident, closed=not remaining_messages)
    await state.clear()

    for chunk_start in range(0, len(status_msg), 3500):
        if chunk_start == 0:
            await callback.message.edit_text(status_msg[:3500], parse_mode="Markdown")
        else:
            await callback.message.answer(status_msg[chunk_start:chunk_start + 3500], parse_mode="Markdown")


# ------------------- ВОССТАНОВЛЕНИЕ ПРОСАДКИ ПО МЕТОДАМ (EGP) -------------------
def build_method_resolve_keyboard(available: list, selected: list, counts: dict):
    sel = set(selected)
    rows = [[InlineKeyboardButton(text=f"{'✅' if m in sel else '❌'} {m} ({counts.get(m, 0)})", callback_data=f"mrtg_{i}")]
            for i, m in enumerate(available)]
    rows.append([InlineKeyboardButton(text="➡️ ДАЛЕЕ", callback_data="mr_done")])
    rows.append([InlineKeyboardButton(text="✅ Всё восстановилось (все методы и каналы)", callback_data="mr_all")])
    rows.append(cancel_button_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_method_resolve_channels_keyboard(available: list, selected: list):
    sel = set(normalize_channels(selected))
    rows = [[InlineKeyboardButton(text=f"{'✅' if ch in sel else '❌'} {CHANNEL_BUTTON_LABELS[ch]}", callback_data=f"mrchtg_{ch}")]
            for ch in available]
    rows.append([InlineKeyboardButton(text="➡️ ПОДТВЕРДИТЬ ВЫБРАННОЕ", callback_data="mrch_done")])
    if len(available) > 1:
        rows.append([InlineKeyboardButton(text="✅ Оба канала (сразу)", callback_data="mrch_both")])
    rows.append(cancel_button_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start_method_resolve(callback: types.CallbackQuery, state: FSMContext, incident: dict, selected_ids: list):
    order = [name for _, name in CURRENCY_METHODS.get(incident.get("currency"), [])]
    avail = pairs_keys(incident, selected_ids, order)
    if not avail:
        await state.clear()
        return await callback.message.edit_text("ℹ️ В выбранных чатах уже нет активных ограничений.")

    await state.update_data(mr_available=avail, mr_selected=[], mr_channels=[])
    if len(avail) == 1:
        # метод один - спрашивать нечего
        await state.update_data(mr_selected=avail)
        return await method_resolve_after_methods(callback, state, incident, selected_ids, avail)

    await state.set_state(ResolveState.selecting_methods)
    counts = outage_currency_counts(incident, selected_ids)
    await callback.message.edit_text(
        "Какие методы восстановились? Отметьте то, что реально заработало (или нажмите «Всё восстановилось»). "
        "В скобках - сколько выбранных мерчантов ещё ограничено:",
        reply_markup=build_method_resolve_keyboard(avail, [], counts)
    )


async def method_resolve_after_methods(callback, state, incident, selected_ids, methods):
    chans = outage_active_channels(incident, selected_ids, methods)
    if len(chans) <= 1:
        # активен один канал - спрашивать нечего
        return await run_method_resolve(callback, state, methods, chans)
    await state.update_data(mr_available_channels=chans, mr_channels=[])
    await state.set_state(ResolveState.selecting_method_channels)
    await callback.message.edit_text(
        f"Методы: **{', '.join(methods)}**.\nПо каким каналам восстановилась работа? (применится ко всем отмеченным методам)",
        parse_mode="Markdown",
        reply_markup=build_method_resolve_channels_keyboard(chans, [])
    )


@dp.callback_query(ResolveState.selecting_methods, F.data.startswith("mrtg_"))
async def method_resolve_toggle(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    avail = d["mr_available"]
    try:
        method = avail[int(callback.data[len("mrtg_"):])]
    except (ValueError, IndexError):
        return await callback.answer()
    selected = list(d["mr_selected"])
    if method in selected:
        selected.remove(method)
    else:
        selected.append(method)
    selected = [m for m in avail if m in selected]
    await state.update_data(mr_selected=selected)
    incident = _find_incident(load_data(), d["resolve_inc_id"])
    counts = outage_currency_counts(incident, [str(x) for x in d["selected_resolve_ids"]]) if incident else {}
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=build_method_resolve_keyboard(avail, selected, counts))


@dp.callback_query(ResolveState.selecting_methods, F.data == "mr_done")
async def method_resolve_done(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    selected = d["mr_selected"]
    if not selected:
        return await callback.answer("⚠️ Отметьте хотя бы один восстановившийся метод!", show_alert=True)
    incident = _find_incident(load_data(), d["resolve_inc_id"])
    if not incident:
        await state.clear()
        return await callback.message.edit_text("❌ Ошибка: Инцидент уже закрыт.")
    await callback.answer()
    await method_resolve_after_methods(callback, state, incident, [str(x) for x in d["selected_resolve_ids"]], selected)


@dp.callback_query(ResolveState.selecting_methods, F.data == "mr_all")
async def method_resolve_all(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await callback.answer()
    await run_method_resolve(callback, state, d["mr_available"], list(CHANNEL_ORDER))


@dp.callback_query(ResolveState.selecting_method_channels, F.data.startswith("mrchtg_"))
async def method_resolve_toggle_channel(callback: types.CallbackQuery, state: FSMContext):
    ch = callback.data[len("mrchtg_"):]
    d = await state.get_data()
    avail = normalize_channels(d["mr_available_channels"])
    if ch not in avail:
        return await callback.answer()
    selected = normalize_channels(d.get("mr_channels", []))
    if ch in selected:
        selected.remove(ch)
    else:
        selected.append(ch)
    selected = normalize_channels(selected)
    await state.update_data(mr_channels=selected)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=build_method_resolve_channels_keyboard(avail, selected))


@dp.callback_query(ResolveState.selecting_method_channels, F.data == "mrch_done")
async def method_resolve_channels_done(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    selected = normalize_channels(d.get("mr_channels", []))
    if not selected:
        return await callback.answer("⚠️ Отметьте хотя бы один восстановившийся канал!", show_alert=True)
    await callback.answer()
    await run_method_resolve(callback, state, d["mr_selected"], selected)


@dp.callback_query(ResolveState.selecting_method_channels, F.data == "mrch_both")
async def method_resolve_channels_both(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await callback.answer()
    await run_method_resolve(callback, state, d["mr_selected"], list(CHANNEL_ORDER))


async def run_method_resolve(callback: types.CallbackQuery, state: FSMContext, restored_methods: list, restored_channels: list):
    """Восстановление по методам и каналам. Мерчант получает ОДНО сообщение только про то, что у него реально вернулось;
    остальное остаётся в просадке. Просадка закрывается, когда ограничений не осталось."""
    d = await state.get_data()
    inc_id = d["resolve_inc_id"]
    selected_ids = [str(x) for x in d["selected_resolve_ids"]]
    restored_methods = list(restored_methods)
    restored_channels = normalize_channels(restored_channels)

    data = load_data()
    incident = _find_incident(data, inc_id)
    if not incident or not incident.get("messages"):
        await state.clear()
        return await callback.message.edit_text("❌ Ошибка: Инцидент уже закрыт.")

    currency = incident["currency"]
    provider = incident.get("provider", "")
    resolved_count = partial_count = skipped_count = 0
    failed, warnings, remaining_messages = [], [], []
    head = f"**{escape_md(incident_currency_label(incident))} ({escape_md(incident.get('label') or provider or 'без названия')})**"

    for item in incident["messages"]:
        cid_str = str(item.get("chat_key", item["chat_id"]))
        if cid_str not in selected_ids:
            remaining_messages.append(item)
            continue

        pairs = {m: normalize_channels(chs) for m, chs in (item.get("pairs") or {}).items()}
        restored_pairs, new_pairs = {}, {}
        for method, chs in pairs.items():
            if method in restored_methods:
                r = [ch for ch in chs if ch in restored_channels]
                rest = [ch for ch in chs if ch not in restored_channels]
            else:
                r, rest = [], chs
            if r:
                restored_pairs[method] = r
            if rest:
                new_pairs[method] = rest

        if not restored_pairs:
            skipped_count += 1
            remaining_messages.append(item)
            continue

        lang = item.get("lang", "RU")
        name = item.get("name") or data.get("chats", {}).get(cid_str, {}).get("name", cid_str)
        tags = data.get("chats", {}).get(cid_str, {}).get("tags", [])
        chat_id = item.get("chat_id")
        thread_id = item.get("thread_id")
        if not chat_id:
            chat_id, thread_id = parse_chat_id(cid_str)

        text = render_method_resolve(lang, currency, provider, restored_pairs, new_pairs)
        if tags:
            text += f"\n\n📌 **cc:** {' '.join(escape_md(t) for t in tags)}"

        sent_ok = False
        fell_back = False
        try:
            _, _, fell_back = await send_with_thread_fallback(chat_id, thread_id, text, reply_to_message_id=item["message_id"])
            sent_ok = True
        except Exception as e:
            log_error(f"[method resolve reply] {cid_str} ({name}): {e}")
            try:
                _, _, fell_back = await send_with_thread_fallback(chat_id, thread_id, text)
                sent_ok = True
            except Exception as ex:
                log_error(f"[method resolve plain] {cid_str} ({name}): {ex}")
                failed.append(f"• `{cid_str}` ({escape_md(name)}): {escape_md(str(ex))}")

        if not sent_ok:
            remaining_messages.append(item)  # не отправилось - оставляем как было
            continue

        resolved_count += 1
        if fell_back:
            warnings.append(f"• `{cid_str}` ({escape_md(name)}): ветка недоступна, ушло в General")
        if new_pairs:
            item["pairs"] = new_pairs
            remaining_messages.append(item)
            partial_count += 1

    if remaining_messages:
        incident["messages"] = remaining_messages
        status = f"🟢 Восстановление по {head} зафиксировано в {resolved_count} чат(ах)."
        if partial_count:
            status += f"\n📌 Из них частично (часть методов/каналов ещё ограничена): **{partial_count}**."
        if skipped_count:
            status += f"\nℹ️ Не затронуты выбранным и остались как были: **{skipped_count}**."
        status += f"\n⚠️ Осталось чатов в этой просадке: **{len(remaining_messages)}**."
        order = [name for _, name in CURRENCY_METHODS.get(currency, [])]
        parts = []
        for method in pairs_keys(incident, None, order):
            chans = set()
            for m in remaining_messages:
                chans.update((m.get("pairs") or {}).get(method, []))
            parts.append(f"{method} {channel_icons(chans)}")
        if parts:
            status += "\nАктивно: " + ", ".join(parts)
    else:
        status = f"🟢 Просадка по {head} полностью закрыта!"

    if failed:
        status += f"\n\n❌ **Не удалось отправить восстановление в {len(failed)} чат(ов):**\n" + "\n".join(failed)
    if warnings:
        status += f"\n\n⚠️ **Автоматически перенаправлено в General ({len(warnings)}):**\n" + "\n".join(warnings)

    commit_incident_state(inc_id, incident, closed=not remaining_messages)
    await state.clear()

    for chunk_start in range(0, len(status), 3500):
        if chunk_start == 0:
            await callback.message.edit_text(status[:3500], parse_mode="Markdown")
        else:
            await callback.message.answer(status[chunk_start:chunk_start + 3500], parse_mode="Markdown")


# ------------------- СБОЙ НА ПЛОЩАДКЕ (общая авария по нескольким валютам) -------------------
MAX_CHAT_BUTTONS = 90  # у Telegram лимит 100 кнопок в одном сообщении, часть уходит на служебные

OUTAGE_TEXTS = {
    "RU": {
        "alert": ("⚠️ Коллеги, в связи с незапланированными техническими работами на площадке наблюдаются сбои "
                  "{channels_on} ({currencies}).\n"
                  "🛑 **Просим временно приостановить трафик {channels_on} ({currencies}).**{unaffected}\n"
                  "О восстановлении сообщим дополнительно."),
        "resolve": "✅ Коллеги, работа {restored} восстановлена, трафик можно возобновить.",
        "resolve_partial": ("✅ Коллеги, работа {restored} восстановлена, трафик можно возобновить.\n"
                            "⚠️ Ограничения сохраняются: {remaining}. О восстановлении сообщим дополнительно."),
        "other_currencies": " По остальным валютам ограничений нет.",
    },
    "EN": {
        "alert": ("⚠️ Colleagues, due to unplanned technical works on the platform, we are experiencing issues "
                  "{channels_on} ({currencies}).\n"
                  "🛑 **Please temporarily pause traffic {channels_on} ({currencies}).**{unaffected}\n"
                  "We will notify you once resolved."),
        "resolve": "✅ Colleagues, operations {restored} have been restored, traffic can be resumed.",
        "resolve_partial": ("✅ Colleagues, operations {restored} have been restored, traffic can be resumed.\n"
                            "⚠️ Restrictions remain in place: {remaining}. We will notify you once resolved."),
        "other_currencies": " There are no restrictions on your other currencies.",
    },
}


def group_pairs(pairs: dict) -> list:
    """{валюта: [каналы]} -> [((каналы...), [валюты...])], сгруппировано по одинаковому набору каналов."""
    groups = {}
    for cur, chs in pairs.items():
        key = tuple(normalize_channels(chs))
        if key:
            groups.setdefault(key, []).append(cur)
    return list(groups.items())


def outage_currency_item(code: str) -> str:
    """Валюта для текста мерчанту. Для валют с методами (EGP) перечисляем ВСЕ методы: при сбое на площадке затронуты все."""
    if code in CURRENCY_METHODS:
        return f"{code} · {', '.join(name for _, name in CURRENCY_METHODS[code])}"
    return code


def join_outage_currencies(codes) -> str:
    """'UZS, KGS' - а если среди валют есть валюта с методами, то через ';', чтобы список методов не путался с валютами:
    'EGP · InstaPay, Orange Cash, Vodafone Cash; UZS'."""
    codes = list(codes)
    sep = "; " if any(c in CURRENCY_METHODS for c in codes) else ", "
    return sep.join(outage_currency_item(c) for c in codes)


def outage_group_phrase(lang: str, pairs: dict, kind: str) -> str:
    """'по приёмному каналу (UZS); по выплатному каналу (KGS)' (kind='on') или 'выплатной канал (UZS, KGS)' (kind='nom').
    Ключи - валюты сбоя (EGP раскрывается в методы) либо названия методов (для просадки по методам)."""
    return "; ".join(f"{channel_phrase(lang, kind, list(key))} ({join_outage_currencies(curs)})" for key, curs in group_pairs(pairs))


def chat_currency_codes(info: dict) -> list:
    out = []
    for c in info.get("currencies", []):
        c = (c or "").strip().upper()
        if CURRENCY_RE.fullmatch(c) and c not in out:
            out.append(c)
    return out


def render_outage_alert(lang: str, currencies_here: list, channels, has_other_currencies: bool) -> str:
    lang = lang if lang in ("RU", "EN") else "RU"
    t = OUTAGE_TEXTS[lang]
    unaffected = channel_phrase(lang, "unaffected", channels)
    if has_other_currencies:
        unaffected += t["other_currencies"]
    return t["alert"].format(
        channels_on=channel_phrase(lang, "on", channels),
        currencies=join_outage_currencies(currencies_here),
        unaffected=unaffected,
    )


def render_outage_resolve(lang: str, restored_pairs: dict, remaining_pairs: dict) -> str:
    lang = lang if lang in ("RU", "EN") else "RU"
    t = OUTAGE_TEXTS[lang]
    restored = outage_group_phrase(lang, restored_pairs, "on")
    if remaining_pairs:
        return t["resolve_partial"].format(restored=restored, remaining=outage_group_phrase(lang, remaining_pairs, "nom"))
    return t["resolve"].format(restored=restored)


def outage_message_text(info: dict, lang: str, currencies: list, channels, custom_texts=None) -> str:
    """Текст сбоя для конкретного мерчанта (без тегов cc): только его валюты из выбранных."""
    lang = lang if lang in ("RU", "EN") else "RU"
    if custom_texts:
        return escape_md(custom_texts.get(lang, custom_texts.get("RU", "")))
    codes = chat_currency_codes(info)
    chat_curs = [c for c in currencies if c in codes]
    has_other = any(c not in currencies for c in codes)
    return render_outage_alert(lang, chat_curs, channels, has_other)


def commit_incident_state(inc_id: int, incident: dict, closed: bool):
    """Сохраняет итог операции по просадке, не затирая то, что другие операторы изменили, пока шла рассылка
    (рассылка в десятки чатов занимает время - файл мог измениться: новые мерчанты, чужие оповещения)."""
    fresh = load_data()
    if closed:
        fresh["active_incidents"] = [x for x in fresh["active_incidents"] if x["id"] != inc_id]
    else:
        target = next((x for x in fresh["active_incidents"] if x["id"] == inc_id), None)
        if target is not None:
            target["messages"] = incident["messages"]
            if incident.get("kind") == "outage":
                target["currency"] = incident.get("currency", target.get("currency"))
    save_data(fresh)


def _grid3(buttons: list) -> list:
    return [buttons[i:i + 3] for i in range(0, len(buttons), 3)]


def build_outage_currency_keyboard(available: list, selected: list, counts: dict):
    sel = set(selected)
    btns = [InlineKeyboardButton(text=f"{'✅' if c in sel else '❌'} {c} ({counts.get(c, 0)})", callback_data=f"outcurtg_{c}")
            for c in available]
    rows = _grid3(btns)
    rows.append([InlineKeyboardButton(text="➡️ ПРОДОЛЖИТЬ", callback_data="outcur_done")])
    rows.append(cancel_button_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_outage_chats_keyboard(target_chats: dict, selected_ids: list, currencies: list):
    sel = set(str(x) for x in selected_ids)
    rows = []
    for cid, info in target_chats.items():
        curs = ",".join(c for c in currencies if c in chat_currency_codes(info))
        text = f"{'✅' if cid in sel else '❌'} {info.get('name', cid)} ({info.get('lang', 'RU')}) · {curs}"
        rows.append([InlineKeyboardButton(text=text, callback_data=f"outchattg_{cid}")])
    rows.append([InlineKeyboardButton(text="➡️ ПРОДОЛЖИТЬ", callback_data="outchats_done")])
    rows.append(cancel_button_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_outage_channels_keyboard(selected: list):
    sel = set(normalize_channels(selected))
    rows = [[InlineKeyboardButton(text=f"{'✅' if ch in sel else '❌'} {CHANNEL_BUTTON_LABELS[ch]}", callback_data=f"outchtg_{ch}")]
            for ch in CHANNEL_ORDER]
    rows.append([InlineKeyboardButton(text="➡️ ПРОДОЛЖИТЬ", callback_data="outchan_done")])
    rows.append([InlineKeyboardButton(text="✅ Оба канала (сразу дальше)", callback_data="outchan_both")])
    rows.append(cancel_button_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_outage_confirmation(d: dict) -> str:
    currencies = d["out_selected"]
    channels = normalize_channels(d["out_channels"])
    target = d["out_target_chats"]
    ids = [str(x) for x in d["out_selected_chat_ids"]]
    n_en = sum(1 for cid in ids if target[cid].get("lang", "RU") == "EN")
    custom = d.get("out_type") == "custom"

    custom_texts = None
    if custom:
        def cut(t):
            t = t or ""
            return t[:1500] + ("…" if len(t) > 1500 else "")
        custom_texts = {"RU": cut(d.get("out_custom_ru")), "EN": cut(d.get("out_custom_en", d.get("out_custom_ru")))}

    sample_cid = next((cid for cid in ids if target[cid].get("lang", "RU") != "EN"), ids[0])
    sample_info = target[sample_cid]
    sample_lang = sample_info.get("lang", "RU")
    sample = outage_message_text(sample_info, sample_lang, currencies, channels, custom_texts)

    return (
        "🛠 **Сбой на площадке - проверьте перед отправкой**\n\n"
        f"Валюты: **{', '.join(currencies)}**\n"
        f"Каналы: **{channel_names_admin(channels)}**\n"
        f"Чатов: **{len(ids)}** (RU: {len(ids) - n_en}, EN: {n_en})\n"
        f"Тип: **{'свой текст' if custom else 'стандартный текст'}**\n\n"
        f"**Пример ({sample_lang}) для «{escape_md(sample_info.get('name', sample_cid))}»:**\n{sample}\n\n"
        "Каждый мерчант получит ОДНО сообщение со своими валютами."
    )


@dp.message(F.text == "🛠 Сбой на площадке", F.chat.type == "private")
async def outage_start(message: types.Message, state: FSMContext):
    if not is_authorized(message.from_user.id):
        return await message.answer("🛑 Введите пароль через /start")

    data = load_data()
    counts = {}
    for info in data.get("chats", {}).values():
        for c in chat_currency_codes(info):
            counts[c] = counts.get(c, 0) + 1
    available = [c for c in get_currency_list(data) if counts.get(c)]
    if not available:
        return await message.answer("❌ В базе нет ни одного мерчанта с валютой.")

    await state.clear()
    await state.set_state(OutageState.selecting_currencies)
    await state.update_data(out_available=available, out_selected=list(available), out_counts=counts)
    await message.answer(
        "🛠 **Сбой на площадке**\nОтметьте валюты, по которым затронуты мерчанты (по умолчанию выбраны все). "
        "В скобках - сколько мерчантов с этой валютой:",
        parse_mode="Markdown",
        reply_markup=build_outage_currency_keyboard(available, available, counts)
    )


@dp.callback_query(OutageState.selecting_currencies, F.data.startswith("outcurtg_"))
async def outage_toggle_currency(callback: types.CallbackQuery, state: FSMContext):
    cur = callback.data[len("outcurtg_"):]
    d = await state.get_data()
    available = d["out_available"]
    if cur not in available:
        return await callback.answer()
    selected = list(d["out_selected"])
    if cur in selected:
        selected.remove(cur)
    else:
        selected.append(cur)
    selected = [c for c in available if c in selected]
    await state.update_data(out_selected=selected)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=build_outage_currency_keyboard(available, selected, d["out_counts"]))


@dp.callback_query(OutageState.selecting_currencies, F.data == "outcur_done")
async def outage_currencies_done(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    selected = d["out_selected"]
    if not selected:
        return await callback.answer("⚠️ Выберите хотя бы одну валюту!", show_alert=True)

    data = load_data()
    sel = set(selected)
    target = {str(cid): info for cid, info in data["chats"].items() if sel & set(chat_currency_codes(info))}
    if not target:
        return await callback.answer("❌ Нет мерчантов с выбранными валютами.", show_alert=True)

    await state.update_data(out_target_chats=target, out_selected_chat_ids=list(target.keys()))
    await callback.answer()

    if len(target) > MAX_CHAT_BUTTONS:
        # слишком много чатов для одного сообщения с кнопками - отправим всем, сузить можно выбором валют
        await state.set_state(OutageState.selecting_channels)
        await state.update_data(out_channels=[])
        await callback.message.edit_text(
            f"Мерчантов с выбранными валютами: **{len(target)}** - это слишком много для списка с кнопками, "
            "поэтому оповещение уйдёт всем (сузить круг можно выбором валют на предыдущем шаге).\n\n"
            "Какие каналы затронуты?",
            parse_mode="Markdown",
            reply_markup=build_outage_channels_keyboard([])
        )
        return

    await state.set_state(OutageState.selecting_chats)
    await callback.message.edit_text(
        f"Валюты: **{', '.join(selected)}**.\nМерчантов: **{len(target)}**. Снимите галочки с тех, кому оповещение не нужно:",
        parse_mode="Markdown",
        reply_markup=build_outage_chats_keyboard(target, list(target.keys()), selected)
    )


@dp.callback_query(OutageState.selecting_chats, F.data.startswith("outchattg_"))
async def outage_toggle_chat(callback: types.CallbackQuery, state: FSMContext):
    cid = callback.data[len("outchattg_"):]
    d = await state.get_data()
    target = d["out_target_chats"]
    if cid not in target:
        return await callback.answer()
    selected_ids = [str(x) for x in d["out_selected_chat_ids"]]
    if cid in selected_ids:
        selected_ids.remove(cid)
    else:
        selected_ids.append(cid)
    await state.update_data(out_selected_chat_ids=selected_ids)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=build_outage_chats_keyboard(target, selected_ids, d["out_selected"]))


@dp.callback_query(OutageState.selecting_chats, F.data == "outchats_done")
async def outage_chats_done(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d["out_selected_chat_ids"]:
        return await callback.answer("⚠️ Выберите хотя бы один чат!", show_alert=True)
    await state.set_state(OutageState.selecting_channels)
    await state.update_data(out_channels=[])
    await callback.answer()
    await callback.message.edit_text(
        "Какие каналы затронуты? Отметьте один или оба (по умолчанию ничего не выбрано) - это попадёт в текст, "
        "чтобы мерчанты понимали, что именно останавливать:",
        reply_markup=build_outage_channels_keyboard([])
    )


@dp.callback_query(OutageState.selecting_channels, F.data.startswith("outchtg_"))
async def outage_toggle_channel(callback: types.CallbackQuery, state: FSMContext):
    ch = callback.data[len("outchtg_"):]
    if ch not in CHANNEL_ORDER:
        return await callback.answer()
    d = await state.get_data()
    selected = normalize_channels(d.get("out_channels", []))
    if ch in selected:
        selected.remove(ch)
    else:
        selected.append(ch)
    selected = normalize_channels(selected)
    await state.update_data(out_channels=selected)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=build_outage_channels_keyboard(selected))


async def outage_ask_type(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(OutageState.waiting_for_type)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 Стандартный текст (приостановить трафик)", callback_data="outtype_std")],
        [InlineKeyboardButton(text="✏️ Ввести свой текст", callback_data="outtype_custom")],
        cancel_button_row()
    ])
    await callback.message.edit_text("Выберите тип оповещения:", reply_markup=kb)


@dp.callback_query(OutageState.selecting_channels, F.data == "outchan_done")
async def outage_channels_done(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not normalize_channels(d.get("out_channels", [])):
        return await callback.answer("⚠️ Выберите хотя бы один канал!", show_alert=True)
    await callback.answer()
    await outage_ask_type(callback, state)


@dp.callback_query(OutageState.selecting_channels, F.data == "outchan_both")
async def outage_channels_both(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(out_channels=list(CHANNEL_ORDER))
    await callback.answer()
    await outage_ask_type(callback, state)


async def outage_show_confirmation(answer_fn, state: FSMContext):
    d = await state.get_data()
    await state.set_state(OutageState.confirming)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ ОТПРАВИТЬ ВСЕМ", callback_data="outsend")],
        cancel_button_row()
    ])
    await answer_fn(build_outage_confirmation(d), reply_markup=kb, parse_mode="Markdown")


@dp.callback_query(OutageState.waiting_for_type, F.data == "outtype_std")
async def outage_type_std(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(out_type="std")
    await callback.answer()
    await outage_show_confirmation(callback.message.edit_text, state)


@dp.callback_query(OutageState.waiting_for_type, F.data == "outtype_custom")
async def outage_type_custom(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(out_type="custom")
    await state.set_state(OutageState.waiting_for_custom_ru)
    await callback.answer()
    await callback.message.edit_text(
        "✏️ Введите текст на **русском** (уйдёт в чаты с языком RU, отправляется 1 в 1).\n"
        "ℹ️ В свой текст ничего не подставляется автоматически (ни валюты, ни каналы) - впишите нужное сами.",
        parse_mode="Markdown",
        reply_markup=cancel_only_keyboard()
    )


@dp.message(OutageState.waiting_for_custom_ru, F.chat.type == "private")
async def outage_custom_ru(message: types.Message, state: FSMContext):
    if is_reserved_input(message.text):
        await state.clear()
        return await message.answer(
            "⚠️ Действие отменено (получена кнопка меню или команда вместо текста).\n"
            "Если это была команда - отправьте её ещё раз, теперь она сработает.",
            reply_markup=main_keyboard()
        )
    await state.update_data(out_custom_ru=message.text)
    d = await state.get_data()
    target = d["out_target_chats"]
    has_en = any(target[str(cid)].get("lang", "RU") == "EN" for cid in d["out_selected_chat_ids"])
    if not has_en:
        await state.update_data(out_custom_en=message.text)
        return await outage_show_confirmation(message.answer, state)
    await state.set_state(OutageState.waiting_for_custom_en)
    await message.answer(
        "✏️ Теперь введите текст на **английском** (уйдёт в чаты с языком EN, отправляется 1 в 1):",
        parse_mode="Markdown",
        reply_markup=cancel_only_keyboard()
    )


@dp.message(OutageState.waiting_for_custom_en, F.chat.type == "private")
async def outage_custom_en(message: types.Message, state: FSMContext):
    if is_reserved_input(message.text):
        await state.clear()
        return await message.answer(
            "⚠️ Действие отменено (получена кнопка меню или команда вместо текста).\n"
            "Если это была команда - отправьте её ещё раз, теперь она сработает.",
            reply_markup=main_keyboard()
        )
    await state.update_data(out_custom_en=message.text)
    await outage_show_confirmation(message.answer, state)


async def send_outage(target_msg: types.Message, d: dict):
    """Рассылка сбоя: каждый мерчант получает ОДНО сообщение со своими валютами. Создаёт одну просадку (kind=outage)."""
    data = load_data()
    currencies = list(d["out_selected"])
    channels = normalize_channels(d["out_channels"])
    target = d["out_target_chats"]
    ids = [str(x) for x in d["out_selected_chat_ids"]]
    custom_texts = None
    if d.get("out_type") == "custom":
        custom_texts = {"RU": d.get("out_custom_ru", ""), "EN": d.get("out_custom_en", d.get("out_custom_ru", ""))}

    sent, failed, warnings = [], [], []

    for cid_str in ids:
        info = target.get(cid_str, {})
        lang = info.get("lang", "RU")
        name = info.get("name", cid_str)
        chat_curs = [c for c in currencies if c in chat_currency_codes(info)]
        if not chat_curs:
            continue

        chat_id = info.get("chat_id")
        thread_id = info.get("thread_id")
        if not chat_id:
            chat_id, thread_id = parse_chat_id(cid_str)

        text = outage_message_text(info, lang, currencies, channels, custom_texts)
        tags = info.get("tags", [])
        if tags:
            text += f"\n\n📌 **cc:** {' '.join(escape_md(t) for t in tags)}"

        try:
            msg, used_thread_id, fell_back = await send_with_thread_fallback(chat_id, thread_id, text)
            sent.append({
                "chat_key": cid_str, "chat_id": chat_id, "thread_id": used_thread_id, "message_id": msg.message_id,
                "lang": lang, "name": name, "pairs": {c: list(channels) for c in chat_curs}
            })
            if fell_back:
                warnings.append(f"• `{cid_str}` ({escape_md(name)}): ветка недоступна, сообщение ушло в General")
        except Exception as e:
            log_error(f"[send_outage] {cid_str} ({name}): {e}")
            failed.append(f"• `{cid_str}` ({escape_md(name)}): {escape_md(str(e))}")
        await asyncio.sleep(0.05)  # небольшая пауза, чтобы не упереться в лимиты Telegram при массовой рассылке

    now_local = datetime.now(timezone.utc).astimezone(LOCAL_TZ)
    label = f"Сбой на площадке · {now_local.strftime('%d.%m %H:%M')}"
    icons = channel_icons(channels)

    if sent:
        data = load_data()  # свежие данные: пока шла рассылка, файл мог измениться
        incident_id = data.get("next_incident_id", 1)
        data["next_incident_id"] = incident_id + 1
        if custom_texts:
            text_ru, text_en = escape_md(custom_texts["RU"]), escape_md(custom_texts["EN"])
        else:
            text_ru = render_outage_alert("RU", currencies, channels, False)
            text_en = render_outage_alert("EN", currencies, channels, False)
        data["active_incidents"].append({
            "id": incident_id,
            "kind": "outage",
            "currency": ", ".join(currencies),
            "currencies": currencies,
            "provider": "",
            "label": label,
            "channels": list(channels),
            "created_at": now_iso(),
            "notified_names": [m["name"] for m in sent],
            "type_label": "🛠 Сбой на площадке · " + ("свой текст" if custom_texts else "стандартный текст"),
            "text_ru": text_ru,
            "text_en": text_en,
            "messages": sent
        })
        save_data(data)
        report = (f"✅ **{escape_md(label)}**: оповещено {len(sent)} чат(ов).\n"
                  f"Валюты: {', '.join(currencies)} · каналы: {icons}")
    else:
        report = f"❌ **{escape_md(label)}**: не отправлено ни в один чат - просадка не создана."

    if warnings:
        report += f"\n\n⚠️ **Автоматически перенаправлено в General ({len(warnings)}):**\n" + "\n".join(warnings)
    if failed:
        report += f"\n\n❌ **Не отправлено в {len(failed)} чат(ов):**\n" + "\n".join(failed)

    for chunk_start in range(0, len(report), 3500):
        await target_msg.answer(report[chunk_start:chunk_start + 3500], parse_mode="Markdown")


@dp.callback_query(OutageState.confirming, F.data == "outsend")
async def outage_send(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    await callback.answer()
    try:
        await callback.message.edit_text(f"⏳ Отправляю в {len(d['out_selected_chat_ids'])} чат(ов)...")
    except Exception:
        pass
    await send_outage(callback.message, d)


# ---------- восстановление сбоя на площадке: по валютам и каналам ----------
def outage_active_currencies(incident: dict, selected_ids=None) -> list:
    return pairs_keys(incident, selected_ids, incident.get("currencies"))


def outage_active_channels(incident: dict, selected_ids, currencies) -> list:
    found = set()
    for m in incident.get("messages", []):
        cid = str(m.get("chat_key", m.get("chat_id")))
        if cid not in selected_ids:
            continue
        for cur, chs in (m.get("pairs") or {}).items():
            if cur in currencies:
                found.update(chs)
    return normalize_channels(found)


def outage_currency_counts(incident: dict, selected_ids) -> dict:
    counts = {}
    for m in incident.get("messages", []):
        cid = str(m.get("chat_key", m.get("chat_id")))
        if cid not in selected_ids:
            continue
        for cur in (m.get("pairs") or {}):
            counts[cur] = counts.get(cur, 0) + 1
    return counts


def build_outage_resolve_currency_keyboard(available: list, selected: list, counts: dict):
    sel = set(selected)
    btns = [InlineKeyboardButton(text=f"{'✅' if c in sel else '❌'} {c} ({counts.get(c, 0)})", callback_data=f"outrcurtg_{c}")
            for c in available]
    rows = _grid3(btns)
    rows.append([InlineKeyboardButton(text="➡️ ДАЛЕЕ", callback_data="outrcur_done")])
    rows.append([InlineKeyboardButton(text="✅ Всё восстановилось (все валюты и каналы)", callback_data="outrcur_all")])
    rows.append(cancel_button_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_outage_resolve_channels_keyboard(available: list, selected: list):
    sel = set(normalize_channels(selected))
    rows = [[InlineKeyboardButton(text=f"{'✅' if ch in sel else '❌'} {CHANNEL_BUTTON_LABELS[ch]}", callback_data=f"outrchtg_{ch}")]
            for ch in available]
    rows.append([InlineKeyboardButton(text="➡️ ПОДТВЕРДИТЬ ВЫБРАННОЕ", callback_data="outrchan_done")])
    if len(available) > 1:
        rows.append([InlineKeyboardButton(text="✅ Оба канала (сразу)", callback_data="outrchan_both")])
    rows.append(cancel_button_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start_outage_resolve(callback: types.CallbackQuery, state: FSMContext, incident: dict, selected_ids: list):
    avail = outage_active_currencies(incident, selected_ids)
    if not avail:
        await state.clear()
        return await callback.message.edit_text("ℹ️ В выбранных чатах уже нет активных ограничений.")

    await state.update_data(or_available_currencies=avail, or_selected_currencies=[], or_selected_channels=[])
    if len(avail) == 1:
        # валюта одна - спрашивать нечего
        await state.update_data(or_selected_currencies=avail)
        return await outage_resolve_after_currencies(callback, state, incident, selected_ids, avail)

    await state.set_state(ResolveState.selecting_outage_currencies)
    counts = outage_currency_counts(incident, selected_ids)
    await callback.message.edit_text(
        "Какие валюты восстановились? Отметьте то, что реально заработало (или нажмите «Всё восстановилось»). "
        "В скобках - сколько выбранных мерчантов ещё ограничено:",
        reply_markup=build_outage_resolve_currency_keyboard(avail, [], counts)
    )


async def outage_resolve_after_currencies(callback, state, incident, selected_ids, currencies):
    chans = outage_active_channels(incident, selected_ids, currencies)
    if len(chans) <= 1:
        # активен один канал - спрашивать нечего
        return await run_outage_resolve(callback, state, currencies, chans)
    await state.update_data(or_available_channels=chans, or_selected_channels=[])
    await state.set_state(ResolveState.selecting_outage_channels)
    await callback.message.edit_text(
        f"Валюты: **{', '.join(currencies)}**.\nПо каким каналам восстановилась работа? (применится ко всем отмеченным валютам)",
        parse_mode="Markdown",
        reply_markup=build_outage_resolve_channels_keyboard(chans, [])
    )


def _find_incident(data: dict, inc_id: int):
    return next((x for x in data.get("active_incidents", []) if x["id"] == inc_id), None)


@dp.callback_query(ResolveState.selecting_outage_currencies, F.data.startswith("outrcurtg_"))
async def outage_resolve_toggle_currency(callback: types.CallbackQuery, state: FSMContext):
    cur = callback.data[len("outrcurtg_"):]
    d = await state.get_data()
    avail = d["or_available_currencies"]
    if cur not in avail:
        return await callback.answer()
    selected = list(d["or_selected_currencies"])
    if cur in selected:
        selected.remove(cur)
    else:
        selected.append(cur)
    selected = [c for c in avail if c in selected]
    await state.update_data(or_selected_currencies=selected)
    incident = _find_incident(load_data(), d["resolve_inc_id"])
    counts = outage_currency_counts(incident, [str(x) for x in d["selected_resolve_ids"]]) if incident else {}
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=build_outage_resolve_currency_keyboard(avail, selected, counts))


@dp.callback_query(ResolveState.selecting_outage_currencies, F.data == "outrcur_done")
async def outage_resolve_currencies_done(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    selected = d["or_selected_currencies"]
    if not selected:
        return await callback.answer("⚠️ Отметьте хотя бы одну восстановившуюся валюту!", show_alert=True)
    incident = _find_incident(load_data(), d["resolve_inc_id"])
    if not incident:
        await state.clear()
        return await callback.message.edit_text("❌ Ошибка: Инцидент уже закрыт.")
    await callback.answer()
    await outage_resolve_after_currencies(callback, state, incident, [str(x) for x in d["selected_resolve_ids"]], selected)


@dp.callback_query(ResolveState.selecting_outage_currencies, F.data == "outrcur_all")
async def outage_resolve_all(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await callback.answer()
    await run_outage_resolve(callback, state, d["or_available_currencies"], list(CHANNEL_ORDER))


@dp.callback_query(ResolveState.selecting_outage_channels, F.data.startswith("outrchtg_"))
async def outage_resolve_toggle_channel(callback: types.CallbackQuery, state: FSMContext):
    ch = callback.data[len("outrchtg_"):]
    d = await state.get_data()
    avail = normalize_channels(d["or_available_channels"])
    if ch not in avail:
        return await callback.answer()
    selected = normalize_channels(d.get("or_selected_channels", []))
    if ch in selected:
        selected.remove(ch)
    else:
        selected.append(ch)
    selected = normalize_channels(selected)
    await state.update_data(or_selected_channels=selected)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=build_outage_resolve_channels_keyboard(avail, selected))


@dp.callback_query(ResolveState.selecting_outage_channels, F.data == "outrchan_done")
async def outage_resolve_channels_done(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    selected = normalize_channels(d.get("or_selected_channels", []))
    if not selected:
        return await callback.answer("⚠️ Отметьте хотя бы один восстановившийся канал!", show_alert=True)
    await callback.answer()
    await run_outage_resolve(callback, state, d["or_selected_currencies"], selected)


@dp.callback_query(ResolveState.selecting_outage_channels, F.data == "outrchan_both")
async def outage_resolve_channels_both(callback: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await callback.answer()
    await run_outage_resolve(callback, state, d["or_selected_currencies"], list(CHANNEL_ORDER))


async def run_outage_resolve(callback: types.CallbackQuery, state: FSMContext, restored_currencies: list, restored_channels: list):
    """Отправляет восстановление по выбранным валютам и каналам. Мерчант получает ОДНО сообщение только про то,
    что у него реально восстановилось; остальное остаётся в просадке. Просадка закрывается, когда ограничений не осталось."""
    d = await state.get_data()
    inc_id = d["resolve_inc_id"]
    selected_ids = [str(x) for x in d["selected_resolve_ids"]]
    restored_currencies = list(restored_currencies)
    restored_channels = normalize_channels(restored_channels)

    data = load_data()
    incident = _find_incident(data, inc_id)
    if not incident or not incident.get("messages"):
        await state.clear()
        return await callback.message.edit_text("❌ Ошибка: Инцидент уже закрыт.")

    resolved_count = partial_count = skipped_count = 0
    failed, warnings, remaining_messages = [], [], []

    for item in incident["messages"]:
        cid_str = str(item.get("chat_key", item["chat_id"]))
        if cid_str not in selected_ids:
            remaining_messages.append(item)
            continue

        pairs = {c: normalize_channels(chs) for c, chs in (item.get("pairs") or {}).items()}
        restored_pairs, new_pairs = {}, {}
        for cur, chs in pairs.items():
            if cur in restored_currencies:
                r = [ch for ch in chs if ch in restored_channels]
                rest = [ch for ch in chs if ch not in restored_channels]
            else:
                r, rest = [], chs
            if r:
                restored_pairs[cur] = r
            if rest:
                new_pairs[cur] = rest

        if not restored_pairs:
            skipped_count += 1
            remaining_messages.append(item)
            continue

        lang = item.get("lang", "RU")
        name = item.get("name") or data.get("chats", {}).get(cid_str, {}).get("name", cid_str)
        tags = data.get("chats", {}).get(cid_str, {}).get("tags", [])
        chat_id = item.get("chat_id")
        thread_id = item.get("thread_id")
        if not chat_id:
            chat_id, thread_id = parse_chat_id(cid_str)

        text = render_outage_resolve(lang, restored_pairs, new_pairs)
        if tags:
            text += f"\n\n📌 **cc:** {' '.join(escape_md(t) for t in tags)}"

        sent_ok = False
        fell_back = False
        try:
            _, _, fell_back = await send_with_thread_fallback(chat_id, thread_id, text, reply_to_message_id=item["message_id"])
            sent_ok = True
        except Exception as e:
            log_error(f"[outage resolve reply] {cid_str} ({name}): {e}")
            try:
                _, _, fell_back = await send_with_thread_fallback(chat_id, thread_id, text)
                sent_ok = True
            except Exception as ex:
                log_error(f"[outage resolve plain] {cid_str} ({name}): {ex}")
                failed.append(f"• `{cid_str}` ({escape_md(name)}): {escape_md(str(ex))}")

        if not sent_ok:
            remaining_messages.append(item)  # не отправилось - оставляем как было
            continue

        resolved_count += 1
        if fell_back:
            warnings.append(f"• `{cid_str}` ({escape_md(name)}): ветка недоступна, ушло в General")
        if new_pairs:
            item["pairs"] = new_pairs
            remaining_messages.append(item)
            partial_count += 1
        await asyncio.sleep(0.05)

    head = f"**🛠 {escape_md(incident.get('label') or 'Сбой на площадке')}**"
    if remaining_messages:
        incident["messages"] = remaining_messages
        active = outage_active_currencies(incident)
        incident["currency"] = ", ".join(active)
        status = f"🟢 Восстановление по {head} зафиксировано в {resolved_count} чат(ах)."
        if partial_count:
            status += f"\n📌 Из них частично (часть валют/каналов ещё ограничена): **{partial_count}**."
        if skipped_count:
            status += f"\nℹ️ Не затронуты выбранным и остались как были: **{skipped_count}**."
        status += f"\n⚠️ Осталось чатов в этой просадке: **{len(remaining_messages)}**."
        if active:
            parts = []
            for cur in active:
                chans = set()
                for m in remaining_messages:
                    chans.update((m.get("pairs") or {}).get(cur, []))
                parts.append(f"{cur} {channel_icons(chans)}")
            status += "\nАктивно: " + ", ".join(parts)
    else:
        data["active_incidents"] = [x for x in data["active_incidents"] if x["id"] != inc_id]
        status = f"🟢 {head} полностью закрыта!"

    if failed:
        status += f"\n\n❌ **Не удалось отправить восстановление в {len(failed)} чат(ов):**\n" + "\n".join(failed)
    if warnings:
        status += f"\n\n⚠️ **Автоматически перенаправлено в General ({len(warnings)}):**\n" + "\n".join(warnings)

    commit_incident_state(inc_id, incident, closed=not remaining_messages)
    await state.clear()

    for chunk_start in range(0, len(status), 3500):
        if chunk_start == 0:
            await callback.message.edit_text(status[:3500], parse_mode="Markdown")
        else:
            await callback.message.answer(status[chunk_start:chunk_start + 3500], parse_mode="Markdown")


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
        label = inc.get("label") or inc.get("provider") or "без названия"
        icons = channel_icons(active_channels(inc))
        icons_part = f"{icons} " if icons else ""
        text += f"• `id {inc['id']}` {icons_part}**{escape_md(incident_currency_label(inc))}** ({escape_md(label)}) — активна в {msg_count} чат(ах)\n"

    buttons = []
    for inc in active:
        label = inc.get("label") or inc.get("provider") or "без названия"
        buttons.append([
            InlineKeyboardButton(text=f"🔍 Подробнее id {inc['id']}", callback_data=f"incdetails_{inc['id']}"),
            InlineKeyboardButton(text=f"🗑 Удалить id {inc['id']}", callback_data=f"silentremove_{inc['id']}")
        ])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("incdetails_"))
async def show_incident_details(callback: types.CallbackQuery):
    """Показывает подробности инцидента: название, валюту, что видят мерчанты, тип оповещения и сам текст."""
    if not is_authorized(callback.from_user.id):
        return await callback.answer("🛑 Нет доступа", show_alert=True)

    inc_id = int(callback.data.split("_")[1])
    data = load_data()
    incident = next((x for x in data.get("active_incidents", []) if x["id"] == inc_id), None)

    if not incident:
        return await callback.answer("Уже закрыт или не найден", show_alert=True)

    await callback.answer()

    details = build_incident_details(incident, data.get("chats", {}))

    for chunk_start in range(0, len(details), 3500):
        chunk = details[chunk_start:chunk_start + 3500]
        try:
            await callback.message.answer(chunk, parse_mode="Markdown")
        except Exception:
            # если разметка сломалась (например, из-за разрезания длинного текста) - отправляем без форматирования
            await callback.message.answer(chunk)


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
        f"🗑 Просадка **{escape_md(incident_currency_label(incident))} ({escape_md(incident.get('label') or incident.get('provider') or 'без названия')})** удалена из списка активных.\n"
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
