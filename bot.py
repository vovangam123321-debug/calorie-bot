import asyncio
import os
import re
import threading
from datetime import date

from aiogram import Bot, Dispatcher, types
from flask import Flask
from supabase import create_client


BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is not set")
if not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_KEY is not set")


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# Мини-веб-сервер нужен Render, чтобы Web Service видел открытый порт.
app = Flask(__name__)

@app.route("/")
def home():
    return "NutriFlow bot is running"


def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


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
        "calories": (food.get("kcal_per_100g") or 0) * k,
        "protein": (food.get("protein_per_100g") or 0) * k,
        "fat": (food.get("fat_per_100g") or 0) * k,
        "carbs": (food.get("carbs_per_100g") or 0) * k,
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
        "calories": sum(x.get("calories", 0) or 0 for x in logs),
        "protein": sum(x.get("protein", 0) or 0 for x in logs),
        "fat": sum(x.get("fat", 0) or 0 for x in logs),
        "carbs": sum(x.get("carbs", 0) or 0 for x in logs),
        "logs": logs,
    }


def get_or_create_user(message):
    telegram_id = message.from_user.id
    name = message.from_user.full_name

    result = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    if result.data:
        return result.data[0]

    new_user = {
        "telegram_id": telegram_id,
        "name": name,
        "daily_goal": 2000,
        "protein_goal": 0,
        "fat_goal": 0,
        "carbs_goal": 0,
        "weight": 0,
        "target_weight": 0,
        "waiting_for": None,
    }

    created = supabase.table("users").insert(new_user).execute()
    return created.data[0]


def update_user(telegram_id, data):
    supabase.table("users").update(data).eq("telegram_id", telegram_id).execute()


def format_food_suggestions(items, grams=None):
    text = "Я нашёл несколько вариантов. Напиши точнее:\n\n"
    for item in items:
        if grams:
            text += f"• {item['name']} {grams:g}\n"
        else:
            text += f"• {item['name']}\n"
    return text


def fmt_goal(current, goal, unit):
    if goal and goal > 0:
        left = goal - current
        sign = "осталось" if left >= 0 else "перебор"
        return f"{current:.1f} / {goal:g} {unit} ({sign}: {abs(left):.1f})"
    return f"{current:.1f} {unit}"


