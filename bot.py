
import asyncio
import re
from datetime import date

from aiogram import Bot, Dispatcher, types
from supabase import create_client

import os

BOT_TOKEN = os.getenv("8942059082:AAEkeNHQTd7-vWDDoarhqq-mVn-3bZBIX84")
SUPABASE_URL = os.getenv("https://enhsspycsdbytmjbjoov.supabase.co")
SUPABASE_KEY = os.getenv("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImVuaHNzcHljc2RieXRtamJqb292Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODAzODMwOTMsImV4cCI6MjA5NTk1OTA5M30.2eZctXsKBOSl3xKiKVrQM-g9G8GLFnd5lZq_Q25FRVc")


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = text.replace("ё", "е")
    text = text.replace(",", ".")
    text = re.sub(r"\s+", " ", text)
    return text


def parse_food_message(text: str):
    """
    Понимает:
    рис 200
    рис 200г
    рис 200 г
    рис 200 грамм
    куриная грудка вареная 150
    chicken fried 180g
    """
    text = normalize_text(text)

    # Убираем слова грамм/г/гр/g, но оставляем число
    text = re.sub(r"(\d+(?:\.\d+)?)\s*(грамм|грамма|граммов|гр|г|grams|gram|g)\b", r"\1", text)

    match = re.search(r"(\d+(?:\.\d+)?)\s*$", text)
    if not match:
        return None, None

    grams = float(match.group(1))
    food_name = text[:match.start()].strip()

    if not food_name:
        return None, None

    return food_name, grams


def find_food(food_name: str):
    food_name = normalize_text(food_name)

    # 1) точное совпадение
    exact = supabase.table("food_db").select("*").eq("name", food_name).execute()
    if exact.data:
        return exact.data[0], []

    # 2) мягкий поиск по части названия
    search = supabase.table("food_db").select("*").ilike("name", f"%{food_name}%").limit(8).execute()
    if search.data:
        if len(search.data) == 1:
            return search.data[0], []
        return None, search.data

    # 3) если написали "курица", покажем все виды курицы
    words = food_name.split()
    if words:
        search = supabase.table("food_db").select("*").ilike("name", f"%{words[0]}%").limit(8).execute()
        if search.data:
            return None, search.data

    return None, []


def calc_nutrition(food, grams):
    k = grams / 100
    return {
        "calories": food["kcal_per_100g"] * k,
        "protein": food.get("protein_per_100g", 0) * k,
        "fat": food.get("fat_per_100g", 0) * k,
        "carbs": food.get("carbs_per_100g", 0) * k,
    }


def get_today_logs(telegram_id):
    result = (
        supabase.table("food_logs")
        .select("*")
        .eq("telegram_id", telegram_id)
        .eq("date", str(date.today()))
        .order("created_at", desc=False)
        .execute()
    )
    return result.data or []


def get_today_totals(telegram_id):
    logs = get_today_logs(telegram_id)
    return {
        "calories": sum(x.get("calories", 0) for x in logs),
        "protein": sum(x.get("protein", 0) for x in logs),
        "fat": sum(x.get("fat", 0) for x in logs),
        "carbs": sum(x.get("carbs", 0) for x in logs),
        "logs": logs,
    }


def format_food_suggestions(items, grams=None):
    text = "Я нашёл несколько вариантов. Напиши точнее:\n\n"
    for item in items:
        if grams:
            text += f"• {item['name']} {grams:g}\n"
        else:
            text += f"• {item['name']}\n"
    return text


@dp.message()
async def handle_message(message: types.Message):
    if not message.text:
        await message.answer("Пока я понимаю только текст. Фото еды добавим следующим этапом 📸")
        return

    text = normalize_text(message.text)

    if text == "/start":
        await message.answer(
            "Привет 👋\n\n"
            "Я считаю калории и КБЖУ.\n\n"
            "Пиши так:\n"
            "• рис вареный 200\n"
            "• курица жареная 150г\n"
            "• chicken fried 180g\n\n"
            "Команды:\n"
            "/day — итог за сегодня\n"
            "/foods — список продуктов\n"
            "/history — что ты ел сегодня"
        )
        return

    if text == "/foods":
        result = supabase.table("food_db").select("name").order("name").limit(120).execute()
        foods = [x["name"] for x in result.data]

        chunks = []
        current = "🍽 Продукты в базе:\n\n"
        for food in foods:
            line = f"• {food}\n"
            if len(current) + len(line) > 3500:
                chunks.append(current)
                current = ""
            current += line
        chunks.append(current)

        for chunk in chunks:
            await message.answer(chunk)
        return

    if text == "/day":
        totals = get_today_totals(message.from_user.id)
        await message.answer(
            "📊 Сегодня:\n\n"
            f"🔥 Калории: {totals['calories']:.1f} ккал\n"
            f"🥩 Белки: {totals['protein']:.1f} г\n"
            f"🥑 Жиры: {totals['fat']:.1f} г\n"
            f"🍚 Углеводы: {totals['carbs']:.1f} г"
        )
        return

    if text == "/history":
        logs = get_today_logs(message.from_user.id)
        if not logs:
            await message.answer("Сегодня пока ничего не записано.")
            return

        answer = "📋 Сегодня ты ел:\n\n"
        for item in logs:
            answer += (
                f"• {item['food']} {item['grams']:g} г — "
                f"{item['calories']:.1f} ккал\n"
            )
        await message.answer(answer)
        return

    food_name, grams = parse_food_message(text)

    if food_name is None or grams is None:
        await message.answer(
            "Пиши так:\n"
            "рис вареный 200\n"
            "курица жареная 150г\n"
            "chicken baked 180g"
        )
        return

    food, suggestions = find_food(food_name)

    if not food:
        if suggestions:
            await message.answer(format_food_suggestions(suggestions, grams))
        else:
            await message.answer(
                "Не нашёл такой продукт 😕\n\n"
                "Попробуй точнее, например:\n"
                "курица вареная 150\n"
                "курица жареная 150\n"
                "рис вареный 200"
            )
        return

    nutrition = calc_nutrition(food, grams)

    supabase.table("food_logs").insert({
        "telegram_id": message.from_user.id,
        "food": food["name"],
        "grams": grams,
        "calories": nutrition["calories"],
        "protein": nutrition["protein"],
        "fat": nutrition["fat"],
        "carbs": nutrition["carbs"],
        "date": str(date.today())
    }).execute()

    totals = get_today_totals(message.from_user.id)

    await message.answer(
        f"✅ Добавлено: {food['name']} {grams:g} г\n\n"
        f"🔥 {nutrition['calories']:.1f} ккал\n"
        f"🥩 Б: {nutrition['protein']:.1f} г\n"
        f"🥑 Ж: {nutrition['fat']:.1f} г\n"
        f"🍚 У: {nutrition['carbs']:.1f} г\n\n"
        f"📊 За сегодня: {totals['calories']:.1f} ккал"
    )


async def main():
    print("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
