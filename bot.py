import logging
import os

import psycopg
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CATEGORY_CHOICE, AMOUNT_INPUT, DESCRIPTION_INPUT = range(3)

CATEGORIES = [
    "Еда",
    "Транспорт",
    "Кофе",
    "Развлечения",
    "Покупки",
    "Другое",
]

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
ALLOWED_USER_IDS_RAW = os.getenv("ALLOWED_USER_IDS", "")


def parse_allowed_user_ids() -> set[int]:
    result = set()
    for part in ALLOWED_USER_IDS_RAW.split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return result


ALLOWED_USER_IDS = parse_allowed_user_ids()


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан")
    return psycopg.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS expenses (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    category TEXT NOT NULL,
                    amount NUMERIC(12, 2) NOT NULL,
                    description TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
        conn.commit()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message or not is_allowed(user.id):
        if update.message:
            await update.message.reply_text("У тебя нет доступа к этому боту.")
        return

    await update.message.reply_text(
        "Привет! Я бот для учета расходов.
"
        "Команды:
"
        "/add — добавить расход
"
        "/today — сумма за сегодня
"
        "/month — сумма за месяц
"
        "/last — последние 10 записей
"
        "/categories — суммы по категориям за месяц
"
        "/cancel — отмена"
    )


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message or not is_allowed(user.id):
        return ConversationHandler.END

    keyboard = [[c] for c in CATEGORIES]
    await update.message.reply_text(
        "Выбери категорию:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True),
    )
    return CATEGORY_CHOICE


async def add_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message or not is_allowed(user.id):
        return ConversationHandler.END

    category = update.message.text.strip()
    if category not in CATEGORIES:
        await update.message.reply_text("Выбери категорию кнопкой ниже.")
        return CATEGORY_CHOICE

    context.user_data["new_expense_category"] = category
    await update.message.reply_text(
        f"Категория: {category}
Теперь введи сумму, например: 350",
        reply_markup=ReplyKeyboardRemove(),
    )
    return AMOUNT_INPUT


async def add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message or not is_allowed(user.id):
        return ConversationHandler.END

    text = update.message.text.strip().replace(",", ".").replace("р", "").replace("₽", "").strip()

    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text("Введите сумму числом, например: 350")
        return AMOUNT_INPUT

    if amount <= 0:
        await update.message.reply_text("Сумма должна быть больше нуля.")
        return AMOUNT_INPUT

    context.user_data["new_expense_amount"] = amount
    await update.message.reply_text("Теперь введи описание, например: кофе в Starbucks")
    return DESCRIPTION_INPUT


async def add_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message or not is_allowed(user.id):
        return ConversationHandler.END

    description = update.message.text.strip()
    category = context.user_data.get("new_expense_category")
    amount = context.user_data.get("new_expense_amount")

    if not category or amount is None:
        await update.message.reply_text("Сессия сбилась. Нажми /add заново.")
        return ConversationHandler.END

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO expenses (user_id, category, amount, description)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (user.id, category, amount, description),
                )
            conn.commit()
    except Exception:
        logger.exception("Failed to insert expense")
        await update.message.reply_text("Не смог сохранить расход. Проверь базу и попробуй еще раз.")
        return ConversationHandler.END

    await update.message.reply_text(
        f"Записал расход:
"
        f"Категория: {category}
"
        f"Сумма: {amount:.2f}
"
        f"Описание: {description}"
    )

    context.user_data.pop("new_expense_category", None)
    context.user_data.pop("new_expense_amount", None)

    return ConversationHandler.END


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message or not is_allowed(user.id):
        return

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0)
                    FROM expenses
                    WHERE user_id = %s
                      AND created_at::date = CURRENT_DATE
                    """,
                    (user.id,),
                )
                total = cur.fetchone()[0]
    except Exception:
        logger.exception("Failed to read today expenses")
        await update.message.reply_text("Не смог прочитать данные из базы.")
        return

    await update.message.reply_text(f"За сегодня: {float(total):.2f}")


async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message or not is_allowed(user.id):
        return

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0)
                    FROM expenses
                    WHERE user_id = %s
                      AND date_trunc('month', created_at) = date_trunc('month', CURRENT_DATE)
                    """,
                    (user.id,),
                )
                total = cur.fetchone()[0]
    except Exception:
        logger.exception("Failed to read month expenses")
        await update.message.reply_text("Не смог прочитать данные из базы.")
        return

    await update.message.reply_text(f"За этот месяц: {float(total):.2f}")


async def last_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message or not is_allowed(user.id):
        return

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT category, amount, description, created_at
                    FROM expenses
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT 10
                    """,
                    (user.id,),
                )
                rows = cur.fetchall()
    except Exception:
        logger.exception("Failed to read last expenses")
        await update.message.reply_text("Не смог прочитать данные из базы.")
        return

    if not rows:
        await update.message.reply_text("Пока нет расходов.")
        return

    lines = ["Последние расходы:"]
    for category, amount, description, created_at in rows:
        dt = created_at.strftime("%d.%m %H:%M")
        desc = description or "Без описания"
        lines.append(f"{dt} | {category} | {float(amount):.2f} | {desc}")

    await update.message.reply_text("
".join(lines))


async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message or not is_allowed(user.id):
        return

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT category, COALESCE(SUM(amount), 0) AS total
                    FROM expenses
                    WHERE user_id = %s
                      AND date_trunc('month', created_at) = date_trunc('month', CURRENT_DATE)
                    GROUP BY category
                    ORDER BY total DESC
                    """,
                    (user.id,),
                )
                rows = cur.fetchall()
    except Exception:
        logger.exception("Failed to read category summary")
        await update.message.reply_text("Не смог прочитать данные из базы.")
        return

    if not rows:
        await update.message.reply_text("За этот месяц расходов по категориям пока нет.")
        return

    lines = ["Категории за месяц:"]
    for category, total in rows:
        lines.append(f"{category}: {float(total):.2f}")

    await update.message.reply_text("
".join(lines))


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("Ок, отменил.", reply_markup=ReplyKeyboardRemove())
    context.user_data.pop("new_expense_category", None)
    context.user_data.pop("new_expense_amount", None)
    return ConversationHandler.END


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            CATEGORY_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_category)],
            AMOUNT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount)],
            DESCRIPTION_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_description)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("last", last_expenses))
    app.add_handler(CommandHandler("categories", categories))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(conv_handler)

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()


