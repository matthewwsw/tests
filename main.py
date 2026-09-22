import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

Token = os.environ["BOT_TOKEN"]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*ELKA VPN*\n\n"
        "От 1 Сервера\n"
        "Без логов подключений\n"
        "Надёжное подключение\n"        
        "Высокая скорость соединения"
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

    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # обязательно, иначе кнопка будет "крутиться"

    if query.data == "buy_subscription":
        await query.message.reply_text("Скоро.")
    elif query.data == "my_subscription":
        await query.message.reply_text("Скоро.")
    elif query.data == "balance":
        await query.message.reply_text("Скоро")
    elif query.data == "promo":
        await query.message.reply_text("Введите промокод:")
    elif query.data == "invite":
        await query.message.reply_text("Скоро")
    elif query.data == "docs":
        await query.message.reply_text(
            "Пользовательское Соглашение:\n"
            "https://telegra.ph/Polzovatelskoe-soglashenie-09-21-65\n\n"
            "Политика Конфиденциальности:\n"
            "https://telegra.ph/Politika-konfidencialnosti-09-21-83"
        )

app = ApplicationBuilder().token(Token).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(button_handler))  # добавь этот импорт ниже

app.run_polling()
