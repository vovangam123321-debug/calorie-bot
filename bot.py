import asyncio
import os
import re
import threading
from datetime import date

from aiogram import Bot, Dispatcher, F, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from flask import Flask
from supabase import create_client


BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing.")
if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is missing.")
if not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_KEY is missing.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = Flask(__name__)

@app.route("/")
def home():
    return "NutriFlow bot is running"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить еду"), KeyboardButton(text="📊 Сегодня")],
            [KeyboardButton(text="📋 История"), KeyboardButton(text="🗑 Удалить последнюю")],
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🎯 Цели")],
            [KeyboardButton(text="🍽 Продукты"), KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Напиши: рис вареный 200",
    )

def gender_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👨 Мужской", callback_data="gender:male"),
        InlineKeyboardButton(text="👩 Женский", callback_data="gender:female"),
    ]])

def goal_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Похудеть", callback_data="goal_type:lose")],
        [InlineKeyboardButton(text="⚖️ Поддерживать", callback_data="goal_type:maintain")],
        [InlineKeyboardButton(text="💪 Набрать", callback_data="goal_type:gain")],
    ])

def activity_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪑 Мало двигаюсь", callback_data="activity:1.2")],
        [InlineKeyboardButton(text="🚶 Средняя активность", callback_data="activity:1.375")],
        [InlineKeyboardButton(text="🏋️ Тренировки 3–5 раз/нед", callback_data="activity:1.55")],
        [InlineKeyboardButton(text="🔥 Очень активный", callback_data="activity:1.725")],
    ])

def settings_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚖️ Изменить вес", callback_data="setting:weight")],
        [InlineKeyboardButton(text="🎯 Цель по весу", callback_data="setting:target_weight")],
        [InlineKeyboardButton(text="🔥 Цель КБЖУ вручную", callback_data="setting:kbju")],
        [InlineKeyboardButton(text="🔁 Регистрация заново", callback_data="setting:onboarding")],
    ])


def normalize_text(text: str) -> str:
    text = text.lower().strip().replace("ё", "е").replace(",", ".")
    return re.sub(r"\s+", " ", text)

def parse_food_message(text: str):
    text = normalize_text(text)
    text = re.sub(r"^(я\s+)?(съел|съела|поел|поела|ем|добавь|добавить|запиши|записать)\s+", "", text)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*(грамм|грамма|граммов|гр|г|grams|gram|g)\b", r"\1", text)
    match = re.search(r"(\d+(?:\.\d+)?)\s*$", text)
    if not match:
        return None, None
    grams = float(match.group(1))
    food_name = text[:match.start()].strip()
    return (food_name, grams) if food_name else (None, None)

def get_food_exact(food_name):
    result = supabase.table("food_db").select("*").eq("name", food_name).execute()
    return result.data[0] if result.data else None

def find_food_by_alias(food_name):
    alias = supabase.table("food_aliases").select("*").eq("alias", normalize_text(food_name)).execute()
    if not alias.data:
        return None
    return get_food_exact(alias.data[0]["target_name"])

def find_food(food_name):
    food_name = normalize_text(food_name)
    alias_food = find_food_by_alias(food_name)
    if alias_food:
        return alias_food, []

    exact = get_food_exact(food_name)
    if exact:
        return exact, []

    search = supabase.table("food_db").select("*").ilike("name", f"%{food_name}%").limit(10).execute()
    if search.data:
        return (search.data[0], []) if len(search.data) == 1 else (None, search.data)

    suggestions, seen = [], set()
    for word in [w for w in food_name.split() if len(w) > 2]:
        rows = supabase.table("food_aliases").select("*").ilike("alias", f"%{word}%").limit(5).execute()
        for row in rows.data or []:
            target = row.get("target_name")
            if target and target not in seen:
                f = get_food_exact(target)
                if f:
                    suggestions.append(f); seen.add(target)
        rows = supabase.table("food_db").select("*").ilike("name", f"%{word}%").limit(5).execute()
        for f in rows.data or []:
            if f["name"] not in seen:
                suggestions.append(f); seen.add(f["name"])
        if len(suggestions) >= 10:
            break
    return (None, suggestions[:10]) if suggestions else (None, [])

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
        supabase.table("food_logs").select("*")
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

def get_or_create_user(obj):
    tg = obj.from_user if hasattr(obj, "from_user") else obj
    result = supabase.table("users").select("*").eq("telegram_id", tg.id).execute()
    if result.data:
        return result.data[0]
    new_user = {
        "telegram_id": tg.id, "name": tg.full_name, "daily_goal": 2000,
        "protein_goal": 0, "fat_goal": 0, "carbs_goal": 0,
        "weight": 0, "target_weight": 0, "gender": None,
        "age": 0, "height": 0, "activity_level": 1.2,
        "goal_type": None, "waiting_for": None, "onboarding_step": None,
    }
    created = supabase.table("users").insert(new_user).execute()
    return created.data[0]

