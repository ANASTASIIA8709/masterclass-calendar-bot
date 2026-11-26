"""
Личный ИИ-секретарь с Google Календарём.
Пишешь «встреча с Ивановым в пятницу в 15:00» — бот добавляет в Google Calendar.
Спрашиваешь «что у меня сегодня?» — показывает расписание.

Переменные окружения:
  BOT_TOKEN                — токен от @BotFather (обязательно)
  OWNER_ID                 — твой Telegram ID (обязательно)
  ANTHROPIC_API_KEY        — ключ Claude API с console.anthropic.com
  CALENDAR_ID              — ID календаря или слово primary
  GOOGLE_CREDENTIALS_JSON  — JSON сервисного аккаунта одной строкой
"""

import asyncio, json, os, logging
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
import anthropic
from google.oauth2 import service_account
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN   = os.environ["BOT_TOKEN"]
OWNER_ID    = int(os.environ.get("OWNER_ID", "0"))
CLAUDE_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
CALENDAR_ID = os.environ.get("CALENDAR_ID", "primary")
CREDS_JSON  = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

MSK = timezone(timedelta(hours=3))
WEEKDAYS_RU = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher()


# ── Google Calendar ──────────────────────────────────────────

def _cal():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(CREDS_JSON),
        scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=creds)

def now_msk() -> datetime:
    return datetime.now(MSK)

