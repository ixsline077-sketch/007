import logging
import os
from decimal import Decimal, InvalidOperation

import psycopg
from psycopg.rows import dict_row
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

CATEGORY_CHOICE, AMOUNT_INPUT = range(2)

CATEGORIES = [
    "Еда",
    "Транспорт",
    "Кофе",
    "Развлечения",
    "Покупки",
    "Другое",
]


def get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} не задан")
    return value


BOT_TOKEN = get_required_env("BOT_TOKEN")
ALLOWED_USER_IDS_RAW = os.getenv("ALLOWED_USER_IDS", "").strip()


def normalize_database_url(raw_url: str | None) -> str:
    url = (raw_url or "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL не задан")

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    return url


DATABASE_URL = normalize_database_url(
    os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")
)


def parse_allowed_user_ids(raw_value: str) -> set[int]:
    result = set()
    for part in raw_value.split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return result


ALLOWED_USER_IDS = parse_allowed_user_ids(ALLOWED_USER_IDS_RAW)


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def get_conn():
    return psycopg.connect(
        DATABASE_URL,
        autocommit=False,
        connect_timeout=10,
        row_factory=dict_row,
    )


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS expenses (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    category TEXT NOT NULL,
                    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
                    description TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )

            cur.execute(
                """
                ALTER TABLE expenses
                ADD COLUMN IF NOT EXISTS chat_id BIGINT
                """
            )
            cur.execute(
                """
                ALTER TABLE expenses
                ADD COLUMN IF NOT EXISTS source_message_id BIGINT
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_expenses_user_created_at
                ON expenses (user_id, created_at DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_expenses_user_message
                ON expenses (user_id, chat_id, source_message_id)
                """
            )
        conn.commit()


def clear_expense_draft(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("new_expense_category", None)
    context.user_data.pop("new_expense_amount", None)
    context.user_data.pop("new_expense_source_message_id", None)
    context.user_data.pop("new_expense_chat_id", None)


def parse_amount(text: str) -> Decimal:
    normalized = (
        text.strip()
        .replace("₽", "")
        .replace("р.", "")
        .replace("р", "")
        .replace(" ", "")
        .replace(",", ".")
    )

    amount = Decimal(normalized)
    amount = amount.quantize(Decimal("0.01"))

    if amount <= 0:
        raise InvalidOperation("Amount must be positive")

    return amount


async def deny_access(update: Update) -> None:
    if update.message:
        await update.message.reply_text("У тебя нет доступа к этому боту.")
    elif update.edited_message:
        await update.edited_message.reply_text("У тебя нет доступа к этому боту.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return

    if not is_allowed(user.id):
        await deny_access(update)
        return

    await update.message.reply_text(
        "Привет! Я бот для записи расходов.\n\n"
        "Команды:\n"
        "/add — добавить расход\n"
        "/today — сумма за сегодня\n"
        "/month — сумма за месяц\n"
        "/last — последние 10 записей\n"
        "/categories — суммы по категориям за месяц\n"
        "/delete 123 — удалить расход по id\n"
        "/cancel — отмена\n\n"
        "Если ошибся в сумме, просто отредактируй сообщение с суммой."
    )


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return ConversationHandler.END

    if not is_allowed(user.id):
        await deny_access(update)
        return ConversationHandler.END

    clear_expense_draft(context)

    keyboard = [[c] for c in CATEGORIES]
    await update.message.reply_text(
        "Выбери категорию:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )
    return CATEGORY_CHOICE


async def add_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return ConversationHandler.END

    if not is_allowed(user.id):
        await deny_access(update)
        return ConversationHandler.END

    category = update.message.text.strip()
    if category not in CATEGORIES:
        await update.message.reply_text("Выбери категорию кнопкой ниже.")
        return CATEGORY_CHOICE

    context.user_data["new_expense_category"] = category
    await update.message.reply_text(
        f"Категория: {category}\n"
        "Теперь введи сумму, например: 350 или 199.90",
        reply_markup=ReplyKeyboardRemove(),
    )
    return AMOUNT_INPUT


async def add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return ConversationHandler.END

    if not is_allowed(user.id):
        await deny_access(update)
        return ConversationHandler.END

    try:
        amount = parse_amount(update.message.text)
    except (InvalidOperation, ValueError):
        await update.message.reply_text("Введите сумму числом, например: 350 или 199.90")
        return AMOUNT_INPUT

    category = context.user_data.get("new_expense_category")
    if not category:
        clear_expense_draft(context)
        await update.message.reply_text("Сессия сбилась. Нажми /add заново.")
        return ConversationHandler.END

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO expenses (
                        user_id, chat_id, source_message_id, category, amount, description
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        user.id,
                        update.message.chat_id,
                        update.message.message_id,
                        category,
                        amount,
                        None,
                    ),
                )
            conn.commit()
    except Exception:
        logger.exception("Failed to insert expense")
        await update.message.reply_text(
            "Не смог сохранить расход. Проверь подключение к базе и попробуй еще раз."
        )
        return ConversationHandler.END
    finally:
        clear_expense_draft(context)

    await update.message.reply_text(
        f"Записал расход:\n"
        f"Категория: {category}\n"
        f"Сумма: {amount:.2f}"
    )

    return ConversationHandler.END


async def handle_edited_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.edited_message
    if not message or not message.from_user or not message.text:
        return

    user_id = message.from_user.id
    if not is_allowed(user_id):
        return

    try:
        new_amount = parse_amount(message.text)
    except (InvalidOperation, ValueError):
        return

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, amount
                    FROM expenses
                    WHERE user_id = %s
                      AND chat_id = %s
                      AND source_message_id = %s
                      AND source_message_id IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (user_id, message.chat_id, message.message_id),
                )
                row = cur.fetchone()

                if not row:
                    return

                old_amount = row["amount"]

                if Decimal(old_amount) == new_amount:
                    return

                cur.execute(
                    """
                    UPDATE expenses
                    SET amount = %s
                    WHERE id = %s
                    """,
                    (new_amount, row["id"]),
                )
            conn.commit()
    except Exception:
        logger.exception("Failed to update edited expense amount")
        return

    await message.reply_text(
        f"Сумму обновил: {Decimal(old_amount):.2f} -> {new_amount:.2f}"
    )


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return

    if not is_allowed(user.id):
        await deny_access(update)
        return

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0) AS total
                    FROM expenses
                    WHERE user_id = %s
                      AND created_at::date = CURRENT_DATE
                    """,
                    (user.id,),
                )
                row = cur.fetchone()
                total = row["total"]
    except Exception:
        logger.exception("Failed to read today expenses")
        await update.message.reply_text("Не смог прочитать данные из базы.")
        return

    await update.message.reply_text(f"За сегодня: {Decimal(total):.2f}")


async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return

    if not is_allowed(user.id):
        await deny_access(update)
        return

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0) AS total
                    FROM expenses
                    WHERE user_id = %s
                      AND date_trunc('month', created_at) = date_trunc('month', CURRENT_DATE)
                    """,
                    (user.id,),
                )
                row = cur.fetchone()
                total = row["total"]
    except Exception:
        logger.exception("Failed to read month expenses")
        await update.message.reply_text("Не смог прочитать данные из базы.")
        return

    await update.message.reply_text(f"За этот месяц: {Decimal(total):.2f}")


