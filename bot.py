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
    raise RuntimeError("BOT_TOKEN is missing. Add it in Render Environment Variables.")
if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is missing. Add it in Render Environment Variables.")
if not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_KEY is missing. Add it in Render Environment Variables.")


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is running"


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
    съел банан 200 грамм
    куриная грудка вареная 150
    chicken fried 180g
    """
    text = normalize_text(text)

    # Убираем лишние слова в начале
    text = re.sub(r"^(я\s+)?(съел|съела|поел|поела|ем|добавь|добавить|запиши|записать)\s+", "", text)

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


def get_food_exact(food_name: str):
    result = supabase.table("food_db").select("*").eq("name", food_name).execute()
    return result.data[0] if result.data else None


def find_food_by_alias(food_name: str):
    food_name = normalize_text(food_name)
    alias = supabase.table("food_aliases").select("*").eq("alias", food_name).execute()
    if not alias.data:
        return None
    target_name = alias.data[0]["target_name"]
    return get_food_exact(target_name)


def find_food(food_name: str):
    food_name = normalize_text(food_name)

    # 1) alias exact
    alias_food = find_food_by_alias(food_name)
    if alias_food:
        return alias_food, []

    # 2) exact food name
    exact = get_food_exact(food_name)
    if exact:
        return exact, []

    # 3) soft contains search
    search = supabase.table("food_db").select("*").ilike("name", f"%{food_name}%").limit(10).execute()
    if search.data:
        if len(search.data) == 1:
            return search.data[0], []
        return None, search.data

    # 4) word-based search: try each word
    words = [w for w in food_name.split() if len(w) > 2]
    suggestions = []
    seen = set()
    for word in words:
        # aliases by word
        alias_rows = supabase.table("food_aliases").select("*").ilike("alias", f"%{word}%").limit(5).execute()
        for row in alias_rows.data or []:
            target = row.get("target_name")
            if target and target not in seen:
                f = get_food_exact(target)
                if f:
                    suggestions.append(f)
                    seen.add(target)

        # foods by word
        food_rows = supabase.table("food_db").select("*").ilike("name", f"%{word}%").limit(5).execute()
        for f in food_rows.data or []:
            if f["name"] not in seen:
                suggestions.append(f)
                seen.add(f["name"])

        if len(suggestions) >= 10:
            break

    if suggestions:
        return None, suggestions[:10]

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
        "waiting_for": None
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


def fmt_goal(value):
    return f"{value:g}" if value else "не задано"


@dp.message()
async def handle_message(message: types.Message):
    print("MESSAGE:", message.text, flush=True)

    if not message.text:
        await message.answer("Пока я понимаю только текст. Фото еды добавим следующим этапом 📸")
        return

    text = normalize_text(message.text)
    user = get_or_create_user(message)
    waiting_for = user.get("waiting_for")

    # Waiting states
    if waiting_for == "weight":
        try:
            weight = float(text.replace(",", "."))
        except ValueError:
            await message.answer("Напиши вес числом. Например: 82")
            return

        update_user(message.from_user.id, {"weight": weight, "waiting_for": None})
        protein_goal = weight * 1.6

        await message.answer(
            f"⚖️ Вес сохранён: {weight:g} кг\n"
            f"🥩 Рекомендованная цель по белку: ~{protein_goal:.0f} г/день"
        )
        return

    if waiting_for == "target_weight":
        try:
            target_weight = float(text.replace(",", "."))
        except ValueError:
            await message.answer("Напиши цель по весу числом. Например: 75")
            return

        update_user(message.from_user.id, {"target_weight": target_weight, "waiting_for": None})
        await message.answer(f"🎯 Цель по весу сохранена: {target_weight:g} кг")
        return

    if waiting_for == "kbju_goal":
        parts = text.split()
        if len(parts) != 4:
            await message.answer(
                "Нужно 4 числа:\n"
                "калории белки жиры углеводы\n\n"
                "Например:\n"
                "2400 160 80 260"
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
            "waiting_for": None
        })

        await message.answer(
            "🎯 Цели сохранены:\n\n"
            f"🔥 Калории: {calories:g} ккал\n"
            f"🥩 Белки: {protein:g} г\n"
            f"🥑 Жиры: {fat:g} г\n"
            f"🍚 Углеводы: {carbs:g} г"
        )
        return

    # Commands
    if text == "/start":
        await message.answer(
            "Привет 👋\n\n"
            "Я считаю калории и КБЖУ.\n\n"
            "Пиши так:\n"
            "• рис вареный 200\n"
            "• курица жареная 150г\n"
            "• съел банан 200 грамм\n"
            "• chicken fried 180g\n\n"
            "Команды:\n"
            "/day — итог за сегодня\n"
            "/foods — список продуктов\n"
            "/history — что ты ел сегодня\n"
            "/delete — удалить последнюю запись\n"
            "/weight — изменить вес\n"
            "/target_weight — цель по весу\n"
            "/goal — цель по КБЖУ\n"
            "/profile — твой профиль"
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
            f"⚖️ Вес: {fmt_goal(user.get('weight') or 0)} кг\n"
            f"🎯 Цель по весу: {fmt_goal(user.get('target_weight') or 0)} кг\n\n"
            f"🔥 Калории: {fmt_goal(user.get('daily_goal') or 2000)} ккал\n"
            f"🥩 Белки: {fmt_goal(user.get('protein_goal') or 0)} г\n"
            f"🥑 Жиры: {fmt_goal(user.get('fat_goal') or 0)} г\n"
            f"🍚 Углеводы: {fmt_goal(user.get('carbs_goal') or 0)} г"
        )
        return

    if text == "/foods":
        result = supabase.table("food_db").select("name").order("name").limit(300).execute()
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

        left = daily_goal - totals["calories"]

        answer = (
            "📊 Сегодня:\n\n"
            f"🔥 Калории: {totals['calories']:.1f} / {daily_goal:g} ккал\n"
            f"📉 Осталось: {left:.1f} ккал\n"
        )

        if protein_goal:
            answer += f"🥩 Белки: {totals['protein']:.1f} / {protein_goal:g} г\n"
        else:
            answer += f"🥩 Белки: {totals['protein']:.1f} г\n"

        if fat_goal:
            answer += f"🥑 Жиры: {totals['fat']:.1f} / {fat_goal:g} г\n"
        else:
            answer += f"🥑 Жиры: {totals['fat']:.1f} г\n"

        if carbs_goal:
            answer += f"🍚 Углеводы: {totals['carbs']:.1f} / {carbs_goal:g} г"
        else:
            answer += f"🍚 Углеводы: {totals['carbs']:.1f} г"

        await message.answer(answer)
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
                f"{item['calories']:.1f} ккал "
                f"(Б {item.get('protein', 0):.1f} / Ж {item.get('fat', 0):.1f} / У {item.get('carbs', 0):.1f})\n"
            )
        await message.answer(answer)
        return

    # Food input
    food_name, grams = parse_food_message(text)

    if food_name is None or grams is None:
        await message.answer(
            "Пиши так:\n"
            "рис вареный 200\n"
            "курица жареная 150г\n"
            "съел банан 200 грамм\n"
            "чечевица 180\n"
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
                "рис вареный 200\n"
                "чечевица вареная 180"
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
    print("Bot started", flush=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    threading.Thread(target=run_web).start()
    asyncio.run(main())
