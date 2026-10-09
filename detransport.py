import logging
import math
import os
from config import BOT_TOKEN

import aiohttp
from telegram import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ConversationHandler,
)

# Налаштування логування
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BASE_URL = "http://api.detransport.com.ua/vehicles/info/"
STOPS_URL = "http://api.detransport.com.ua/stops/list/"

HEADERS = {
    'User-Agent': 'okhttp/2.5.0',
    'Content-Type': 'application/x-www-form-urlencoded'
}

# Стани для ConversationHandler
TYPING_STOP = 1

# Сховища в пам'яті:
# user_favorites = {user_id: [ { 'id': stop_id, 'name': stop_name }, ... ]}
user_favorites = {}
# user_favorite_routes = {user_id: {stop_id: [route_number, ...], ...}}
user_favorite_routes = {}


# --- Функція визначення типу транспорту ---
def get_transport_type_info(v_type):
    """Повертає назву та емодзі залежно від типу транспорту."""
    try:
        v_type_int = int(v_type)
    except (ValueError, TypeError):
        v_type_int = None

    if v_type_int == 1:
        return "Автобус", "🚌"
    elif v_type_int == 2:
        return "Тролейбус", "🚎"
    else:
        return "Транспорт", "🚊"


# --- Оригінальні функції API та математики ---

async def get_all_stops():
    """Завантажує список усіх зупинок."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(STOPS_URL, headers=HEADERS, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('stops', [])
                return []
        except Exception as e:
            logger.error(f"Помилка завантаження зупинок: {e}")
            return []


def haversine_distance(lat1, lon1, lat2, lon2):
    """Обчислює відстань у метрах між координатами."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def find_nearest_stops(stops, user_lat, user_lng, limit=3):
    """Знаходить найближчі зупинки за координатами."""
    stops_with_dist = []
    for s in stops:
        try:
            dist_m = haversine_distance(user_lat, user_lng, float(s['lat']), float(s['lng']))
            stops_with_dist.append({'stop': s, 'dist_m': dist_m})
        except (ValueError, KeyError):
            continue
    stops_with_dist.sort(key=lambda x: x['dist_m'])
    return stops_with_dist[:limit]


def search_stops(stops, search_query):
    """Шукає зупинки за назвою."""
    search_query = search_query.strip().lower()
    return [s for s in stops if search_query in s.get('name', '').lower()]


async def get_vehicles_for_stop(stop_id):
    """Отримує транспорт для зупинки."""
    async with aiohttp.ClientSession() as session:
        try:
            payload = {'stop': str(stop_id)}
            async with session.post(BASE_URL, headers=HEADERS, data=payload, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('vehicles', [])
                return []
        except Exception as e:
            logger.error(f"Помилка завантаження транспорту: {e}")
            return []


# --- Головне меню ---
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔍 Ввести зупинку"), KeyboardButton("⭐ Улюблені зупинки")],
            [KeyboardButton("📍 Поділитися координатами", request_location=True)]
        ],
        resize_keyboard=True
    )


# --- Генерація тексту та 2 головних кнопок для зупинки ---
async def build_stop_response(user_id, stop_id, stop_name, extra_dist_text="", show_only_fav_routes=False):
    vehicles = await get_vehicles_for_stop(stop_id)
    user_fav_routes = user_favorite_routes.get(user_id, {}).get(stop_id, [])

    response_text = f"🚏 **{stop_name}**{extra_dist_text}\n"

    if vehicles:
        vehicles.sort(key=lambda x: x.get('time', 0))

        if show_only_fav_routes:
            vehicles = [
                v for v in vehicles
                if str(v.get('name') or v.get('number') or v.get('route') or v.get('route_name') or v.get(
                    'title') or '?') in user_fav_routes
            ]
            response_text += "⭐ **Улюблені маршрути:**\n"

        if vehicles:
            for v in vehicles:
                route = str(
                    v.get('name') or v.get('number') or v.get('route') or v.get('route_name') or v.get('title') or '?')
                time_min = round(v.get('time', 0) / 60)

                v_type = v.get('type')
                t_name, t_emoji = get_transport_type_info(v_type)

                # Формуємо базовий рядок для першого прибуття
                line = f" • {t_emoji} {t_name} {route}: через {time_min} хв"

                # Додаємо час наступного рейсу (timenext), якщо він наявний в API
                timenext = v.get('timenext')
                if timenext is not None:
                    try:
                        timenext_min = round(int(timenext) / 60)
                        line += f" (-> {timenext_min} хв)"
                    except (ValueError, TypeError):
                        pass

                response_text += line + "\n"
        else:
            if show_only_fav_routes:
                response_text += "⚠️ У вас немає улюблених маршрутів на цій зупинці або вони зараз не їдуть.\n"
            else:
                response_text += " • Немає даних про транспорт\n"
    else:
        response_text += " • Немає даних про транспорт\n"

    favs = user_favorites.get(user_id, [])
    is_favorite_stop = any(f['id'] == stop_id for f in favs)

    if is_favorite_stop:
        stop_btn = InlineKeyboardButton("🗑 Видалити зупинку з улюблених", callback_data=f"fav_del_{stop_id}")
    else:
        stop_btn = InlineKeyboardButton("⭐ Додати в улюблені", callback_data=f"fav_add_{stop_id}")

    bus_btn = InlineKeyboardButton("🚌 Додати автобус в улюблені", callback_data=f"menu_buses_{stop_id}")

    refresh_flag = "1" if show_only_fav_routes else "0"
    refresh_btn = InlineKeyboardButton("🔄 Оновити", callback_data=f"refresh_stop_{stop_id}_{refresh_flag}")

    keyboard = InlineKeyboardMarkup([
        [refresh_btn],
        [stop_btn],
        [bus_btn]
    ])

    return response_text, keyboard