async def last_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return

    if not is_allowed(user.id):
        await deny_access(update)
        return

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, category, amount, description, created_at
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
    for row in rows:
        dt = row["created_at"].strftime("%d.%m %H:%M")
        lines.append(
            f"#{row['id']} | {dt} | {row['category']} | {Decimal(row['amount']):.2f}"
        )

    await update.message.reply_text("\n".join(lines))


async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return

    if not is_allowed(user.id):
        await deny_access(update)
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
                    ORDER BY total DESC, category ASC
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
    for row in rows:
        lines.append(f"{row['category']}: {Decimal(row['total']):.2f}")

    await update.message.reply_text("\n".join(lines))


async def delete_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return

    if not is_allowed(user.id):
        await deny_access(update)
        return

    if len(context.args) != 1 or not context.args[0].strip().isdigit():
        await update.message.reply_text("Используй: /delete 123")
        return

    expense_id = int(context.args[0].strip())

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, category, amount, description, created_at
                    FROM expenses
                    WHERE user_id = %s AND id = %s
                    LIMIT 1
                    """,
                    (user.id, expense_id),
                )
                row = cur.fetchone()

                if not row:
                    await update.message.reply_text("Расход не найден.")
                    return

                cur.execute(
                    """
                    DELETE FROM expenses
                    WHERE user_id = %s AND id = %s
                    """,
                    (user.id, expense_id),
                )
            conn.commit()
    except Exception:
        logger.exception("Failed to delete expense")
        await update.message.reply_text("Не смог удалить расход из базы.")
        return

    await update.message.reply_text(
        f"Удалил расход:\n"
        f"#{row['id']} | {row['category']} | {Decimal(row['amount']):.2f}"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_expense_draft(context)
    if update.message:
        await update.message.reply_text(
            "Ок, отменил добавление расхода.",
            reply_markup=ReplyKeyboardRemove(),
        )
    return ConversationHandler.END


def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            CATEGORY_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_category)],
            AMOUNT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("last", last_expenses))
    app.add_handler(CommandHandler("categories", categories))
    app.add_handler(CommandHandler("delete", delete_expense))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(conv_handler)
    app.add_handler(
        MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.TEXT, handle_edited_amount)
    )

    logger.info("Bot started")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
