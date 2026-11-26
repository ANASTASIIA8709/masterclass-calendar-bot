"""
Бот-расписание: клиент выбирает дату и время, запись сохраняется в Google Calendar.
Показывает свободные слоты на ближайшие 2 недели.
Утренний дайджест в 9:05 MSK.

Переменные окружения:
  BOT_TOKEN           - токен от @BotFather
  OWNER_ID            - ваш Telegram ID для утреннего дайджеста
  CALENDAR_ID         - ID календаря Google (или 'primary')
  GOOGLE_CREDENTIALS_JSON - JSON service account в одну строку
  SLOTS_PER_DAY       - слоты через запятую, например: 10:00,14:00,17:00
  AVAILABLE_DAYS      - дни недели через запятую 0=пн..6=вс, например: 0,1,2,3,4

TODO: добавьте Service Account как редактора в настройках вашего Google Calendar.
"""

import asyncio, os, json, logging
from datetime import datetime, timedelta, date, timezone
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from google.oauth2 import service_account
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO)

BOT_TOKEN        = os.environ["BOT_TOKEN"]
OWNER_ID         = int(os.environ.get("OWNER_ID", "0"))
CALENDAR_ID      = os.environ.get("CALENDAR_ID", "primary")
CREDS_JSON       = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
_slots_env       = os.environ.get("SLOTS_PER_DAY", "10:00,13:00,16:00")
SLOTS_PER_DAY    = [s.strip() for s in _slots_env.split(",") if s.strip()]
_days_env        = os.environ.get("AVAILABLE_DAYS", "0,1,2,3,4")
AVAILABLE_DAYS   = [int(d.strip()) for d in _days_env.split(",") if d.strip()]

MSK = timezone(timedelta(hours=3))


def get_calendar_service():
    if not CREDS_JSON:
        raise ValueError("GOOGLE_CREDENTIALS_JSON не задан")
    creds_data = json.loads(CREDS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_data,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    return build("calendar", "v3", credentials=creds)


def free_slots(days_ahead: int = 14) -> list[tuple[date, str]]:
    """Возвращает слоты, не занятые в Google Calendar."""
    svc   = get_calendar_service()
    today = date.today()
    end   = today + timedelta(days=days_ahead)

    events = svc.events().list(
        calendarId=CALENDAR_ID,
        timeMin=today.isoformat() + "T00:00:00Z",
        timeMax=end.isoformat()   + "T23:59:59Z",
        singleEvents=True,
    ).execute().get("items", [])

    busy = {
        (e["start"].get("dateTime", "")[:10],
         e["start"].get("dateTime", "")[11:16])
        for e in events
    }

    slots = []
    d = today + timedelta(days=1)
    while d <= end:
        if d.weekday() in AVAILABLE_DAYS:
            for t in SLOTS_PER_DAY:
                if (str(d), t) not in busy:
                    slots.append((d, t))
        d += timedelta(days=1)
    return slots[:20]


def get_today_events() -> list[dict]:
    svc   = get_calendar_service()
    today = date.today()
    events = svc.events().list(
        calendarId=CALENDAR_ID,
        timeMin=today.isoformat() + "T00:00:00+03:00",
        timeMax=today.isoformat() + "T23:59:59+03:00",
        singleEvents=True,
        orderBy="startTime",
    ).execute().get("items", [])
    return events


def get_week_events() -> list[dict]:
    svc   = get_calendar_service()
    today = date.today()
    week_end = today + timedelta(days=7)
    events = svc.events().list(
        calendarId=CALENDAR_ID,
        timeMin=today.isoformat() + "T00:00:00+03:00",
        timeMax=week_end.isoformat() + "T23:59:59+03:00",
        singleEvents=True,
        orderBy="startTime",
    ).execute().get("items", [])
    return events


def create_event(dt_str: str, name: str, phone: str):
    svc   = get_calendar_service()
    start = datetime.fromisoformat(dt_str)
    end   = start + timedelta(hours=1)
    svc.events().insert(
        calendarId=CALENDAR_ID,
        body={
            "summary":     f"Запись: {name}",
            "description": f"Телефон: {phone}",
            "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Moscow"},
            "end":   {"dateTime": end.isoformat(),   "timeZone": "Europe/Moscow"},
        },
    ).execute()