def create_event(title: str, date: str, time: str = None, description: str = None) -> None:
    if time:
        start = {"dateTime": f"{date}T{time}:00", "timeZone": "Europe/Moscow"}
        end_dt = datetime.fromisoformat(f"{date}T{time}:00") + timedelta(hours=1)
        end = {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Europe/Moscow"}
    else:
        start = {"date": date}
        end   = {"date": (datetime.fromisoformat(date) + timedelta(days=1)).strftime("%Y-%m-%d")}
    body = {"summary": title, "start": start, "end": end}
    if description:
        body["description"] = description
    _cal().events().insert(calendarId=CALENDAR_ID, body=body).execute()

def get_events(date: str, days: int = 1) -> list:
    start = datetime.fromisoformat(date).replace(tzinfo=MSK)
    end   = start + timedelta(days=days)
    result = _cal().events().list(
        calendarId=CALENDAR_ID,
        timeMin=start.isoformat(), timeMax=end.isoformat(),
        singleEvents=True, orderBy="startTime", timeZone="Europe/Moscow"
    ).execute()
    return result.get("items", [])

def fmt_event(ev: dict) -> str:
    start = ev.get("start", {})
    t = datetime.fromisoformat(start["dateTime"]).strftime("%H:%M") if "dateTime" in start else "весь день"
    return f"• {t} — {ev.get('summary', '(без названия)')}"

MONTHS_RU = {1:"января",2:"февраля",3:"марта",4:"апреля",5:"мая",6:"июня",
             7:"июля",8:"августа",9:"сентября",10:"октября",11:"ноября",12:"декабря"}

def fmt_date_ru(date: str) -> str:
    d = datetime.fromisoformat(date)
    return f"{d.day} {MONTHS_RU[d.month]}"


# ── Claude NL-парсер ─────────────────────────────────────────

def parse_with_claude(text: str) -> dict:
    """Свободный текст → JSON с action/title/date/time."""
    now  = now_msk()
    today = now.strftime("%Y-%m-%d")
    wd    = WEEKDAYS_RU[now.weekday()]

    def nxt(wd_num: int) -> str:
        days = (wd_num - now.weekday() - 1) % 7 + 1
        return (now + timedelta(days=days)).strftime("%Y-%m-%d")

    prompt = f"""Сегодня {today} ({wd}), время {now.strftime("%H:%M")} МСК.
Завтра: {(now+timedelta(days=1)).strftime("%Y-%m-%d")}.
Ближайшие дни: пн={nxt(0)}, вт={nxt(1)}, ср={nxt(2)}, чт={nxt(3)}, пт={nxt(4)}, сб={nxt(5)}, вс={nxt(6)}.

Верни ТОЛЬКО JSON, без пояснений.
Если это добавление события:
  {{"action":"create","title":"название","date":"YYYY-MM-DD","time":"HH:MM или null","description":"заметка или null"}}
Если это просмотр расписания (что сегодня / завтра / на неделе и т.п.):
  {{"action":"list","date":"YYYY-MM-DD","days":1}}
  (days=7 если «на неделе»)

Сообщение: {text}"""

    client = anthropic.Anthropic(api_key=CLAUDE_KEY)
    resp   = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip().rstrip("```").strip()
    return json.loads(raw)


# ── Хелперы ──────────────────────────────────────────────────

def only_owner(msg: types.Message) -> bool:
    return OWNER_ID == 0 or msg.from_user.id == OWNER_ID

async def show_schedule(msg: types.Message, date: str, days: int, label: str):
    if not CREDS_JSON:
        await msg.answer("⚙️ Задай GOOGLE_CREDENTIALS_JSON и CALENDAR_ID в переменных Railway.")
        return
    try:
        events = get_events(date, days)
        if not events:
            await msg.answer(f"📅 Событий {label} нет.")
            return
        lines = [fmt_event(e) for e in events]
        await msg.answer(f"📅 <b>Расписание {label}:</b>\n\n" + "\n".join(lines))
    except Exception as e:
        await msg.answer(f"Ошибка Calendar: {e}")


# ── Хендлеры ─────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(msg: types.Message):
    if not only_owner(msg): return
    await msg.answer(
        "👋 Привет! Пиши мне как в чат:\n\n"
        "— <i>встреча с Ивановым в пятницу в 14:00</i>\n"
        "— <i>звонок в банк завтра в 10</i>\n"
        "— <i>что у меня сегодня?</i>\n"
        "— <i>что на этой неделе?</i>\n\n"
        "Или команды: /today /tomorrow /week"
    )

@dp.message(Command("today"))
async def cmd_today(msg: types.Message):
    if not only_owner(msg): return
    await show_schedule(msg, now_msk().strftime("%Y-%m-%d"), 1, "сегодня")

@dp.message(Command("tomorrow"))
async def cmd_tomorrow(msg: types.Message):
    if not only_owner(msg): return
    await show_schedule(msg, (now_msk()+timedelta(days=1)).strftime("%Y-%m-%d"), 1, "завтра")

@dp.message(Command("week"))
async def cmd_week(msg: types.Message):
    if not only_owner(msg): return
    await show_schedule(msg, now_msk().strftime("%Y-%m-%d"), 7, "на этой неделе")

@dp.message()
async def handle_text(msg: types.Message):
    if not only_owner(msg): return
    if not CLAUDE_KEY:
        await msg.answer("⚙️ Задай ANTHROPIC_API_KEY в переменных Railway.")
        return
    if not CREDS_JSON:
        await msg.answer("⚙️ Задай GOOGLE_CREDENTIALS_JSON и CALENDAR_ID в переменных Railway.")
        return
    try:
        parsed = parse_with_claude(msg.text or "")
        action = parsed.get("action")

        if action == "create":
            title = parsed["title"]
            date  = parsed["date"]
            time  = parsed.get("time")
            desc  = parsed.get("description")
            create_event(title, date, time, desc)
            date_ru = fmt_date_ru(date)
            suffix  = f", {time}" if time else ""
            await msg.answer(f"✅ Добавлено: <b>{title}</b>\n📅 {date_ru}{suffix}")

        elif action == "list":
            date = parsed.get("date", now_msk().strftime("%Y-%m-%d"))
            days = int(parsed.get("days", 1))
            await show_schedule(msg, date, days, "на запрошенный период")

        else:
            await msg.answer("Не понял. Попробуй: «встреча завтра в 15:00» или «что сегодня?»")

    except json.JSONDecodeError:
        await msg.answer("Не смог разобрать запрос — попробуй иначе.")
    except Exception as e:
        logger.error(f"handle_text error: {e}")
        await msg.answer(f"Ошибка: {e}")


async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
