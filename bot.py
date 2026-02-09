import os
import asyncio
from datetime import datetime, timezone
import requests

from fastapi import FastAPI
import uvicorn

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ---------------- ENV ----------------
TG_TOKEN = os.getenv("TG_TOKEN")
GEOAPIFY_KEY = os.getenv("GEOAPIFY_KEY")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", 10))

PORT = int(os.getenv("PORT", 10000))  # Render сам задаёт PORT

if not TG_TOKEN:
    raise RuntimeError("ENV TG_TOKEN not set")

if not GEOAPIFY_KEY:
    raise RuntimeError("ENV GEOAPIFY_KEY not set")


# ---------------- FASTAPI ----------------
app = FastAPI()


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "kp-aurora-bot",
        "check_interval_minutes": CHECK_INTERVAL_MINUTES
    }


# ---------------- BOT GLOBAL STATE ----------------
daily_tasks = {}      # chat_id -> asyncio task
stop_messages = {}    # chat_id -> message_id
is_active = {}        # chat_id -> bool
city_data = {}        # chat_id -> city info dict

telegram_app: Application | None = None


# ---------------- GEOAPIFY ----------------
def geocode_city_geoapify(city: str):
    url = "https://api.geoapify.com/v1/geocode/search"
    params = {
        "text": city,
        "apiKey": GEOAPIFY_KEY,
        "limit": 1,
        "format": "json"
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if "results" not in data or not data["results"]:
        return None

    first = data["results"][0]
    lat = float(first["lat"])
    lon = float(first["lon"])
    name = first.get("formatted", city)
    return lat, lon, name


# ---------------- Kp THRESHOLD BY LAT ----------------
def kp_by_lat(lat: float) -> int:
    if lat < 40:
        return 9
    elif 40 <= lat <= 45:
        return 8
    elif 46 <= lat <= 50:
        return 7
    elif 51 <= lat <= 55:
        return 6
    elif lat >= 56:
        return 5

    if 45 < lat < 46:
        return 7
    if 55 < lat < 56:
        return 5

    return 0


# ---------------- NOAA Kp ----------------
def get_latest_kp_noaa():
    url = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"

    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    if not isinstance(data, list) or not data:
        return None

    for row in reversed(data):
        if not isinstance(row, dict):
            continue

        time_str = row.get("time_tag")
        if not time_str:
            continue

        kp_val = row.get("estimated_kp")
        if kp_val is None:
            kp_val = row.get("kp_index")

        try:
            kp_val = float(kp_val)
        except Exception:
            continue

        try:
            dt_utc = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            try:
                dt_utc = datetime.fromisoformat(time_str).replace(tzinfo=timezone.utc)
            except Exception:
                continue

        return kp_val, dt_utc

    return None


# ---------------- AURORA PROBABILITY ----------------
def aurora_probability_text(kp_city: int, kp_noaa: float | None) -> str:
    if kp_noaa is None:
        return "Нет данных NOAA 😕"

    diff = kp_noaa - kp_city

    if diff >= 2:
        return "Очень высокая 🌌"
    elif diff >= 1:
        return "Высокая 🌠"
    elif diff >= -0.3:
        return "Средняя ✨"
    elif diff >= -1:
        return "Низкая 🌙"
    else:
        return "Очень низкая 🌑"


def aurora_above_threshold(kp_city: int, kp_noaa: float | None) -> bool:
    return kp_noaa is not None and kp_noaa >= kp_city


# ---------------- MESSAGE ----------------
def build_message(full_name: str, lat: float, lon: float, kp_city: int,
                  kp_noaa: float, kp_dt: datetime):
    prob = aurora_probability_text(kp_city, kp_noaa)

    return (
        f"📍 <b>{full_name}</b>\n"
        f"🔹 Широта: <i>{lat}</i>\n"
        f"🔹 Долгота: <i>{lon}</i>\n\n"
        f"🌍 NOAA Kp (последний): <b>{kp_noaa:.2f}</b>\n"
        f"🕒 NOAA (UTC): <i>{kp_dt.strftime('%Y-%m-%d %H:%M')}</i>\n"
        f"📍 Порог для города: <b>{kp_city}</b>\n"
        f"🔮 Вероятность сияния: <b>{prob}</b>\n\n"
        f"🗺️ <a href='https://maps.google.com/?q={lat},{lon}'>Открыть карту</a>"
    )


# ---------------- LOOP TASK ----------------
async def kp_checker_loop(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """
    Проверяет NOAA Kp каждые N минут.
    Если NOAA >= порога города — отправляет сообщение.
    """
    try:
        while is_active.get(chat_id, False):
            cd = city_data.get(chat_id)
            if not cd:
                await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)
                continue

            kp_data = await asyncio.to_thread(get_latest_kp_noaa)

            if kp_data:
                kp_noaa, kp_dt = kp_data

                if aurora_above_threshold(cd["kp_city"], kp_noaa):
                    msg = build_message(
                        full_name=cd["full_name"],
                        lat=cd["lat"],
                        lon=cd["lon"],
                        kp_city=cd["kp_city"],
                        kp_noaa=kp_noaa,
                        kp_dt=kp_dt
                    )

                    keyboard = InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🛑 STOP", callback_data="stop")]]
                    )

                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=msg,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        disable_web_page_preview=False
                    )

            await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)

    except asyncio.CancelledError:
        await context.bot.send_message(chat_id=chat_id, text="Рассылка остановлена ✅")
        raise