def format_events(events: list[dict]) -> str:
    if not events:
        return "Событий нет."
    lines = []
    for e in events:
        summary = e.get("summary", "(без названия)")
        start = e["start"].get("dateTime", e["start"].get("date", ""))
        if "T" in start:
            dt = datetime.fromisoformat(start)
            time_str = dt.strftime("%d.%m %H:%M")
        else:
            time_str = start
        lines.append(f"• {time_str} - {summary}")
    return "\n".join(lines)


bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher()


class Book(StatesGroup):
    slot  = State()
    name  = State()
    phone = State()


@dp.message(CommandStart())
async def start(msg: types.Message, state: FSMContext):
    try:
        slots = free_slots()
    except Exception as e:
        await msg.answer(f"⚠️ Ошибка подключения к Calendar: {e}\n\nПроверь переменную GOOGLE_CREDENTIALS_JSON.")
        return
    if not slots:
        await msg.answer("Свободных слотов пока нет. Напиши мне напрямую.")
        return
    await state.update_data(slots=[(str(d), t) for d, t in slots])
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text=f"{d.strftime('%d.%m %a')} в {t}",
            callback_data=f"slot:{i}",
        )]
        for i, (d, t) in enumerate(slots)
    ])
    await state.set_state(Book.slot)
    await msg.answer("Выбери удобное время:", reply_markup=kb)


@dp.message(Command("today"))
async def cmd_today(msg: types.Message):
    try:
        events = get_today_events()
        text = "📅 <b>Сегодня:</b>\n\n" + format_events(events)
    except Exception as e:
        text = f"❌ Ошибка: {e}"
    await msg.answer(text)


@dp.message(Command("week"))
async def cmd_week(msg: types.Message):
    try:
        events = get_week_events()
        text = "📅 <b>Ближайшая неделя:</b>\n\n" + format_events(events)
    except Exception as e:
        text = f"❌ Ошибка: {e}"
    await msg.answer(text)


@dp.callback_query(Book.slot, F.data.startswith("slot:"))
async def pick_slot(call: types.CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[1])
    data = await state.get_data()
    chosen = data["slots"][idx]
    await state.update_data(chosen=chosen)
    await state.set_state(Book.name)
    await call.message.answer(f"Выбрано: {chosen[0]} в {chosen[1]}\n\nКак тебя зовут?")
    await call.answer()


@dp.message(Book.name)
async def get_name(msg: types.Message, state: FSMContext):
    await state.update_data(name=msg.text)
    await state.set_state(Book.phone)
    await msg.answer("Телефон для подтверждения:")


@dp.message(Book.phone)
async def get_phone(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    d, t = data["chosen"]
    dt_str = f"{d}T{t}:00+03:00"
    try:
        create_event(dt_str, data["name"], msg.text)
        await msg.answer(
            f"✅ Записала!\n{d} в {t}\nИмя: {data['name']}\n"
            "Жди подтверждения - напишу за день до встречи."
        )
    except Exception as e:
        await msg.answer(f"Ошибка при записи: {e}")


async def morning_digest():
    """Утренний дайджест в 9:05 MSK."""
    if not OWNER_ID:
        return
    while True:
        now = datetime.now(MSK)
        # Следующий 9:05 MSK
        target = now.replace(hour=9, minute=5, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            events = get_today_events()
            text = "☀️ <b>Доброе утро! Расписание на сегодня:</b>\n\n" + format_events(events)
            await bot.send_message(OWNER_ID, text)
        except Exception as e:
            logging.error(f"Morning digest error: {e}")


async def main():
    asyncio.create_task(morning_digest())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
