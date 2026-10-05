import os
import sqlite3
import json
import uuid
import time
from datetime import datetime, timedelta
from typing import Optional
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageOriginUser
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
    "plan_1m": {"amount": 80, "label": "1 месяц", "days": 30},
    "plan_3m": {"amount": 220, "label": "3 месяца", "days": 90},
}

# --- H1Cloud VPN API (выдача VPN-доступа) ---
H1CLOUD_API_URL = os.environ["H1CLOUD_API_URL"].rstrip("/")  # например http://de13.h1cloud.net:25053
H1CLOUD_API_KEY = os.environ["H1CLOUD_API_KEY"]


async def h1cloud_create_client(telegram_user_id: int, days: int) -> str:
    """Создаёт нового клиента в H1Cloud VPN API и возвращает его персональную
    ссылку-подписку (sub_url). Бросает исключение при любой ошибке."""
    name = f"tg{telegram_user_id}_{int(time.time())}"  # уникально даже при продлении

    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(
            f"{H1CLOUD_API_URL}/create",
            headers={"X-API-Key": H1CLOUD_API_KEY, "Content-Type": "application/json"},
            json={"name": name, "days": days},
        )
        resp.raise_for_status()
        data = resp.json()

    if not data.get("ok"):
        raise RuntimeError(data.get("error", "H1Cloud create failed"))

    sub_url = data.get("client", {}).get("sub_url")
    if not sub_url:
        raise RuntimeError("H1Cloud не вернул sub_url")
    return sub_url


# --- Обязательная подписка на канал ---
CHANNEL_USERNAME = "@realelkavpn"
CHANNEL_URL = "https://t.me/realelkavpn"

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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS promo_codes ("
        "code TEXT PRIMARY KEY, "
        "amount REAL NOT NULL, "
        "uses_left INTEGER, "  # NULL = безлимит
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS promo_redemptions ("
        "code TEXT NOT NULL, "
        "user_id INTEGER NOT NULL, "
        "redeemed_at TEXT DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY (code, user_id)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS referrals ("
        "referred_id INTEGER PRIMARY KEY, "  # каждого приглашённого считаем только один раз
        "referrer_id INTEGER NOT NULL, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
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


def record_referral(referred_id: int, referrer_id: int):
    """Запоминает, что referred_id пришёл по ссылке referrer_id.
    Каждый приглашённый засчитывается только один раз (даже если перейдёт по ссылке снова)."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO referrals (referred_id, referrer_id) VALUES (?, ?)",
        (referred_id, referrer_id),
    )
    conn.commit()
    conn.close()


def count_referrals(referrer_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (referrer_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def get_stats() -> dict:
    conn = sqlite3.connect(DB_PATH)
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    paying_users = conn.execute("SELECT COUNT(*) FROM balances WHERE total_paid > 0").fetchone()[0]
    conn.close()
    return {"total_users": total_users, "paying_users": paying_users}


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


def subtract_balance(user_id: int, amount: float) -> bool:
    """Списывает сумму с баланса, если денег достаточно. Возвращает True при успехе."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "UPDATE balances SET total_paid = total_paid - ? WHERE user_id = ? AND total_paid >= ?",
        (amount, user_id, amount),
    )
    success = cur.rowcount > 0
    conn.commit()
    conn.close()
    return success


def admin_adjust_balance(user_id: int, amount: float) -> float:
    """Принудительно изменяет баланс на сумму amount (может быть отрицательной) —
    без проверки достаточности средств. Используется админом для ручных корректировок
    (например, после оформления возврата в Platega). Возвращает новый баланс."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO balances (user_id, total_paid) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET total_paid = total_paid + excluded.total_paid",
        (user_id, amount),
    )
    conn.commit()
    new_balance = conn.execute(
        "SELECT total_paid FROM balances WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    conn.close()
    return new_balance


def create_promo_code(code: str, amount: float, uses_left: Optional[int]):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO promo_codes (code, amount, uses_left) VALUES (?, ?, ?) "
        "ON CONFLICT(code) DO UPDATE SET amount = excluded.amount, uses_left = excluded.uses_left",
        (code, amount, uses_left),
    )
    conn.commit()
    conn.close()


def redeem_promo_code(code: str, user_id: int) -> tuple[bool, str]:
    """Возвращает (успех, сообщение для пользователя)."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT amount, uses_left FROM promo_codes WHERE code = ?", (code,)).fetchone()

    if row is None:
        conn.close()
        return False, "Такого промокода не существует."

    amount, uses_left = row

    already = conn.execute(
        "SELECT 1 FROM promo_redemptions WHERE code = ? AND user_id = ?", (code, user_id)
    ).fetchone()
    if already:
        conn.close()
        return False, "Ты уже активировал(а) этот промокод раньше."

    if uses_left is not None and uses_left <= 0:
        conn.close()
        return False, "У этого промокода закончились активации."

    if uses_left is not None:
        conn.execute("UPDATE promo_codes SET uses_left = uses_left - 1 WHERE code = ?", (code,))

    conn.execute(
        "INSERT INTO promo_redemptions (code, user_id) VALUES (?, ?)", (code, user_id)
    )
    conn.execute(
        "INSERT INTO balances (user_id, total_paid) VALUES (?, 0) ON CONFLICT(user_id) DO NOTHING",
        (user_id,),
    )
    conn.commit()
    conn.close()

    add_to_balance(user_id, amount)
    return True, f"Промокод активирован! Начислено {amount:.0f} руб."


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