def update_user(telegram_id, data):
    supabase.table("users").update(data).eq("telegram_id", telegram_id).execute()

def gender_name(v):
    return {"male": "мужской", "female": "женский"}.get(v, "не задано")

def goal_type_name(v):
    return {"lose": "похудеть", "maintain": "поддерживать", "gain": "набрать"}.get(v, "не задано")

def activity_name(v):
    try: v = float(v)
    except Exception: return "не задано"
    if v <= 1.2: return "мало двигаюсь"
    if v <= 1.375: return "средняя активность"
    if v <= 1.55: return "тренировки 3–5 раз/нед"
    return "очень активный"

def calculate_targets(user):
    weight = float(user.get("weight") or 0)
    height = float(user.get("height") or 0)
    age = float(user.get("age") or 0)
    gender = user.get("gender")
    activity = float(user.get("activity_level") or 1.2)
    goal_type = user.get("goal_type") or "maintain"

    if not weight or not height or not age or gender not in ("male", "female"):
        return None

    bmr = 10 * weight + 6.25 * height - 5 * age + (5 if gender == "male" else -161)
    calories = bmr * activity
    if goal_type == "lose":
        calories *= 0.82
    elif goal_type == "gain":
        calories *= 1.12

    protein = weight * (1.8 if goal_type == "lose" else 1.6)
    fat = max(weight * 0.8, 45)
    carbs = max((calories - protein * 4 - fat * 9) / 4, 50)

    return {
        "daily_goal": round(calories),
        "protein_goal": round(protein),
        "fat_goal": round(fat),
        "carbs_goal": round(carbs),
    }

def save_auto_targets(telegram_id, user):
    targets = calculate_targets(user)
    if targets:
        update_user(telegram_id, targets)
    return targets

def fmt(v):
    return f"{v:g}" if v else "не задано"

def profile_text(user):
    return (
        "👤 Профиль:\n\n"
        f"🚻 Пол: {gender_name(user.get('gender'))}\n"
        f"🎂 Возраст: {fmt(user.get('age') or 0)} лет\n"
        f"📏 Рост: {fmt(user.get('height') or 0)} см\n"
        f"⚖️ Вес: {fmt(user.get('weight') or 0)} кг\n"
        f"🎯 Цель по весу: {fmt(user.get('target_weight') or 0)} кг\n"
        f"🏃 Активность: {activity_name(user.get('activity_level'))}\n"
        f"🎯 Режим: {goal_type_name(user.get('goal_type'))}\n\n"
        f"🔥 Калории: {fmt(user.get('daily_goal') or 2000)} ккал\n"
        f"🥩 Белки: {fmt(user.get('protein_goal') or 0)} г\n"
        f"🥑 Жиры: {fmt(user.get('fat_goal') or 0)} г\n"
        f"🍚 Углеводы: {fmt(user.get('carbs_goal') or 0)} г"
    )

def format_food_suggestions(items, grams=None):
    text = "Я нашёл несколько вариантов. Напиши точнее:\n\n"
    for item in items:
        text += f"• {item['name']} {grams:g}\n" if grams else f"• {item['name']}\n"
    return text

def day_text(telegram_id):
    totals = get_today_totals(telegram_id)
    user_result = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    user = user_result.data[0] if user_result.data else {}
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
    answer += f"🥩 Белки: {totals['protein']:.1f}" + (f" / {protein_goal:g}" if protein_goal else "") + " г\n"
    answer += f"🥑 Жиры: {totals['fat']:.1f}" + (f" / {fat_goal:g}" if fat_goal else "") + " г\n"
    answer += f"🍚 Углеводы: {totals['carbs']:.1f}" + (f" / {carbs_goal:g}" if carbs_goal else "") + " г"
    return answer

async def start_onboarding(message):
    get_or_create_user(message)
    update_user(message.from_user.id, {"onboarding_step": "gender", "waiting_for": None})
    await message.answer(
        "Давай настроим профиль, чтобы я считал норму калорий и КБЖУ точнее.\n\nВыбери пол:",
        reply_markup=gender_keyboard(),
    )

@dp.callback_query(F.data.startswith("gender:"))
async def gender_callback(callback):
    gender = callback.data.split(":")[1]
    get_or_create_user(callback.from_user)
    update_user(callback.from_user.id, {"gender": gender, "onboarding_step": "age", "waiting_for": "age"})
    await callback.message.answer("🎂 Введи возраст полных лет. Например: 19")
    await callback.answer()

@dp.callback_query(F.data.startswith("goal_type:"))
async def goal_type_callback(callback):
    goal_type = callback.data.split(":")[1]
    update_user(callback.from_user.id, {"goal_type": goal_type, "onboarding_step": "activity", "waiting_for": None})
    await callback.message.answer("🏃 Выбери активность:", reply_markup=activity_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("activity:"))
