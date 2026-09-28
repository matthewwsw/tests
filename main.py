import os
import sqlite3
from typing import Optional
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

Token = os.environ["BOT_TOKEN"]

# --- Админка ---
# Свой числовой Telegram ID узнать можно у бота @userinfobot — пришли ему /start
ADMIN_ID = int(os.environ["ADMIN_ID"])

# --- Platega ---
PLATEGA_MERCHANT_ID = os.environ["PLATEGA_MERCHANT_ID"]
PLATEGA_SECRET = os.environ["PLATEGA_SECRET"]
PLATEGA_BASE_URL = "https://app.platega.io"

PLANS = {
    "plan_1m": {"amount": 80, "label": "1 месяц"},
    "plan_3m": {"amount": 220, "label": "3 месяца"},
}

CHECK_INTERVAL = 10
MAX_CHECKS = 90  # 90 * 10 сек = 15 минут

# --- База данных ---
DB_PATH = "/data/bot_data.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS balances (user_id INTEGER PRIMARY KEY, total_paid REAL NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_seen TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    conn.close()


def register_user(user_id: int, username: Optional[str]):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO users (user_id, username) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username",
        (user_id, username),
    )
    conn.commit()
    conn.close()


def get_all_user_ids() -> list[int]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_stats() -> dict:
    conn = sqlite3.connect(DB_PATH)
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_revenue = conn.execute("SELECT COALESCE(SUM(total_paid), 0) FROM balances").fetchone()[0]
    paying_users = conn.execute("SELECT COUNT(*) FROM balances WHERE total_paid > 0").fetchone()[0]
    conn.close()
    return {"total_users": total_users, "total_revenue": total_revenue, "paying_users": paying_users}


def add_to_balance(user_id: int, amount: float):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO balances (user_id, total_paid) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET total_paid = total_paid + excluded.total_paid",
        (user_id, amount),
    )
    conn.commit()
    conn.close()


def get_balance(user_id: int) -> float:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT total_paid FROM balances WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row[0] if row else 0.0


# ---------- Тексты и клавиатуры меню ----------

def main_menu():
    text = (
        "🌲 *ELKA VPN*\n\n"
        "🌎 От 1 Сервера\n"
        "🛡️ Без логов подключений\n"
        "🔒 Надёжное подключение\n"
        "🚀 Высокая скорость соединения"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Купить подписку", callback_data="buy_subscription")],
        [InlineKeyboardButton("📊 Моя подписка", callback_data="my_subscription")],
        [
            InlineKeyboardButton("💰 Баланс", callback_data="balance"),
            InlineKeyboardButton("🎁 Промокод", callback_data="promo"),
        ],
        [InlineKeyboardButton("👥 Пригласить друзей", callback_data="invite")],
        [InlineKeyboardButton("🆘 Поддержка", url="https://t.me/blelbu")],
        [InlineKeyboardButton("📄 Документы", callback_data="docs")],
    ])
    return text, keyboard


def plans_menu():
    text = "Выберите срок подписки:"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("1 месяц — 80 руб", callback_data="plan_1m")],
        [InlineKeyboardButton("3 месяца — 220 руб", callback_data="plan_3m")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
    ])
    return text, keyboard


def back_only_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")]])


def admin_menu():
    text = "🛠 *Админ-панель*"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("🔍 Найти пользователя", callback_data="admin_find")],
        [InlineKeyboardButton("📢 Рассылка всем", callback_data="admin_broadcast")],
    ])
    return text, keyboard


def admin_back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")]])


# ---------- Platega ----------