def balance_menu(total: float):
    text = f"💰 Баланс: {total:.0f} руб."
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Пополнить", callback_data="topup")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
    ])
    return text, keyboard


def admin_menu():
    text = "🛠 *Админ-панель*"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("🔍 Найти пользователя", callback_data="admin_find")],
        [InlineKeyboardButton("📢 Рассылка всем", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🎁 Создать промокод", callback_data="admin_create_promo")],
        [InlineKeyboardButton("💸 Корректировка баланса", callback_data="admin_adjust")],
    ])
    return text, keyboard


def admin_back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")]])


def docs_menu():
    text = "📄 *Документы ELKA VPN*\n\nНиже — все документы сервиса:"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛡 Политика конфиденциальности", url="https://telegra.ph/Politika-konfidencialnosti-09-21-83")],
        [InlineKeyboardButton("📋 Условия использования", url="https://telegra.ph/Polzovatelskoe-soglashenie-09-21-65")],
        [InlineKeyboardButton("💵 Условия возврата", url="https://telegra.ph/Usloviya-vozvrata-10-03")],
        [InlineKeyboardButton("🔒 Политика бота", callback_data="bot_policy")],
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")],
    ])
    return text, keyboard


BOT_POLICY_TEXT = (
    "🔒 Политика конфиденциальности бота Elka VPN\n\n"
    "1. Мы храним только данные, необходимые для работы сервиса: ваш Telegram ID, username, баланс и историю покупок подписок.\n\n"
    "2. Мы не ведём логи посещённых сайтов и не анализируем содержимое VPN-трафика.\n\n"
    "3. Данные об оплате обрабатываются платёжным партнёром Platega согласно его правилам.\n\n"
    "4. Данные не передаются третьим лицам, кроме случаев, предусмотренных законом.\n\n"
    "5. Вы можете запросить удаление своих данных, обратившись в поддержку https://t.me/blelbu"
)


def docs_back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="docs")]])


def subscribe_gate_menu():
    text = (
        "Чтобы пользоваться ботом, подпишитесь на наш канал, "
        "а затем нажмите «Проверить подписку»."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Канал", url=CHANNEL_URL)],
        [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_subscription")],
    ])
    return text, keyboard