# --- Логіка Telegram-бота ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник команди /start."""
    await update.message.reply_text(
        "Привіт! Я бот громадського транспорту.\nОберіть дію за допомогою кнопок нижче:",
        reply_markup=get_main_keyboard()
    )


async def ask_stop_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запит назви зупинки у користувача."""
    await update.message.reply_text(
        "Введіть назву зупинки (або її частину):",
        reply_markup=ReplyKeyboardRemove()
    )
    return TYPING_STOP


async def handle_text_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка введеного тексту зупинки."""
    user_id = update.message.from_user.id
    query = update.message.text
    stops = await get_all_stops()
    found = search_stops(stops, query)

    if not found:
        await update.message.reply_text(
            "❌ Зупинок за таким запитом не знайдено. Спробуйте ще раз.",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END

    await update.message.reply_text(f"Знайдено зупинок: {len(found)}", reply_markup=get_main_keyboard())

    for s in found[:3]:
        stop_id = str(s.get('id'))
        stop_name = s.get('name')

        text, kb = await build_stop_response(user_id, stop_id, stop_name)
        try:
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
        except Exception as e:
            # Якщо виникне помилка парсингу Markdown через спецсимволи, надсилаємо без форматування
            logger.error(f"Помилка відправки повідомлення зупинки: {e}")
            await update.message.reply_text(text, reply_markup=kb)

    return ConversationHandler.END


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка координат та виведення найближчих зупинок."""
    user_id = update.message.from_user.id
    location = update.message.location
    stops = await get_all_stops()
    nearest = find_nearest_stops(stops, location.latitude, location.longitude, limit=3)

    if not nearest:
        await update.message.reply_text("❌ Не вдалося знайти найближчі зупинки.", reply_markup=get_main_keyboard())
        return

    await update.message.reply_text("📍 **Найближчі зупинки та транспорт:**", parse_mode="Markdown",
                                    reply_markup=get_main_keyboard())

    for item in nearest:
        s = item['stop']
        dist = int(item['dist_m'])
        stop_id = str(s.get('id'))
        stop_name = s.get('name')

        text, kb = await build_stop_response(user_id, stop_id, stop_name, extra_dist_text=f" (~{dist} м)")
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def show_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує список улюблених зупинок."""
    user_id = update.message.from_user.id
    favs = user_favorites.get(user_id, [])

    if not favs:
        await update.message.reply_text(
            "⭐ У вас поки немає улюблених зупинок.\nДодайте їх через пошук або геопозицію!",
            reply_markup=get_main_keyboard()
        )
        return

    keyboard = []
    for f in favs:
        keyboard.append([InlineKeyboardButton(f['name'], callback_data=f"fav_show_{f['id']}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "⭐ **Ваші улюблені зупинки:**\nОберіть зупинку, щоб переглянути улюблені автобуси:",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка натискань інлайн-кнопок."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data.startswith("refresh_stop_"):
        rest = data.removeprefix("refresh_stop_")
        stop_id, flag = rest.rsplit("_", 1)
        show_favs_flag = (flag == "1")

        stops = await get_all_stops()
        stop_name = "Зупинка"
        for s in stops:
            if str(s.get('id')) == stop_id:
                stop_name = s.get('name', 'Зупинка')
                break

        text, kb = await build_stop_response(user_id, stop_id, stop_name, show_only_fav_routes=show_favs_flag)
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            pass
        await query.answer("Дані оновлено!", show_alert=False)

    elif data.startswith("fav_add_"):
        stop_id = data.removeprefix("fav_add_")

        stops = await get_all_stops()
        stop_name = "Зупинка"
        for s in stops:
            if str(s.get('id')) == stop_id:
                stop_name = s.get('name', 'Зупинка')
                break

        if user_id not in user_favorites:
            user_favorites[user_id] = []

        if not any(f['id'] == stop_id for f in user_favorites[user_id]):
            user_favorites[user_id].append({'id': stop_id, 'name': stop_name})

            text, kb = await build_stop_response(user_id, stop_id, stop_name)
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
            await query.message.reply_text(f"✅ Зупинку **{stop_name}** додано до улюблених!", parse_mode="Markdown",
                                           reply_markup=get_main_keyboard())
        else:
            await query.answer("Ця зупинка вже є в улюблених!", show_alert=True)

    elif data.startswith("fav_del_"):
        stop_id = data.removeprefix("fav_del_")

        if user_id in user_favorites:
            user_favorites[user_id] = [f for f in user_favorites[user_id] if f['id'] != stop_id]
        if user_id in user_favorite_routes and stop_id in user_favorite_routes[user_id]:
            del user_favorite_routes[user_id][stop_id]

        await query.message.delete()
        await query.message.reply_text("❌ Зупинку видалено з улюблених.", parse_mode="Markdown",
                                       reply_markup=get_main_keyboard())

    elif data.startswith("menu_buses_"):
        stop_id = data.removeprefix("menu_buses_")

        stops = await get_all_stops()
        stop_name = "Зупинка"
        for s in stops:
            if str(s.get('id')) == stop_id:
                stop_name = s.get('name', 'Зупинка')
                break

        vehicles = await get_vehicles_for_stop(stop_id)
        user_fav_routes = user_favorite_routes.get(user_id, {}).get(stop_id, [])

        if not vehicles:
            await query.answer("Наразі немає даних про маршрути на цій зупинці.", show_alert=True)
            return

        unique_routes = {}
        for v in vehicles:
            r = str(v.get('name') or v.get('number') or v.get('route') or v.get('route_name') or v.get('title') or '?')
            t_name, t_emoji = get_transport_type_info(v.get('type'))
            unique_routes[r] = (t_name, t_emoji)

        bus_keyboard = []
        for r, (t_name, t_emoji) in sorted(unique_routes.items(), key=lambda x: x[0]):
            is_fav = r in user_fav_routes
            icon = "✅" if is_fav else "➕"
            btn_text = f"{icon} {t_emoji} {t_name} {r}"
            bus_keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"toggle_bus_{stop_id}_{r}")])

        bus_keyboard.append(
            [InlineKeyboardButton("⬅️ Назад до зупинки", callback_data=f"back_to_stop_{stop_id}")])

        await query.edit_message_text(
            f"🚌 **Керування улюбленими маршрутами** для зупинки:\n🚏 *{stop_name}*\n\nНатисніть на маршрут, щоб додати/видалити з улюблених:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(bus_keyboard)
        )

    elif data.startswith("toggle_bus_"):
        rest = data.removeprefix("toggle_bus_")
        stop_id, route_num = rest.split("_", 1)

        if user_id not in user_favorite_routes:
            user_favorite_routes[user_id] = {}
        if stop_id not in user_favorite_routes[user_id]:
            user_favorite_routes[user_id][stop_id] = []

        fav_routes = user_favorite_routes[user_id][stop_id]
        if route_num in fav_routes:
            fav_routes.remove(route_num)
        else:
            fav_routes.append(route_num)

        stops = await get_all_stops()
        stop_name = "Зупинка"
        for s in stops:
            if str(s.get('id')) == stop_id:
                stop_name = s.get('name', 'Зупинка')
                break

        vehicles = await get_vehicles_for_stop(stop_id)
        unique_routes = {}
        for v in vehicles:
            r = str(v.get('name') or v.get('number') or v.get('route') or v.get('route_name') or v.get('title') or '?')
            t_name, t_emoji = get_transport_type_info(v.get('type'))
            unique_routes[r] = (t_name, t_emoji)

        bus_keyboard = []
        for r, (t_name, t_emoji) in sorted(unique_routes.items(), key=lambda x: x[0]):
            is_fav = r in user_favorite_routes[user_id][stop_id]
            icon = "✅" if is_fav else "➕"
            btn_text = f"{icon} {t_emoji} {t_name} {r}"
            bus_keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"toggle_bus_{stop_id}_{r}")])

        bus_keyboard.append(
            [InlineKeyboardButton("⬅️ Назад до зупинки", callback_data=f"back_to_stop_{stop_id}")])

        await query.edit_message_text(
            f"🚌 **Керування улюбленими маршрутами** для зупинки:\n🚏 *{stop_name}*\n\nНатисніть на маршрут, щоб додати/видалити з улюблених:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(bus_keyboard)
        )

    elif data.startswith("back_to_stop_"):
        stop_id = data.removeprefix("back_to_stop_")

        stops = await get_all_stops()
        stop_name = "Зупинка"
        for s in stops:
            if str(s.get('id')) == stop_id:
                stop_name = s.get('name', 'Зупинка')
                break

        text, kb = await build_stop_response(user_id, stop_id, stop_name)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    elif data.startswith("fav_show_"):
        stop_id = data.removeprefix("fav_show_")

        favs = user_favorites.get(user_id, [])
        stop_name = next((f['name'] for f in favs if f['id'] == stop_id), "Зупинка")

        text, kb = await build_stop_response(user_id, stop_id, stop_name, show_only_fav_routes=True)
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

def main():
   #TOKEN = os.getenv("BOT_TOKEN")
    TOKEN = BOT_TOKEN

    app = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🔍 Ввести зупинку$"), ask_stop_name)],
        states={
            TYPING_STOP: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_search)]
        },
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.Regex("^⭐ Улюблені зупинки$"), show_favorites))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Бот запущено...")
    app.run_polling()


if __name__ == '__main__':
    main()