def stop_task(chat_id: int):
    task = daily_tasks.get(chat_id)
    if task and not task.done():
        task.cancel()

    daily_tasks.pop(chat_id, None)
    is_active[chat_id] = False


# ---------------- HANDLERS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 🛰️ Напиши название города.\n\n"
        f"Я проверяю NOAA Kp каждые {CHECK_INTERVAL_MINUTES} минут.\n"
        "Сообщение придёт только если NOAA Kp >= порога твоего города.\n\n"
        "Кнопка 🛑 STOP всегда доступна."
    )


async def handle_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    city = update.message.text.strip()

    stop_task(chat_id)

    result = await asyncio.to_thread(geocode_city_geoapify, city)
    if not result:
        await update.message.reply_text("Не нашёл такой город 😕 Попробуй другое название.")
        return

    lat, lon, full_name = result
    kp_city = kp_by_lat(lat)

    city_data[chat_id] = {
        "city": city,
        "lat": lat,
        "lon": lon,
        "full_name": full_name,
        "kp_city": kp_city
    }

    await update.message.reply_text(
        f"Город установлен: {full_name}\n"
        f"Порог Kp: {kp_city}\n\n"
        f"Я буду присылать сообщения только если NOAA Kp >= {kp_city}."
    )

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛑 STOP", callback_data="stop")]]
    )

    if chat_id not in stop_messages:
        message = await update.message.reply_text(
            "🛑 STOP всегда доступен 👇",
            reply_markup=keyboard
        )
        stop_messages[chat_id] = message.message_id
    else:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=stop_messages[chat_id],
            reply_markup=keyboard
        )

    is_active[chat_id] = True
    task = asyncio.create_task(kp_checker_loop(chat_id, context))
    daily_tasks[chat_id] = task


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id

    if query.data == "stop":
        if is_active.get(chat_id, False):
            stop_task(chat_id)
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=stop_messages.get(chat_id, query.message.message_id),
                text="Рассылка остановлена ✅\nЧтобы запустить снова — напиши город ещё раз."
            )
        else:
            await query.message.reply_text("Рассылка уже остановлена ✅")


# ---------------- START TELEGRAM ----------------
async def start_telegram():
    global telegram_app

    telegram_app = Application.builder().token(TG_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_city))
    telegram_app.add_handler(CallbackQueryHandler(button_callback))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()

    print("Telegram polling started")


async def stop_telegram():
    global telegram_app
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()


# ---------------- FASTAPI LIFESPAN ----------------
@app.on_event("startup")
async def on_startup():
    asyncio.create_task(start_telegram())


@app.on_event("shutdown")
async def on_shutdown():
    await stop_telegram()


# ---------------- MAIN ----------------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