async def is_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


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
                text="✅ Оплата получена!",
                reply_markup=back_only_keyboard(),
            )
        except Exception:
            await context.bot.send_message(
                chat_id=data["chat_id"],
                text="✅ Оплата получена!",
                reply_markup=back_only_keyboard(),
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

    # Если пользователь пришёл по реферальной ссылке — запоминаем, кто его пригласил
    if context.args:
        payload = context.args[0]
        if payload.startswith("ref_"):
            try:
                referrer_id = int(payload[len("ref_"):])
                if referrer_id != update.effective_user.id:  # нельзя пригласить самого себя
                    record_referral(update.effective_user.id, referrer_id)
            except ValueError:
                pass

    if not await is_subscribed(context, update.effective_user.id):
        text, keyboard = subscribe_gate_menu()
        await update.message.reply_text(text, reply_markup=keyboard)
        return

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
            f"💳 Оплативших хотя бы раз: {stats['paying_users']}",
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

    if query.data == "admin_create_promo":
        context.user_data["admin_awaiting"] = "create_promo"
        await query.edit_message_text(
            "Пришли промокод в формате:\n\n"
            "`КОД СУММА КОЛИЧЕСТВО`\n\n"
            "Например: `SALE50 100 10` — код SALE50, начисляет 100 руб., можно активировать 10 раз всего.\n"
            "Если количество не указать — промокод будет безлимитным.\n"
            "Например просто: `SALE50 100`",
            reply_markup=admin_back_keyboard(),
            parse_mode="Markdown",
        )
        return True

    if query.data.startswith("adj_plus_") or query.data.startswith("adj_minus_"):
        sign = 1 if query.data.startswith("adj_plus_") else -1
        target_id = int(query.data.split("_")[-1])
        context.user_data["admin_awaiting"] = "adjust_amount"
        context.user_data["adjust_target_id"] = target_id
        context.user_data["adjust_sign"] = sign
        action = "начислить" if sign == 1 else "списать"
        await query.edit_message_text(
            f"Сколько руб. {action} пользователю {target_id}? Пришли число.",
            reply_markup=admin_back_keyboard(),
        )
        return True

    if query.data == "admin_adjust":
        context.user_data["admin_awaiting"] = "adjust"
        await query.edit_message_text(
            "Пришли в формате:\n\n"
            "`ID СУММА`\n\n"
            "Например: `123456789 -100` — спишет 100 руб. с баланса (используй при возврате в Platega).\n"
            "Со знаком `+` или без знака — наоборот, начислит. Например: `123456789 50`",
            reply_markup=admin_back_keyboard(),
            parse_mode="Markdown",
        )
        return True

    return False


async def notify_user_balance_change(context: ContextTypes.DEFAULT_TYPE, user_id: int, delta: float, new_balance: float):
    """Шлёт пользователю сообщение о том, что админ изменил его баланс.
    Если не получилось (например, пользователь ни разу не писал боту) — тихо игнорируем."""
    if delta >= 0:
        text = (
            f"💰 Вам начислено {delta:.0f} руб.\n"
            f"Текущий баланс: {new_balance:.0f} руб."
        )
    else:
        text = (
            f"⚠️ С вашего баланса списано {abs(delta):.0f} руб.\n"
            f"Текущий баланс: {new_balance:.0f} руб."
        )
    try:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=back_only_keyboard())
    except Exception:
        pass


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Единый обработчик обычных текстовых сообщений: ловит промокоды и суммы пополнения
    от любых пользователей, и ответы админа в режиме поиска/рассылки/создания промокода."""

    # --- Пользователь вводит сумму пополнения баланса ---
    if context.user_data.get("awaiting_topup"):
        context.user_data["awaiting_topup"] = False

        raw = update.message.text.strip().replace(",", ".")
        try:
            amount = float(raw)
        except ValueError:
            await update.message.reply_text(
                "Это не похоже на число. Открой раздел «Баланс» ещё раз и попробуй снова.",
                reply_markup=back_only_keyboard(),
            )
            return

        if amount <= 0:
            await update.message.reply_text(
                "Сумма должна быть больше нуля. Открой раздел «Баланс» ещё раз и попробуй снова.",
                reply_markup=back_only_keyboard(),
            )
            return

        try:
            payment = await create_payment(amount=amount, description="Пополнение баланса ELKA VPN")
        except Exception:
            await update.message.reply_text(
                "Не получилось создать платёж. Попробуй ещё раз чуть позже.",
                reply_markup=back_only_keyboard(),
            )
            return

        pay_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Оплатить", url=payment["redirect"])],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
        ])
        sent = await update.message.reply_text(
            f"Пополнение: {amount:.0f} руб.\n"
            f"Нажми кнопку ниже, чтобы перейти к оплате. После оплаты бот сам зачислит сумму на баланс.",
            reply_markup=pay_keyboard,
        )

        context.job_queue.run_repeating(
            check_payment_job,
            interval=CHECK_INTERVAL,
            first=CHECK_INTERVAL,
            data={
                "transaction_id": payment["transactionId"],
                "chat_id": update.effective_chat.id,
                "message_id": sent.message_id,
                "user_id": update.effective_user.id,
                "amount": amount,
                "plan_label": "Пополнение баланса",
                "attempts": 0,
            },
            name=f"check_{payment['transactionId']}",
        )
        return

    # --- Пользователь вводит промокод ---
    if context.user_data.get("awaiting_promo"):
        context.user_data["awaiting_promo"] = False
        code = update.message.text.strip().upper()
        success, message = redeem_promo_code(code, update.effective_user.id)
        await update.message.reply_text(message, reply_markup=back_only_keyboard())
        return

    # --- Дальше — только для админа ---
    if update.effective_user.id != ADMIN_ID:
        return

    awaiting = context.user_data.get("admin_awaiting")
    if not awaiting:
        return  # админ просто пишет боту не в контексте админки — игнорируем

    context.user_data["admin_awaiting"] = None

    if awaiting == "find":
        # Если админ переслал сообщение от пользователя — берём ID прямо оттуда
        if isinstance(update.message.forward_origin, MessageOriginUser):
            target_id = update.message.forward_origin.sender_user.id
        else:
            try:
                target_id = int(update.message.text.strip())
            except ValueError:
                await update.message.reply_text(
                    "Это не похоже на числовой ID. Пришли ID цифрами или перешли сюда "
                    "любое сообщение от нужного пользователя.",
                    reply_markup=admin_back_keyboard(),
                )
                return

        total = get_balance(target_id)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("➕ Начислить", callback_data=f"adj_plus_{target_id}"),
                InlineKeyboardButton("➖ Списать", callback_data=f"adj_minus_{target_id}"),
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")],
        ])
        await update.message.reply_text(
            f"Пользователь {target_id}: баланс {total:.0f} руб.",
            reply_markup=keyboard,
        )

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
        await update.message.reply_text(
            f"Рассылка завершена. Доставлено: {sent}, не удалось: {failed}.",
            reply_markup=admin_back_keyboard(),
        )

    elif awaiting == "create_promo":
        parts = update.message.text.strip().split()
        if len(parts) not in (2, 3):
            await update.message.reply_text(
                "Неверный формат. Нужно: КОД СУММА [КОЛИЧЕСТВО]. Попробуй ещё раз через /admin.",
                reply_markup=admin_back_keyboard(),
            )
            return

        code = parts[0].upper()
        try:
            amount = float(parts[1])
        except ValueError:
            await update.message.reply_text(
                "Сумма должна быть числом. Попробуй ещё раз через /admin.",
                reply_markup=admin_back_keyboard(),
            )
            return

        uses_left = None
        if len(parts) == 3:
            try:
                uses_left = int(parts[2])
            except ValueError:
                await update.message.reply_text(
                    "Количество активаций должно быть целым числом.",
                    reply_markup=admin_back_keyboard(),
                )
                return

        create_promo_code(code, amount, uses_left)
        uses_text = "безлимитный" if uses_left is None else f"{uses_left} активаций"
        await update.message.reply_text(
            f"✅ Промокод «{code}» создан: {amount:.0f} руб., {uses_text}.",
            reply_markup=admin_back_keyboard(),
        )

    elif awaiting == "adjust":
        parts = update.message.text.strip().split()
        if len(parts) != 2:
            await update.message.reply_text(
                "Неверный формат. Нужно: ID СУММА. Попробуй ещё раз через /admin.",
                reply_markup=admin_back_keyboard(),
            )
            return

        try:
            target_id = int(parts[0])
            delta = float(parts[1])
        except ValueError:
            await update.message.reply_text(
                "ID должен быть целым числом, сумма — числом (можно со знаком -). Попробуй ещё раз через /admin.",
                reply_markup=admin_back_keyboard(),
            )
            return

        new_balance = admin_adjust_balance(target_id, delta)
        action = "списано" if delta < 0 else "начислено"
        await update.message.reply_text(
            f"Готово. У пользователя {target_id} {action} {abs(delta):.0f} руб.\n"
            f"Текущий баланс: {new_balance:.0f} руб.",
            reply_markup=admin_back_keyboard(),
        )
        await notify_user_balance_change(context, target_id, delta, new_balance)

    elif awaiting == "adjust_amount":
        target_id = context.user_data.get("adjust_target_id")
        sign = context.user_data.get("adjust_sign", 1)

        try:
            value = float(update.message.text.strip().replace(",", "."))
        except ValueError:
            await update.message.reply_text(
                "Это не похоже на число. Попробуй ещё раз через /admin.",
                reply_markup=admin_back_keyboard(),
            )
            return

        delta = abs(value) * sign
        new_balance = admin_adjust_balance(target_id, delta)
        action = "начислено" if sign == 1 else "списано"
        await update.message.reply_text(
            f"Готово. Пользователю {target_id} {action} {abs(value):.0f} руб.\n"
            f"Текущий баланс: {new_balance:.0f} руб.",
            reply_markup=admin_back_keyboard(),
        )
        await notify_user_balance_change(context, target_id, delta, new_balance)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data["awaiting_promo"] = False
    context.user_data["awaiting_topup"] = False

    if query.data.startswith("admin_") or query.data.startswith("adj_"):
        handled = await admin_button_handler(update, context)
        if handled:
            return

    if query.data == "check_subscription":
        if await is_subscribed(context, query.from_user.id):
            text, keyboard = main_menu()
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        # если подписки нет — ничего не делаем, query.answer() уже вызван выше
        return

    if query.data == "buy_subscription":
        text, keyboard = plans_menu()
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")

    elif query.data in PLANS:
        plan = PLANS[query.data]
        user_id = query.from_user.id
        current_balance = get_balance(user_id)

        if current_balance < plan["amount"]:
            missing = plan["amount"] - current_balance
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Пополнить баланс", callback_data="topup")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
            ])
            await query.edit_message_text(
                f"Недостаточно средств на балансе.\n\n"
                f"Тариф «{plan['label']}» стоит {plan['amount']:.0f} руб.\n"
                f"На балансе: {current_balance:.0f} руб.\n"
                f"Не хватает: {missing:.0f} руб.",
                reply_markup=keyboard,
            )
            return

        confirm_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить оплату", callback_data=f"confirm_{query.data}")],
            [InlineKeyboardButton("❌ Отмена", callback_data="buy_subscription")],
        ])
        await query.edit_message_text(
            f"Тариф: {plan['label']} — {plan['amount']:.0f} руб.\n"
            f"Текущий баланс: {current_balance:.0f} руб.\n\n"
            f"Подтвердить списание с баланса?",
            reply_markup=confirm_keyboard,
        )

    elif query.data.startswith("confirm_") and query.data[len("confirm_"):] in PLANS:
        plan_key = query.data[len("confirm_"):]
        plan = PLANS[plan_key]
        user_id = query.from_user.id

        success = subtract_balance(user_id, plan["amount"])
        if not success:
            # Баланс мог измениться между подтверждением и списанием (например, уже потратил в другом месте)
            await query.edit_message_text(
                "Не получилось списать средства — возможно, баланс изменился. Попробуй ещё раз.",
                reply_markup=back_only_keyboard(),
            )
            return

        try:
            sub_url = await h1cloud_create_client(user_id, plan["days"])
            text = (
                "✅ Оплата прошла!\n\n"
                "Ваша персональная ссылка-подписка:\n"
                f"{sub_url}\n\n"
                "Добавьте эту ссылку в приложение (v2rayNG, NekoBox, Happ, Streisand и т.п.) — "
                "там появятся все доступные сервера."
            )
        except Exception:
            # Деньги уже списаны — возвращаем их, раз ключ выдать не получилось
            admin_adjust_balance(user_id, plan["amount"])
            text = (
                "Оплата прошла, но не получилось автоматически выдать ключ. "
                "Деньги возвращены на баланс — попробуйте ещё раз чуть позже или напишите в поддержку."
            )

        await query.edit_message_text(text, reply_markup=back_only_keyboard())

    elif query.data == "back_to_menu":
        text, keyboard = main_menu()
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")

    elif query.data == "my_subscription":
        await query.edit_message_text("Скоро.", reply_markup=back_only_keyboard())

    elif query.data == "balance":
        total = get_balance(query.from_user.id)
        text, keyboard = balance_menu(total)
        await query.edit_message_text(text, reply_markup=keyboard)

    elif query.data == "topup":
        context.user_data["awaiting_topup"] = True
        await query.edit_message_text(
            "Напишите любую сумму, которую хотите пополнить (например: 100)",
            reply_markup=back_only_keyboard(),
        )

    elif query.data == "promo":
        context.user_data["awaiting_promo"] = True
        await query.edit_message_text("Введите промокод:", reply_markup=back_only_keyboard())

    elif query.data == "invite":
        bot_username = context.bot.username
        link = f"https://t.me/{bot_username}?start=ref_{query.from_user.id}"
        count = count_referrals(query.from_user.id)
        await query.edit_message_text(
            f"👥 Ваша реферальная ссылка:\n{link}\n\n"
            f"Приглашено друзей: {count}",
            reply_markup=back_only_keyboard(),
        )

    elif query.data == "docs":
        text, keyboard = docs_menu()
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")

    elif query.data == "bot_policy":
        await query.edit_message_text(BOT_POLICY_TEXT, reply_markup=docs_back_keyboard())


init_db()

app = ApplicationBuilder().token(Token).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("admin", admin_command))
app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

app.run_polling()