async def create_payment(amount: float, description: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PLATEGA_BASE_URL}/transaction/process",
            headers={
                "X-MerchantId": PLATEGA_MERCHANT_ID,
                "X-Secret": PLATEGA_SECRET,
                "Content-Type": "application/json",
            },
            json={
                "paymentMethod": 2,  # СБП — сверь код метода в доках/у менеджера Platega
                "paymentDetails": {"amount": amount, "currency": "RUB"},
                "description": description,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()


async def check_payment_status(transaction_id: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{PLATEGA_BASE_URL}/transaction/{transaction_id}",
            headers={
                "X-MerchantId": PLATEGA_MERCHANT_ID,
                "X-Secret": PLATEGA_SECRET,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data.get("status", "")).upper()


async def check_payment_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data
    data["attempts"] = data.get("attempts", 0) + 1

    try:
        status = await check_payment_status(data["transaction_id"])
    except Exception:
        status = ""

    print(f"[Platega] transaction={data['transaction_id']} status={status}")

    if status in ("CONFIRMED", "PAID", "SUCCESS", "SUCCEEDED"):
        add_to_balance(data["user_id"], data["amount"])
        try:
            await context.bot.edit_message_text(
                chat_id=data["chat_id"],
                message_id=data["message_id"],
                text=f"✅ Оплата получена! Подписка «{data['plan_label']}» активирована.",
                reply_markup=back_only_keyboard(),
            )
        except Exception:
            await context.bot.send_message(
                chat_id=data["chat_id"],
                text=f"✅ Оплата получена! Подписка «{data['plan_label']}» активирована.",
            )
        job.schedule_removal()
    elif status in ("EXPIRED", "FAILED", "CANCELLED", "CANCELED", "DECLINED"):
        try:
            await context.bot.edit_message_text(
                chat_id=data["chat_id"],
                message_id=data["message_id"],
                text="❌ Платёж не прошёл или истёк. Попробуй оформить оплату заново через меню.",
                reply_markup=back_only_keyboard(),
            )
        except Exception:
            pass
        job.schedule_removal()
    elif data["attempts"] >= MAX_CHECKS:
        try:
            await context.bot.edit_message_text(
                chat_id=data["chat_id"],
                message_id=data["message_id"],
                text="⌛ Время ожидания оплаты истекло. Если уже оплатил(а) — напиши в поддержку.",
                reply_markup=back_only_keyboard(),
            )
        except Exception:
            pass
        job.schedule_removal()


# ---------- Хендлеры ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user.id, update.effective_user.username)
    text, keyboard = main_menu()
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return  # обычные пользователи не получают вообще никакого ответа на /admin
    text, keyboard = admin_menu()
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def admin_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия внутри админ-панели. Возвращает True, если кнопка обработана здесь."""
    query = update.callback_query

    if query.from_user.id != ADMIN_ID:
        return False

    if query.data == "admin_back":
        text, keyboard = admin_menu()
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return True

    if query.data == "admin_stats":
        stats = get_stats()
        await query.edit_message_text(
            "📊 *Статистика*\n\n"
            f"👥 Всего пользователей: {stats['total_users']}\n"
            f"💳 Оплативших хотя бы раз: {stats['paying_users']}\n"
            f"💰 Общая выручка: {stats['total_revenue']:.0f} руб.",
            reply_markup=admin_back_keyboard(),
            parse_mode="Markdown",
        )
        return True

    if query.data == "admin_find":
        context.user_data["admin_awaiting"] = "find"
        await query.edit_message_text(
            "Пришли числовой Telegram ID пользователя, чтобы посмотреть его баланс.",
            reply_markup=admin_back_keyboard(),
        )
        return True

    if query.data == "admin_broadcast":
        context.user_data["admin_awaiting"] = "broadcast"
        await query.edit_message_text(
            "Пришли текст сообщения — он будет отправлен всем пользователям бота.",
            reply_markup=admin_back_keyboard(),
        )
        return True

    return False


async def admin_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит обычные текстовые сообщения от админа, когда он в режиме поиска/рассылки."""
    if update.effective_user.id != ADMIN_ID:
        return

    awaiting = context.user_data.get("admin_awaiting")
    if not awaiting:
        return  # админ просто пишет боту не в контексте админки — игнорируем

    context.user_data["admin_awaiting"] = None

    if awaiting == "find":
        try:
            target_id = int(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("Это не похоже на числовой ID. Попробуй ещё раз через /admin.")
            return
        total = get_balance(target_id)
        await update.message.reply_text(f"Пользователь {target_id}: всего оплачено {total:.0f} руб.")

    elif awaiting == "broadcast":
        text = update.message.text
        user_ids = get_all_user_ids()
        sent, failed = 0, 0
        for uid in user_ids:
            try:
                await context.bot.send_message(chat_id=uid, text=text)
                sent += 1
            except Exception:
                failed += 1
        await update.message.reply_text(f"Рассылка завершена. Доставлено: {sent}, не удалось: {failed}.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("admin_"):
        handled = await admin_button_handler(update, context)
        if handled:
            return

    if query.data == "buy_subscription":
        text, keyboard = plans_menu()
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")

    elif query.data in PLANS:
        plan = PLANS[query.data]

        try:
            payment = await create_payment(
                amount=plan["amount"],
                description=f"Подписка ELKA VPN — {plan['label']}",
            )
        except Exception:
            await query.edit_message_text(
                "Не получилось создать платёж. Попробуй ещё раз чуть позже.",
                reply_markup=back_only_keyboard(),
            )
            return

        pay_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Оплатить", url=payment["redirect"])],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
        ])
        await query.edit_message_text(
            f"Тариф: {plan['label']} — {plan['amount']} руб.\n"
            f"Нажми кнопку ниже, чтобы перейти к оплате. После оплаты бот сам подтвердит платёж.",
            reply_markup=pay_keyboard,
        )

        context.job_queue.run_repeating(
            check_payment_job,
            interval=CHECK_INTERVAL,
            first=CHECK_INTERVAL,
            data={
                "transaction_id": payment["transactionId"],
                "chat_id": update.effective_chat.id,
                "message_id": query.message.message_id,
                "user_id": query.from_user.id,
                "amount": plan["amount"],
                "plan_label": plan["label"],
                "attempts": 0,
            },
            name=f"check_{payment['transactionId']}",
        )

    elif query.data == "back_to_menu":
        text, keyboard = main_menu()
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")

    elif query.data == "my_subscription":
        await query.edit_message_text("Скоро.", reply_markup=back_only_keyboard())

    elif query.data == "balance":
        total = get_balance(query.from_user.id)
        await query.edit_message_text(
            f"💰 Всего оплачено: {total:.0f} руб.",
            reply_markup=back_only_keyboard(),
        )

    elif query.data == "promo":
        await query.edit_message_text("Введите промокод:", reply_markup=back_only_keyboard())

    elif query.data == "invite":
        await query.edit_message_text("Скоро", reply_markup=back_only_keyboard())

    elif query.data == "docs":
        await query.edit_message_text(
            "Пользовательское Соглашение:\n"
            "https://telegra.ph/Polzovatelskoe-soglashenie-09-21-65\n\n"
            "Политика Конфиденциальности:\n"
            "https://telegra.ph/Politika-konfidencialnosti-09-21-83",
            reply_markup=back_only_keyboard(),
        )


init_db()

app = ApplicationBuilder().token(Token).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("admin", admin_command))
app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_handler))

app.run_polling()