async def activity_callback(callback):
    activity = float(callback.data.split(":")[1])
    update_user(callback.from_user.id, {"activity_level": activity, "onboarding_step": None, "waiting_for": None})
    user = supabase.table("users").select("*").eq("telegram_id", callback.from_user.id).execute().data[0]
    targets = save_auto_targets(callback.from_user.id, user)
    await callback.message.answer(
        "Готово 🎯\n\n"
        f"🔥 Калории: {targets['daily_goal']} ккал\n"
        f"🥩 Белки: {targets['protein_goal']} г\n"
        f"🥑 Жиры: {targets['fat_goal']} г\n"
        f"🍚 Углеводы: {targets['carbs_goal']} г\n\n"
        "Теперь можешь писать еду: рис вареный 200",
        reply_markup=main_menu(),
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("setting:"))
async def setting_callback(callback):
    setting = callback.data.split(":")[1]
    get_or_create_user(callback.from_user)
    if setting == "weight":
        update_user(callback.from_user.id, {"waiting_for": "weight"})
        await callback.message.answer("⚖️ Напиши новый вес в кг. Например: 82")
    elif setting == "target_weight":
        update_user(callback.from_user.id, {"waiting_for": "target_weight"})
        await callback.message.answer("🎯 Напиши желаемый вес в кг. Например: 75")
    elif setting == "kbju":
        update_user(callback.from_user.id, {"waiting_for": "kbju_goal"})
        await callback.message.answer("🎯 Напиши цель: калории белки жиры углеводы\n\nНапример:\n2400 160 80 260")
    elif setting == "onboarding":
        update_user(callback.from_user.id, {"onboarding_step": "gender", "waiting_for": None})
        await callback.message.answer("Выбери пол:", reply_markup=gender_keyboard())
    await callback.answer()

