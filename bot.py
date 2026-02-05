import asyncio
from datetime import datetime, timezone, timedelta
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

TG_TOKEN = "8539569873:AAFDqohhpCfEjP8qKS076fe6gxpnr2PmgPg"
GEOAPIFY_KEY = "b306a3bf83344450a9d62945269e112d"

# chat_id -> asyncio task
daily_tasks = {}
# chat_id -> message_id (STOP message)
stop_messages = {}
# chat_id -> bool
is_active = {}
# chat_id -> saved city info
# { "city": str, "lat": float, "lon": float, "full_name": str, "kp_city": int }
city_data = {}


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


# ---------------- CITY Kp BY LAT (your rule) ----------------
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

    # на всякий случай
    if 45 < lat < 46:
        return 7
    if 55 < lat < 56:
        return 5

    return 0


# ---------------- NOAA SWPC: latest real Kp ----------------
def get_latest_kp_noaa():
    """
    NOAA planetary K index 1-minute
    Returns: (kp_value: float, dt_utc: datetime) or None
    """
    url = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"

    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    if not isinstance(data, list) or not data:
        return None

    # иногда последние строки бывают "битые" — ищем с конца первую валидную
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

        # NOAA time_tag обычно: 2026-02-04T08:13:00
        try:
            dt_utc = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            try:
                dt_utc = datetime.fromisoformat(time_str).replace(tzinfo=timezone.utc)
            except Exception:
                continue

        return kp_val, dt_utc

    return None


# ---------------- AURORA THRESHOLD ----------------
def aurora_above_threshold(kp_city: int, kp_noaa: float | None) -> bool:
    if kp_noaa is None:
        return False
    return kp_noaa >= kp_city


def aurora_probability_text(kp_city: int, kp_noaa: float | None) -> str:
    """
    Реалистичный текст, основанный на разнице NOAA и порога города
    """
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


# ---------------- MESSAGE ----------------
def build_message_from_city_data(full_name: str, lat: float, lon: float, kp_city: int,
                                kp_noaa: float, kp_dt: datetime):
    prob = aurora_probability_text(kp_city, kp_noaa)

    return (
        f"📍 {full_name}\n"
        f"🔹 Широта: {lat}\n"
        f"🔹 Долгота: {lon}\n\n"
        f"🌍 NOAA Kp (последний): {kp_noaa:.2f}\n"
        f"🕒 Время NOAA (UTC): {kp_dt.strftime('%Y-%m-%d %H:%M')}\n"
        f"📍 Порог Kp для города: {kp_city}\n"
        f"🔮 Вероятность полярного сияния: {prob}\n\n"
        f"🗺️ https://maps.google.com/?q={lat},{lon}"
    )


# ---------------- DAILY SENDER ----------------
async def daily_sender(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """
    Каждый день в заданное время:
    - берём NOAA Kp
    - сравниваем с порогом города
    - отправляем сообщение только если NOAA >= порога
    """
    try:
        while is_active.get(chat_id, False):
            now = datetime.now()
            target = now.replace(hour=20, minute=0, second=0, microsecond=0)  # <-- ТУТ ТВОЁ ВРЕМЯ

            if now >= target:
                target += timedelta(days=1)

            wait_seconds = (target - now).total_seconds()
            await asyncio.sleep(wait_seconds)

            # если рассылку выключили пока ждали
            if not is_active.get(chat_id, False):
                break

            # есть ли данные города?
            cd = city_data.get(chat_id)
            if not cd:
                continue

            # NOAA Kp
            kp_data = await asyncio.to_thread(get_latest_kp_noaa)
            if not kp_data:
                continue

            kp_noaa, kp_dt = kp_data

            # отправляем только если NOAA >= порога города
            if aurora_above_threshold(cd["kp_city"], kp_noaa):
                msg = build_message_from_city_data(
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
                await context.bot.send_message(chat_id=chat_id, text=msg, reply_markup=keyboard)

    except asyncio.CancelledError:
        await context.bot.send_message(chat_id=chat_id, text="Ежедневная рассылка остановлена ✅")
        raise


def stop_daily_task(chat_id: int):
    task = daily_tasks.get(chat_id)
    if task and not task.done():
        task.cancel()

    daily_tasks.pop(chat_id, None)
    is_active[chat_id] = False


# ---------------- HANDLERS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 🛰️ Напиши название города.\n\n"
        "Я буду проверять NOAA Kp ежедневно в заданное время.\n"
        "Сообщение придёт ТОЛЬКО если вероятность северного сияния выше порога для твоего города.\n\n"
        "Кнопка 🛑 STOP будет всегда."
    )


async def handle_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    city = update.message.text.strip()

    # стопаем старую задачу
    stop_daily_task(chat_id)

    # геокодим город (один раз)
    result = await asyncio.to_thread(geocode_city_geoapify, city)
    if not result:
        await update.message.reply_text("Не нашёл такой город 😕 Попробуй другое название.")
        return

    lat, lon, full_name = result
    kp_city = kp_by_lat(lat)

    # сохраняем данные города
    city_data[chat_id] = {
        "city": city,
        "lat": lat,
        "lon": lon,
        "full_name": full_name,
        "kp_city": kp_city
    }

    await update.message.reply_text(
        f"Город установлен: {full_name}\n"
        f"Порог Kp для города: {kp_city}\n\n"
        f"Теперь я буду присылать сообщение только если NOAA Kp >= {kp_city}."
    )

    # создаём постоянную кнопку STOP
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛑 STOP", callback_data="stop")]]
    )

    if chat_id not in stop_messages:
        message = await update.message.reply_text(
            "🛑 STOP всегда доступен снизу 👇",
            reply_markup=keyboard
        )
        stop_messages[chat_id] = message.message_id
    else:
        # если сообщение уже было — просто обновим клавиатуру на всякий случай
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=stop_messages[chat_id],
            reply_markup=keyboard
        )

    # запускаем задачу
    is_active[chat_id] = True
    task = asyncio.create_task(daily_sender(chat_id, context))
    daily_tasks[chat_id] = task


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id

    if query.data == "stop":
        if is_active.get(chat_id, False):
            stop_daily_task(chat_id)
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=stop_messages.get(chat_id, query.message.message_id),
                text="Рассылка остановлена ✅\n(Чтобы запустить снова — просто напиши город ещё раз)"
            )
        else:
            await query.message.reply_text("Рассылка уже остановлена ✅")


def main():
    app = Application.builder().token(TG_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_city))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
