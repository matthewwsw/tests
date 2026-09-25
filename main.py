import os
import asyncio
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

Token = os.environ["BOT_TOKEN"]

# --- Platega ---
PLATEGA_MERCHANT_ID = os.environ["PLATEGA_MERCHANT_ID"]
PLATEGA_SECRET = os.environ["PLATEGA_SECRET"]
PLATEGA_BASE_URL = "https://app.platega.io"

# Тарифы: callback_data -> (сумма в руб, подпись)
PLANS = {
    "plan_1m": {"amount": 80, "label": "1 месяц"},
    "plan_3m": {"amount": 220, "label": "3 месяца"},
}

# Сколько раз подряд проверять статус оплаты и с каким интервалом (секунды)
CHECK_INTERVAL = 10
MAX_CHECKS = 90  # 90 * 10 сек = 15 минут — под expiresIn из ответа Platega


async def create_payment(amount: float, description: str) -> dict:
    """Создаёт платёж в Platega и возвращает ответ API (там есть 'redirect' и 'transactionId')."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PLATEGA_BASE_URL}/transaction/process",
            headers={
                "X-MerchantId": PLATEGA_MERCHANT_ID,
                "X-Secret": PLATEGA_SECRET,
                "Content-Type": "application/json",
            },
            json={
                # 2 = СБП (см. таблицу PaymentMethodInt в доках Platega — уточни код у менеджера,
                # если нужен другой способ оплаты, например карта).
                "paymentMethod": 2,
                "paymentDetails": {"amount": amount, "currency": "RUB"},
                "description": description,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()


async def check_payment_status(transaction_id: str) -> str:
    """Возвращает статус транзакции строкой (например 'PENDING', 'CONFIRMED', ...).
    ВАЖНО: путь ниже — по аналогии с остальным API Platega.
    Сверь точный путь в разделе 'Проверка статуса оплаты платежа' на docs.platega.io —
    если Platega ответит 404, поменяй путь здесь на тот, что указан в доке."""
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

    # Если после первой реальной оплаты окажется, что Platega возвращает другое слово для
    # "оплачено" — посмотри его в логах Railway (print(status) ниже) и поправь список тут.
    print(f"[Platega] transaction={data['transaction_id']} status={status}")

    if status in ("CONFIRMED", "PAID", "SUCCESS", "SUCCEEDED"):
        await context.bot.send_message(
            chat_id=data["chat_id"],
            text=f"✅ Оплата получена! Подписка «{data['plan_label']}» активирована.",
        )
        # Здесь позже добавим реальную активацию подписки (запись в базу и т.п.)
        job.schedule_removal()
    elif status in ("EXPIRED", "FAILED", "CANCELLED", "CANCELED", "DECLINED"):
        await context.bot.send_message(
            chat_id=data["chat_id"],
            text="❌ Платёж не прошёл или истёк. Попробуй оформить оплату заново через меню.",
        )
        job.schedule_removal()
    elif data["attempts"] >= MAX_CHECKS:
        await context.bot.send_message(
            chat_id=data["chat_id"],
            text="⌛ Время ожидания оплаты истекло. Если уже оплатил(а) — напиши в поддержку.",
        )
        job.schedule_removal()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def send_tracked(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, text: str, reply_markup=None):
    last_msgs = context.user_data.setdefault("last_msgs", {})

    old_msg_id = last_msgs.get(key)
    if old_msg_id:
        await asyncio.sleep(0.3)
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=old_msg_id)
        except Exception:
            pass

    sent = await update.effective_message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    last_msgs[key] = sent.message_id


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "buy_subscription":
        plans_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("1 месяц — 80 руб", callback_data="plan_1m")],
            [InlineKeyboardButton("3 месяца — 220 руб", callback_data="plan_3m")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")],
        ])
        await send_tracked(update, context, "buy_subscription", "Выберите срок подписки:", plans_keyboard)

    elif query.data in PLANS:
        plan = PLANS[query.data]

        try:
            payment = await create_payment(
                amount=plan["amount"],
                description=f"Подписка ELKA VPN — {plan['label']}",
            )
        except Exception:
            await send_tracked(
                update, context, query.data,
                "Не получилось создать платёж. Попробуй ещё раз чуть позже.",
            )
            return

        pay_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Оплатить", url=payment["redirect"])],
        ])
        await send_tracked(
            update, context, query.data,
            f"Тариф: {plan['label']} — {plan['amount']} руб.\n"
            f"Нажми кнопку ниже, чтобы перейти к оплате. После оплаты бот сам подтвердит платёж.",
            pay_keyboard,
        )

        context.job_queue.run_repeating(
            check_payment_job,
            interval=CHECK_INTERVAL,
            first=CHECK_INTERVAL,
            data={
                "transaction_id": payment["transactionId"],
                "chat_id": update.effective_chat.id,
                "plan_label": plan["label"],
                "attempts": 0,
            },
            name=f"check_{payment['transactionId']}",
        )

    elif query.data == "back_to_menu":
        await start(update, context)
    elif query.data == "my_subscription":
        await send_tracked(update, context, "my_subscription", "Скоро.")
    elif query.data == "balance":
        await send_tracked(update, context, "balance", "Скоро")
    elif query.data == "promo":
        await send_tracked(update, context, "promo", "Введите промокод:")
    elif query.data == "invite":
        await send_tracked(update, context, "invite", "Скоро")
    elif query.data == "docs":
        await send_tracked(
            update, context, "docs",
            "Пользовательское Соглашение:\n"
            "https://telegra.ph/Polzovatelskoe-soglashenie-09-21-65\n\n"
            "Политика Конфиденциальности:\n"
            "https://telegra.ph/Politika-konfidencialnosti-09-21-83"
        )


app = ApplicationBuilder().token(Token).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(button_handler))

app.run_polling()