@dp.message()
async def handle_message(message: types.Message):
    print("MESSAGE:", message.text, flush=True)
    if not message.text:
        await message.answer("Пока я понимаю только текст. Фото еды добавим следующим этапом 📸")
        return

    text = normalize_text(message.text)
    user = get_or_create_user(message)
    waiting_for = user.get("waiting_for")

    if waiting_for == "age":
        try: age = int(float(text))
        except ValueError:
            await message.answer("Возраст нужен числом. Например: 19"); return
        update_user(message.from_user.id, {"age": age, "waiting_for": "height", "onboarding_step": "height"})
        await message.answer("📏 Введи рост в см. Например: 188"); return

    if waiting_for == "height":
        try: height = float(text)
        except ValueError:
            await message.answer("Рост нужен числом. Например: 188"); return
        update_user(message.from_user.id, {"height": height, "waiting_for": "weight", "onboarding_step": "weight"})
        await message.answer("⚖️ Введи текущий вес в кг. Например: 82"); return

    if waiting_for == "weight":
        try: weight = float(text)
        except ValueError:
            await message.answer("Вес нужен числом. Например: 82"); return
        update_user(message.from_user.id, {"weight": weight, "waiting_for": None})
        fresh = supabase.table("users").select("*").eq("telegram_id", message.from_user.id).execute().data[0]
        if fresh.get("onboarding_step") == "weight":
            update_user(message.from_user.id, {"onboarding_step": "target_weight", "waiting_for": "target_weight"})
            await message.answer("🎯 Введи желаемый вес в кг. Например: 75"); return
        targets = save_auto_targets(message.from_user.id, fresh)
        answer = f"⚖️ Вес сохранён: {weight:g} кг"
        if targets:
            answer += f"\n\n🎯 Цели пересчитаны:\n🔥 {targets['daily_goal']} ккал\n🥩 {targets['protein_goal']} г\n🥑 {targets['fat_goal']} г\n🍚 {targets['carbs_goal']} г"
        await message.answer(answer, reply_markup=main_menu()); return

    if waiting_for == "target_weight":
        try: target_weight = float(text)
        except ValueError:
            await message.answer("Цель по весу нужна числом. Например: 75"); return
        update_user(message.from_user.id, {"target_weight": target_weight, "waiting_for": None})
        fresh = supabase.table("users").select("*").eq("telegram_id", message.from_user.id).execute().data[0]
        if fresh.get("onboarding_step") == "target_weight":
            update_user(message.from_user.id, {"onboarding_step": "goal_type", "waiting_for": None})
            await message.answer("🎯 Какая основная цель?", reply_markup=goal_keyboard()); return
        await message.answer(f"🎯 Цель по весу сохранена: {target_weight:g} кг", reply_markup=main_menu()); return

    if waiting_for == "kbju_goal":
        parts = text.split()
        if len(parts) != 4:
            await message.answer("Нужно 4 числа: калории белки жиры углеводы\nНапример: 2400 160 80 260"); return
        try: calories, protein, fat, carbs = [float(x) for x in parts]
        except ValueError:
            await message.answer("Все значения должны быть числами."); return
        update_user(message.from_user.id, {"daily_goal": calories, "protein_goal": protein, "fat_goal": fat, "carbs_goal": carbs, "waiting_for": None})
        await message.answer(f"🎯 Цели сохранены:\n🔥 {calories:g} ккал\n🥩 {protein:g} г\n🥑 {fat:g} г\n🍚 {carbs:g} г", reply_markup=main_menu()); return

    if text in ("/start", "🔁 начать заново"):
        await start_onboarding(message); return
    if text in ("➕ добавить еду", "/add"):
        await message.answer("Напиши еду и граммы:\nрис вареный 200\nкурица жареная 150г\nчечевица 180"); return
    if text in ("📊 сегодня", "/day"):
        await message.answer(day_text(message.from_user.id), reply_markup=main_menu()); return
    if text in ("📋 история", "/history"):
        logs = get_today_logs(message.from_user.id)
        if not logs:
            await message.answer("Сегодня пока ничего не записано.", reply_markup=main_menu()); return
        answer = "📋 Сегодня ты ел:\n\n"
        for item in logs:
            answer += f"• {item['food']} {item['grams']:g} г — {item['calories']:.1f} ккал (Б {item.get('protein',0):.1f} / Ж {item.get('fat',0):.1f} / У {item.get('carbs',0):.1f})\n"
        await message.answer(answer, reply_markup=main_menu()); return
    if text in ("🗑 удалить последнюю", "/delete"):
        logs = get_today_logs(message.from_user.id)
        if not logs:
            await message.answer("Сегодня нечего удалять.", reply_markup=main_menu()); return
        last = logs[-1]
        supabase.table("food_logs").delete().eq("id", last["id"]).execute()
        await message.answer(f"🗑 Удалил последнюю запись:\n{last['food']} {last['grams']:g} г — {last['calories']:.1f} ккал", reply_markup=main_menu()); return
    if text in ("👤 профиль", "/profile"):
        user = supabase.table("users").select("*").eq("telegram_id", message.from_user.id).execute().data[0]
        await message.answer(profile_text(user), reply_markup=main_menu()); return
    if text in ("🎯 цели", "/goal", "⚙️ настройки", "/settings"):
        await message.answer("🎯 Что хочешь настроить?", reply_markup=settings_keyboard()); return
    if text in ("🍽 продукты", "/foods"):
        foods = supabase.table("food_db").select("name").order("name").limit(300).execute().data
        chunks, current = [], "🍽 Продукты в базе, первые 300:\n\n"
        for x in foods:
            line = f"• {x['name']}\n"
            if len(current) + len(line) > 3500:
                chunks.append(current); current = ""
            current += line
        chunks.append(current)
        for chunk in chunks:
            await message.answer(chunk, reply_markup=main_menu())
        return
    if text in ("/help", "помощь"):
        await message.answer("Примеры:\nрис вареный 200\nкурица жареная 150г\nсъел банан 200 грамм\nчечевица 180", reply_markup=main_menu()); return

    food_name, grams = parse_food_message(text)
    if food_name is None or grams is None:
        await message.answer("Я не понял 😕\nНапиши так: рис вареный 200", reply_markup=main_menu()); return

    food, suggestions = find_food(food_name)
    if not food:
        if suggestions:
            await message.answer(format_food_suggestions(suggestions, grams), reply_markup=main_menu())
        else:
            await message.answer("Не нашёл продукт 😕\nПопробуй: курица вареная 150\nрис вареный 200\nчечевица 180", reply_markup=main_menu())
        return

    nutrition = calc_nutrition(food, grams)
    supabase.table("food_logs").insert({
        "telegram_id": message.from_user.id, "food": food["name"], "grams": grams,
        "calories": nutrition["calories"], "protein": nutrition["protein"],
        "fat": nutrition["fat"], "carbs": nutrition["carbs"], "date": str(date.today())
    }).execute()
    totals = get_today_totals(message.from_user.id)

    await message.answer(
        f"✅ Добавлено: {food['name']} {grams:g} г\n\n"
        f"🔥 {nutrition['calories']:.1f} ккал\n"
        f"🥩 Б: {nutrition['protein']:.1f} г\n"
        f"🥑 Ж: {nutrition['fat']:.1f} г\n"
        f"🍚 У: {nutrition['carbs']:.1f} г\n\n"
        f"📊 За сегодня: {totals['calories']:.1f} ккал",
        reply_markup=main_menu(),
    )

async def main():
    print("Bot started", flush=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    threading.Thread(target=run_web).start()
    asyncio.run(main())