@dp.message()
async def handle_message(message: types.Message):
    print("MESSAGE:", message.text, flush=True)

    if not message.text:
        await message.answer("Пока я понимаю только текст. Фото еды добавим следующим этапом 📸")
        return

    raw_text = message.text.strip()
    text = normalize_text(raw_text)
    user = get_or_create_user(message)

    if text == "/cancel":
        update_user(message.from_user.id, {"waiting_for": None})
        await message.answer("Ок, отменил действие.")
        return

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
            "/history — что ты ел сегодня\n"
            "/delete — удалить последнюю запись\n"
            "/weight — изменить вес\n"
            "/target_weight — цель по весу\n"
            "/goal — цель по калориям и КБЖУ\n"
            "/profile — твои настройки\n"
            "/cancel — отменить ввод"
        )
        return

    if text == "/weight":
        update_user(message.from_user.id, {"waiting_for": "weight"})
        await message.answer("⚖️ Напиши свой вес в кг. Например: 82")
        return

    if text == "/target_weight":
        update_user(message.from_user.id, {"waiting_for": "target_weight"})
        await message.answer("🎯 Напиши желаемый вес в кг. Например: 75")
        return

    if text == "/goal":
        update_user(message.from_user.id, {"waiting_for": "kbju_goal"})
        await message.answer(
            "🎯 Напиши цель на день в формате:\n\n"
            "калории белки жиры углеводы\n\n"
            "Например:\n"
            "2400 160 80 260"
        )
        return

    if text == "/profile":
        user = get_or_create_user(message)

        await message.answer(
            "👤 Профиль:\n\n"
            f"⚖️ Вес: {user.get('weight') or 0:g} кг\n"
            f"🎯 Цель по весу: {user.get('target_weight') or 0:g} кг\n\n"
            f"🔥 Калории: {user.get('daily_goal') or 2000:g} ккал\n"
            f"🥩 Белки: {user.get('protein_goal') or 0:g} г\n"
            f"🥑 Жиры: {user.get('fat_goal') or 0:g} г\n"
            f"🍚 Углеводы: {user.get('carbs_goal') or 0:g} г"
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
        user = get_or_create_user(message)

        daily_goal = user.get("daily_goal") or 2000
        protein_goal = user.get("protein_goal") or 0
        fat_goal = user.get("fat_goal") or 0
        carbs_goal = user.get("carbs_goal") or 0

        await message.answer(
            "📊 Сегодня:\n\n"
            f"🔥 Калории: {fmt_goal(totals['calories'], daily_goal, 'ккал')}\n"
            f"🥩 Белки: {fmt_goal(totals['protein'], protein_goal, 'г')}\n"
            f"🥑 Жиры: {fmt_goal(totals['fat'], fat_goal, 'г')}\n"
            f"🍚 Углеводы: {fmt_goal(totals['carbs'], carbs_goal, 'г')}"
        )
        return

    if text == "/delete":
        logs = get_today_logs(message.from_user.id)

        if not logs:
            await message.answer("Сегодня нечего удалять.")
            return

        last = logs[-1]
        supabase.table("food_logs").delete().eq("id", last["id"]).execute()

        await message.answer(
            f"🗑 Удалил последнюю запись:\n"
            f"{last['food']} {last['grams']:g} г — {last['calories']:.1f} ккал"
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

    # Диалоговые ответы после команд /weight, /target_weight, /goal
    waiting_for = user.get("waiting_for")

    if waiting_for == "weight":
        try:
            weight = float(text.replace(",", "."))
        except ValueError:
            await message.answer("Напиши вес числом. Например: 82\n\n/cancel — отменить")
            return

        update_user(message.from_user.id, {"weight": weight, "waiting_for": None})
        protein_goal = weight * 1.6

        await message.answer(
            f"⚖️ Вес сохранён: {weight:g} кг\n"
            f"🥩 Ориентир по белку: ~{protein_goal:.0f} г/день"
        )
        return

    if waiting_for == "target_weight":
        try:
            target_weight = float(text.replace(",", "."))
        except ValueError:
            await message.answer("Напиши цель по весу числом. Например: 75\n\n/cancel — отменить")
            return

        update_user(message.from_user.id, {"target_weight": target_weight, "waiting_for": None})

        weight = user.get("weight") or 0
        if weight:
            diff = target_weight - weight
            if diff < 0:
                msg = f"🎯 Цель по весу сохранена: {target_weight:g} кг\n📉 Нужно снизить: {abs(diff):.1f} кг"
            elif diff > 0:
                msg = f"🎯 Цель по весу сохранена: {target_weight:g} кг\n📈 Нужно набрать: {diff:.1f} кг"
            else:
                msg = f"🎯 Цель по весу сохранена: {target_weight:g} кг\n✅ Ты уже на цели"
        else:
            msg = f"🎯 Цель по весу сохранена: {target_weight:g} кг"

        await message.answer(msg)
        return

    if waiting_for == "kbju_goal":
        parts = text.split()

        if len(parts) != 4:
            await message.answer(
                "Нужно 4 числа:\n"
                "калории белки жиры углеводы\n\n"
                "Например:\n"
                "2400 160 80 260\n\n"
                "/cancel — отменить"
            )
            return

        try:
            calories, protein, fat, carbs = [float(x.replace(",", ".")) for x in parts]
        except ValueError:
            await message.answer("Все значения должны быть числами. Например: 2400 160 80 260")
            return

        update_user(message.from_user.id, {
            "daily_goal": calories,
            "protein_goal": protein,
            "fat_goal": fat,
            "carbs_goal": carbs,
            "waiting_for": None,
        })

        await message.answer(
            "🎯 Цели сохранены:\n\n"
            f"🔥 Калории: {calories:g} ккал\n"
            f"🥩 Белки: {protein:g} г\n"
            f"🥑 Жиры: {fat:g} г\n"
            f"🍚 Углеводы: {carbs:g} г"
        )
        return

    food_name, grams = parse_food_message(text)

    if food_name is None or grams is None:
        await message.answer(
            "Пиши так:\n"
            "рис вареный 200\n"
            "курица жареная 150г\n"
            "chicken baked 180g\n\n"
            "Команды:\n"
            "/weight — изменить вес\n"
            "/goal — настроить цели"
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
        "date": str(date.today()),
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
    print("Bot started", flush=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    asyncio.run(main())
