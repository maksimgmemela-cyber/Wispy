import asyncio
import json
import os
import logging
from datetime import datetime, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.filters import Command, StateFilter
from aiogram.types import InlineKeyboardButton, Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# Включаем логирование, чтобы видеть ошибки в консоли
logging.basicConfig(level=logging.INFO)

# ==========================================
# ⚙️ НАСТРОЙКИ
# ==========================================
# Берем токен из переменных окружения хостинга
BOT_TOKEN = os.getenv("8952832886:AAGzCPvZV0rxxNMYoVdH83MphNi9h3FIG8Y") 
ADMIN_IDS = [6819742341, 5737924625]

if not BOT_TOKEN:
    logging.error("ТОКЕН НЕ НАЙДЕН! Добавь BOT_TOKEN в переменные окружения.")
    exit()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "store.db")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
temp_qty = {} 

# ==========================================
# 💾 ДВИЖОК БАЗЫ ДАННЫХ
# ==========================================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, balance INTEGER, purchases INTEGER,
            spent INTEGER, active_promo TEXT, banned BOOLEAN,
            ban_reason TEXT, ban_until TEXT, cart TEXT
        )""")
        await db.execute("CREATE TABLE IF NOT EXISTS categories (id TEXT PRIMARY KEY, name TEXT)")
        
        await db.execute("""CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY, name TEXT, price INTEGER, cat_id TEXT, desc TEXT, content TEXT
        )""")
        try: await db.execute("ALTER TABLE products ADD COLUMN content TEXT")
        except aiosqlite.OperationalError: pass
        
        await db.execute("CREATE TABLE IF NOT EXISTS promos (code TEXT PRIMARY KEY, discount REAL)")
        
        await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute("INSERT OR IGNORE INTO settings VALUES ('maintenance', '0')")
        
        async with db.execute("SELECT COUNT(*) FROM categories") as cursor:
            if (await cursor.fetchone())[0] == 0:
                await db.executemany("INSERT INTO categories VALUES (?, ?)", [
                    ("c_brawl", "📦 Brawl Stars"), ("c_ai", "🤖 ИИ Подписки")
                ])
                await db.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?)", [
                    ("p1", "Гемы 30 шт.", 150, "c_brawl", "Быстрая доставка.", "Твоя ссылка на гемы: https://..."),
                    ("p2", "ChatGPT Plus", 2200, "c_ai", "Аккаунт на 1 месяц.", "Логин: user@mail.com\nПароль: 123456")
                ])
                await db.execute("INSERT INTO promos VALUES (?, ?)", ("WISPY20", 0.20))
        await db.commit()

async def get_user(user_id: int) -> dict:
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                cart_json = json.dumps({})
                await db.execute("""INSERT INTO users 
                    (id, balance, purchases, spent, active_promo, banned, ban_reason, ban_until, cart) 
                    VALUES (?, 0, 0, 0, NULL, False, NULL, NULL, ?)""", (user_id, cart_json))
                await db.commit()
                return {"id": user_id, "balance": 0, "purchases": 0, "spent": 0, "active_promo": None, "banned": False, "ban_reason": None, "ban_until": None, "cart": {}}
            
            user = dict(row)
            user["cart"] = json.loads(user["cart"])
            user["banned"] = bool(user["banned"])
            if user["ban_until"]: user["ban_until"] = datetime.fromisoformat(user["ban_until"])
            return user

async def update_user(user_id: int, **kwargs):
    async with aiosqlite.connect(DB_NAME) as db:
        for key, value in kwargs.items():
            if key == "cart": value = json.dumps(value)
            elif isinstance(value, datetime): value = value.isoformat()
            await db.execute(f"UPDATE users SET {key} = ? WHERE id = ?", (value, user_id))
        await db.commit()

# ==========================================
# 🛡️ MIDDLEWARE: ТЕХ.РАБОТЫ И БАНЫ
# ==========================================
class MainMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user_id = event.from_user.id
        
        if user_id not in ADMIN_IDS:
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute("SELECT value FROM settings WHERE key = 'maintenance'") as cur:
                    m_mode = await cur.fetchone()
                    if m_mode and m_mode[0] == '1':
                        msg = "🛠 <b>Wispy Store на техническом обслуживании!</b>\n\nМы скоро вернемся, пожалуйста, подождите."
                        if isinstance(event, types.Message): await event.answer(msg, parse_mode="HTML")
                        elif isinstance(event, types.CallbackQuery): await event.answer("🛠 Идут тех. работы!", show_alert=True)
                        return
                        
        user_data = await get_user(user_id)
        if user_data["banned"]:
            ban_until = user_data["ban_until"]
            if ban_until and datetime.now() >= ban_until:
                await update_user(user_id, banned=False, ban_reason=None, ban_until=None)
            else:
                reason = user_data["ban_reason"] or "Не указана"
                until_str = ban_until.strftime('%d.%m.%Y %H:%M') if ban_until else "Навсегда"
                ban_text = f"⛔️ <b>Доступ ограничен!</b>\n\n📝 <b>Причина:</b> {reason}\n⏳ <b>Разблокировка:</b> {until_str}"
                
                if isinstance(event, types.Message): await event.answer(ban_text, parse_mode="HTML")
                elif isinstance(event, types.CallbackQuery): await event.answer(f"⛔️ Бан до: {until_str}", show_alert=True)
                return 
        return await handler(event, data)

dp.message.middleware(MainMiddleware())
dp.callback_query.middleware(MainMiddleware())

# ==========================================
# ⚙️ FSM И КОЛЛБЕКИ
# ==========================================
class MenuCb(CallbackData, prefix="menu"): action: str
class CatCb(CallbackData, prefix="cat"): id: str
class ProdCb(CallbackData, prefix="prod"): id: str; action: str
class CartCb(CallbackData, prefix="cart"): action: str

class UserStates(StatesGroup): waiting_for_promo = State()
class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    add_cat_name = State()
    add_name = State()
    add_price = State()
    add_cat = State()
    add_desc = State()
    add_content = State()
    ban_user_id = State()
    ban_reason = State()
    ban_duration = State()
    unban_user = State()
    add_promo_code = State()
    add_promo_discount = State()
    del_cat = State()
    del_prod = State()

# ==========================================
# 1. ГЛАВНОЕ МЕНЮ И ПРОФИЛЬ
# ==========================================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await get_user(message.from_user.id) 
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🛍 Каталог", callback_data=MenuCb(action="catalog").pack(), style="primary"), 
        InlineKeyboardButton(text="ℹ️ О боте", callback_data=MenuCb(action="about").pack(), style="primary")
    )
    builder.row(
        InlineKeyboardButton(text="🛒 Корзина", callback_data=MenuCb(action="cart").pack(), style="primary"), 
        InlineKeyboardButton(text="👤 Профиль", callback_data=MenuCb(action="profile").pack(), style="primary")
    )
    builder.row(InlineKeyboardButton(text="⚙️ Прочее", callback_data=MenuCb(action="misc").pack(), style="primary"))
    await message.answer("👋 <b>Добро пожаловать в Wispy Store!</b>\n\nВыбирай нужный раздел ниже 👇", reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(MenuCb.filter(F.action == "main"))
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await cmd_start(callback.message, state)
    await callback.message.delete()

@dp.callback_query(MenuCb.filter(F.action == "about"))
async def show_about(callback: CallbackQuery):
    text = "ℹ️ <b>О Wispy Store</b>\n\nАвтоматизированный магазин игровых ценностей. Работаем 24/7."
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📢 Наш Канал", url="https://t.me/WispyStore"))
    builder.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data=MenuCb(action="main").pack(), style="primary"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(MenuCb.filter(F.action == "misc"))
async def show_misc(callback: CallbackQuery):
    text = "⚙️ <b>Прочее</b>\n\nЗдесь вы можете найти юридическую информацию и связаться с нашей поддержкой."
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📄 Политика конфиденциальности", url="https://telegra.ph/Politika-konfidencialnosti-06-21-31"))
    builder.row(InlineKeyboardButton(text="📝 Пользовательское соглашение", url="https://telegra.ph/Polzovatelskoe-soglashenie-04-01-19"))
    builder.row(InlineKeyboardButton(text="👨‍💻 Контакт поддержки", url="https://t.me/Rubynchikk"))
    builder.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data=MenuCb(action="main").pack(), style="primary"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(MenuCb.filter(F.action == "profile"))
async def show_profile(callback: CallbackQuery):
    u = await get_user(callback.from_user.id)
    promo_text = f"Активирован ({u['active_promo']})" if u['active_promo'] else "Нет"
    text = (f"👤 <b>Ваш профиль</b>\n\n🔑 <b>ID:</b> <code>{callback.from_user.id}</code>\n"
            f"💰 <b>Баланс:</b> {u['balance']} ₽\n🛍 <b>Покупок:</b> {u['purchases']} шт. (на {u['spent']} ₽)\n"
            f"🎟 <b>Промокод:</b> {promo_text}")
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="stub", style="success"))
    if not u['active_promo']: 
        builder.row(InlineKeyboardButton(text="🎟 Ввести промокод", callback_data=MenuCb(action="promo").pack(), style="primary"))
    builder.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data=MenuCb(action="main").pack(), style="primary"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(MenuCb.filter(F.action == "promo"))
async def profile_promo_enter(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("✍️ <b>Напишите промокод</b> (или /cancel для отмены):", parse_mode="HTML")
    await state.set_state(UserStates.waiting_for_promo)

@dp.message(StateFilter(UserStates.waiting_for_promo))
async def process_promo(message: Message, state: FSMContext):
    code = message.text.upper()
    if code == "/CANCEL":
        await state.clear()
        return await message.answer("❌ Ввод отменен. Нажмите /start")
        
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM promos WHERE code = ?", (code,)) as cursor:
            if await cursor.fetchone():
                await update_user(message.from_user.id, active_promo=code)
                await message.answer(f"✅ Промокод <b>{code}</b> успешно применен!", parse_mode="HTML")
                await state.clear()
            else:
                await message.answer("❌ Неверный или несуществующий промокод. Попробуйте еще раз или напишите /cancel.")

# ==========================================
# 2. КАТАЛОГ И ПОКУПКА
# ==========================================
@dp.callback_query(MenuCb.filter(F.action == "catalog"))
async def show_catalog(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM categories") as cursor:
            async for row in cursor:
                builder.row(InlineKeyboardButton(text=row[1], callback_data=CatCb(id=row[0]).pack(), style="primary"))
    builder.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data=MenuCb(action="main").pack(), style="primary"))
    await callback.message.edit_text("<b>🛍 Каталог товаров</b>\nВыберите категорию:", reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(CatCb.filter())
async def show_cat(callback: CallbackQuery, callback_data: CatCb):
    c_id = callback_data.id
    builder = InlineKeyboardBuilder()
    has_prods = False
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM products WHERE cat_id = ?", (c_id,)) as cursor:
            async for p in cursor:
                has_prods = True
                builder.row(InlineKeyboardButton(text=f"▪️ {p[1]} — {p[2]} ₽", callback_data=ProdCb(id=p[0], action="view").pack(), style="primary"))
        async with db.execute("SELECT name FROM categories WHERE id = ?", (c_id,)) as cursor:
            cat_name = (await cursor.fetchone())[0]

    if not has_prods: return await callback.answer("Пусто!", show_alert=True)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=MenuCb(action="catalog").pack(), style="primary"))
    await callback.message.edit_text(f"📂 <b>{cat_name}</b>\nВыберите товар:", reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(ProdCb.filter())
async def handle_product(callback: CallbackQuery, callback_data: ProdCb):
    uid = callback.from_user.id
    p_id = callback_data.id
    act = callback_data.action
    
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE id = ?", (p_id,)) as cursor:
            prod = await cursor.fetchone()
            
    if not prod: return await callback.answer("Товар не найден!", show_alert=True)

    if act == "view": temp_qty[uid] = 1 
    elif act == "inc": temp_qty[uid] = temp_qty.get(uid, 1) + 1
    elif act == "dec" and temp_qty.get(uid, 1) > 1: temp_qty[uid] -= 1
    elif act == "add":
        u = await get_user(uid)
        cart = u["cart"]
        add_q = temp_qty.get(uid, 1)
        cart[p_id] = cart.get(p_id, 0) + add_q
        await update_user(uid, cart=cart)
        await callback.answer(f"✅ Добавлено: {add_q} шт.", show_alert=True)
        return await show_cat(callback, CatCb(id=prod["cat_id"]))
    elif act == "buy_now":
        u = await get_user(uid)
        qty = temp_qty.get(uid, 1)
        price = prod["price"] * qty
        
        if u["active_promo"]:
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute("SELECT discount FROM promos WHERE code = ?", (u["active_promo"],)) as cursor:
                    promo = await cursor.fetchone()
                    if promo: price = int(price * (1 - promo[0]))
                    
        await update_user(uid, purchases=u["purchases"] + 1, spent=u["spent"] + price)
        
        delivery_text = f"✅ <b>Успешная покупка!</b>\nВы купили {prod['name']} (x{qty}) за {price} ₽\n\n📦 <b>Ваш товар:</b>\n<code>{prod['content']}</code>"
        return await callback.message.edit_text(delivery_text, parse_mode="HTML")

    qty = temp_qty.get(uid, 1)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="➖", callback_data=ProdCb(id=p_id, action="dec").pack(), style="danger"), 
        InlineKeyboardButton(text=f"{qty} шт.", callback_data="ignore", style="primary"), 
        InlineKeyboardButton(text="➕", callback_data=ProdCb(id=p_id, action="inc").pack(), style="success")
    )
    builder.row(
        InlineKeyboardButton(text="🛒 В корзину", callback_data=ProdCb(id=p_id, action="add").pack(), style="primary"), 
        InlineKeyboardButton(text="💸 Оплатить сразу", callback_data=ProdCb(id=p_id, action="buy_now").pack(), style="success")
    )
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=CatCb(id=prod["cat_id"]).pack(), style="primary"))
    
    text = f"🏷 <b>{prod['name']}</b>\n📝 <i>{prod['desc']}</i>\n\n💵 Цена: {prod['price']} ₽ / шт.\n📦 Выбрано: <b>{qty} шт.</b> (<b>{qty * prod['price']} ₽</b>)"
    try: await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except: pass

# ==========================================
# 3. КОРЗИНА И ОПЛАТА
# ==========================================
@dp.callback_query(MenuCb.filter(F.action == "cart"))
async def show_cart(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await get_user(callback.from_user.id)
    cart = u["cart"]
    
    if not cart:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="⬅️ В меню", callback_data=MenuCb(action="main").pack(), style="primary"))
        return await callback.message.edit_text("🛒 Корзина пуста.", reply_markup=builder.as_markup())

    total = 0
    text = "🛒 <b>Корзина:</b>\n\n"
    
    async with aiosqlite.connect(DB_NAME) as db:
        for pid, q in cart.items():
            async with db.execute("SELECT name, price FROM products WHERE id = ?", (pid,)) as cursor:
                p_data = await cursor.fetchone()
                if p_data:
                    cost = p_data[1] * q
                    total += cost
                    text += f"▪️ {p_data[0]} x{q} — {cost} ₽\n"
                    
        discount = 0
        if u["active_promo"]:
            async with db.execute("SELECT discount FROM promos WHERE code = ?", (u["active_promo"],)) as cursor:
                promo_data = await cursor.fetchone()
                if promo_data: discount = promo_data[0]
        
    final_total = int(total * (1 - discount))
    text += f"\n💰 Сумма: {total} ₽"
    if discount: text += f"\n🎟 Промокод: -{int(discount*100)}%\n💸 <b>К оплате: {final_total} ₽</b>"
    else: text += f"\n💸 <b>К оплате: {final_total} ₽</b>"

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="💳 Оплатить", callback_data=CartCb(action="pay").pack(), style="success"), 
        InlineKeyboardButton(text="🗑 Очистить", callback_data=CartCb(action="clear").pack(), style="danger")
    )
    builder.row(InlineKeyboardButton(text="⬅️ В меню", callback_data=MenuCb(action="main").pack(), style="primary"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(CartCb.filter())
async def handle_cart(callback: CallbackQuery, callback_data: CartCb, state: FSMContext):
    uid = callback.from_user.id
    act = callback_data.action

    if act == "clear":
        await update_user(uid, cart={}, active_promo=None)
        await show_cart(callback, state)
    elif act == "pay":
        u = await get_user(uid)
        cart = u["cart"]
        
        delivery_text = "✅ <b>Корзина успешно оплачена!</b>\n\nВот ваши товары:\n"
        async with aiosqlite.connect(DB_NAME) as db:
            for pid, q in cart.items():
                async with db.execute("SELECT name, content FROM products WHERE id = ?", (pid,)) as cursor:
                    p = await cursor.fetchone()
                    if p: delivery_text += f"\n📦 <b>{p[0]}</b> (x{q}):\n<code>{p[1] or 'Нет данных'}</code>\n"

        await update_user(uid, cart={}, active_promo=None, purchases=u["purchases"] + sum(cart.values()))
        await callback.message.edit_text(delivery_text, parse_mode="HTML")

# ==========================================
# 4. АДМИН ПАНЕЛЬ
# ==========================================
@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id not in ADMIN_IDS: return await message.answer("⛔️ Отказано.")
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT value FROM settings WHERE key = 'maintenance'") as cur:
            m_mode = await cur.fetchone()
            is_maint = m_mode and m_mode[0] == '1'

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Стат-ка", callback_data="adm_stats", style="primary"),
        InlineKeyboardButton(text="📥 Выгрузить юзеров", callback_data="adm_export_users", style="primary")
    )
    builder.row(InlineKeyboardButton(text="✉️ Рассылка", callback_data="adm_broadcast", style="primary"))
    
    builder.row(
        InlineKeyboardButton(text="📂 Добавить Категорию", callback_data="adm_add_cat", style="primary"), 
        InlineKeyboardButton(text="🗑 Удалить Кат.", callback_data="adm_del_cat_list", style="danger")
    )
    builder.row(
        InlineKeyboardButton(text="➕ Добавить Товар", callback_data="adm_add", style="primary"), 
        InlineKeyboardButton(text="🗑 Удалить Товар", callback_data="adm_del_prod_list", style="danger")
    )
    
    builder.row(
        InlineKeyboardButton(text="🚫 Выдать Бан", callback_data="adm_ban", style="danger"), 
        InlineKeyboardButton(text="✅ Снять Бан", callback_data="adm_unban", style="success")
    )
    builder.row(InlineKeyboardButton(text="🎟 Создать промокод", callback_data="adm_add_promo", style="primary"))
    
    maint_text = "✅ Включить бота" if is_maint else "🛑 Выключить бота"
    builder.row(InlineKeyboardButton(text=maint_text, callback_data="adm_toggle_maint", style="danger" if not is_maint else "success"))
    
    status = "🔴 ВЫКЛЮЧЕН" if is_maint else "🟢 РАБОТАЕТ"
    await message.answer(f"🛠 <b>Админ-панель</b>\nСтатус бота: {status}", reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("adm_"))
async def handle_admin(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("⛔️ Отказано в доступе. Вы не администратор.", show_alert=True)

    act = callback.data
    if act == "adm_toggle_maint":
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT value FROM settings WHERE key = 'maintenance'") as cur:
                val = await cur.fetchone()
                new_val = '0' if val and val[0] == '1' else '1'
            await db.execute("UPDATE settings SET value = ? WHERE key = 'maintenance'", (new_val,))
            await db.commit()
        await callback.answer("Статус изменен!")
        await cmd_admin(callback.message, state)
        await callback.message.delete()
    elif act == "adm_stats":
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT COUNT(*), SUM(spent) FROM users") as cursor:
                row = await cursor.fetchone()
                count, total = row[0], row[1] or 0
        await callback.message.edit_text(f"📊 <b>Статистика</b>\nЮзеров: {count}\nОборот: {total} ₽", parse_mode="HTML")
    elif act == "adm_export_users":
        export_text = "ID Пользователя | Баланс | Покупок | Потрачено\n"
        export_text += "-" * 50 + "\n"
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT id, balance, purchases, spent FROM users") as cursor:
                async for row in cursor:
                    export_text += f"{row[0]} | {row[1]} ₽ | {row[2]} шт. | {row[3]} ₽\n"
        
        file = types.BufferedInputFile(export_text.encode('utf-8'), filename=f"users_{datetime.now().strftime('%Y%m%d')}.txt")
        await callback.message.answer_document(document=file, caption="📥 Выгрузка пользователей")
        await callback.answer()
    elif act == "adm_add_promo":
        await callback.message.edit_text("Введите название нового промокода (Например: SALE20):")
        await state.set_state(AdminStates.add_promo_code)
    elif act == "adm_broadcast":
        await callback.message.edit_text("Текст рассылки (или /cancel):")
        await state.set_state(AdminStates.waiting_for_broadcast)
    elif act == "adm_add_cat":
        await callback.message.edit_text("Название категории:")
        await state.set_state(AdminStates.add_cat_name)
    elif act == "adm_add":
        await callback.message.edit_text("Название товара:")
        await state.set_state(AdminStates.add_name)
    elif act == "adm_ban":
        await callback.message.edit_text("ID для блокировки:")
        await state.set_state(AdminStates.ban_user_id)
    elif act == "adm_unban":
        await callback.message.edit_text("ID для разбана:")
        await state.set_state(AdminStates.unban_user)
    elif act == "adm_del_cat_list":
        text = "📂 <b>Список категорий:</b>\n"
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT id, name FROM categories") as cursor:
                async for row in cursor:
                    text += f"ID: <code>{row[0]}</code> — {row[1]}\n"
        text += "\nОтправьте ID категории для удаления (или /cancel):"
        await callback.message.edit_text(text, parse_mode="HTML")
        await state.set_state(AdminStates.del_cat)
    elif act == "adm_del_prod_list":
        text = "📦 <b>Список товаров:</b>\n"
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT id, name FROM products") as cursor:
                async for row in cursor:
                    text += f"ID: <code>{row[0]}</code> — {row[1]}\n"
        text += "\nОтправьте ID товара для удаления (или /cancel):"
        await callback.message.edit_text(text, parse_mode="HTML")
        await state.set_state(AdminStates.del_prod)

# Удаление категорий и товаров
@dp.message(StateFilter(AdminStates.del_cat))
async def adm_del_cat_finish(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS: return
    if m.text == "/cancel": return await state.clear()
    cat_id = m.text.strip()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        await db.execute("DELETE FROM products WHERE cat_id = ?", (cat_id,))
        await db.commit()
    await m.answer(f"✅ Категория <code>{cat_id}</code> и все её товары удалены!", parse_mode="HTML")
    await state.clear()

@dp.message(StateFilter(AdminStates.del_prod))
async def adm_del_prod_finish(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS: return
    if m.text == "/cancel": return await state.clear()
    prod_id = m.text.strip()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM products WHERE id = ?", (prod_id,))
        await db.commit()
    await m.answer(f"✅ Товар <code>{prod_id}</code> удален!", parse_mode="HTML")
    await state.clear()

# Админка: Промокоды
@dp.message(StateFilter(AdminStates.add_promo_code))
async def adm_promo_1(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS: return
    await state.update_data(p_code=m.text.upper())
    await m.answer("Укажите скидку в процентах (например, 15 для 15%):")
    await state.set_state(AdminStates.add_promo_discount)

@dp.message(StateFilter(AdminStates.add_promo_discount))
async def adm_promo_2(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS: return
    if not m.text.isdigit(): return await m.answer("Укажите число!")
    disc = int(m.text) / 100
    data = await state.get_data()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO promos VALUES (?, ?)", (data['p_code'], disc))
        await db.commit()
    await m.answer(f"✅ Промокод <b>{data['p_code']}</b> на {m.text}% успешно создан!", parse_mode="HTML")
    await state.clear()

# Админка: Товары
@dp.message(StateFilter(AdminStates.add_name))
async def adm_add_1(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS: return
    await state.update_data(name=m.text)
    await m.answer("Цена (число):")
    await state.set_state(AdminStates.add_price)

@dp.message(StateFilter(AdminStates.add_price))
async def adm_add_2(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS: return
    if not m.text.isdigit(): return
    await state.update_data(price=int(m.text))
    builder = InlineKeyboardBuilder()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM categories") as cursor:
            async for c in cursor:
                builder.row(InlineKeyboardButton(text=c[1], callback_data=f"setcat_{c[0]}", style="primary"))
    await m.answer("Категория?", reply_markup=builder.as_markup())
    await state.set_state(AdminStates.add_cat)

@dp.callback_query(StateFilter(AdminStates.add_cat), F.data.startswith("setcat_"))
async def adm_add_3(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS: return
    await state.update_data(cat=c.data.replace("setcat_", ""))
    await c.message.edit_text("Описание для витрины:")
    await state.set_state(AdminStates.add_desc)

@dp.message(StateFilter(AdminStates.add_desc))
async def adm_add_4(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS: return
    await state.update_data(desc=m.text)
    await m.answer("📦 Текст/Товар, который выдастся юзеру ПОСЛЕ оплаты (ссылка, аккаунт, ключ):")
    await state.set_state(AdminStates.add_content)

@dp.message(StateFilter(AdminStates.add_content))
async def adm_add_5(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS: return
    d = await state.get_data()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM products") as cursor:
            new_id = f"p_{(await cursor.fetchone())[0] + 1}"
        await db.execute("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?)", (new_id, d["name"], d["price"], d["cat"], d["desc"], m.text))
        await db.commit()
    await m.answer("✅ Товар успешно добавлен и готов к выдаче!")
    await state.clear()

# Админка: Категории, Баны, Рассылка
@dp.message(StateFilter(AdminStates.add_cat_name))
async def adm_add_category(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS: return
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM categories") as cursor:
            new_id = f"c_{(await cursor.fetchone())[0] + 1}"
        await db.execute("INSERT INTO categories VALUES (?, ?)", (new_id, m.text))
        await db.commit()
    await m.answer(f"✅ Категория создана: {m.text}")
    await state.clear()

@dp.message(StateFilter(AdminStates.waiting_for_broadcast))
async def adm_broadcast(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS: return
    if m.text == "/cancel": return await state.clear()
    sent = 0
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id FROM users WHERE banned = 0") as cursor:
            async for row in cursor:
                try:
                    await bot.send_message(row[0], f"🔔 <b>Уведомление:</b>\n\n{m.text}", parse_mode="HTML")
                    sent += 1
                except: pass
    await m.answer(f"✅ Разослано: {sent} юзерам")
    await state.clear()

@dp.message(StateFilter(AdminStates.ban_user_id))
async def adm_ban_1(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS: return
    if not m.text.isdigit(): return
    uid = int(m.text)
    await get_user(uid) 
    await state.update_data(ban_id=uid)
    await m.answer("Причина блокировки:")
    await state.set_state(AdminStates.ban_reason)

@dp.message(StateFilter(AdminStates.ban_reason))
async def adm_ban_2(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS: return
    await state.update_data(ban_reason=m.text)
    await m.answer("На сколько часов? (0 - навсегда):")
    await state.set_state(AdminStates.ban_duration)

@dp.message(StateFilter(AdminStates.ban_duration))
async def adm_ban_3(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS: return
    if not m.text.isdigit(): return
    data = await state.get_data()
    hours = int(m.text)
    until_date = None
    time_text = "Навсегда"
    if hours > 0:
        until_date = datetime.now() + timedelta(hours=hours)
        time_text = f"до {until_date.strftime('%d.%m.%Y %H:%M')}"
        
    await update_user(data["ban_id"], banned=True, ban_reason=data["ban_reason"], ban_until=until_date)
    await m.answer(f"✅ Юзер забанен {time_text}.", parse_mode="HTML")
    await state.clear()

@dp.message(StateFilter(AdminStates.unban_user))
async def adm_unban_user(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS: return
    if not m.text.isdigit(): return
    await update_user(int(m.text), banned=False, ban_reason=None, ban_until=None)
    await m.answer("✅ Юзер разблокирован.")
    await state.clear()

@dp.callback_query(F.data.in_(["ignore", "stub"]))
async def ignore_stub(c: CallbackQuery): await c.answer("В разработке!", show_alert=True)

@dp.message(Command("about"))
async def stub_about(message: Message): pass

# ==========================================
# ЗАПУСК БОТА
# ==========================================
async def main():
    print(f"Инициализация базы данных... Файл: {DB_NAME}")
    await init_db()
    print("Wispy Store запущен с логированием!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())