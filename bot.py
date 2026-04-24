import logging
import os
import sqlite3
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    ConversationHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_USER_IDS_RAW = os.getenv("ALLOWED_USER_IDS", "")
CHAT_ID = os.getenv("CHAT_ID")
TZ_NAME = os.getenv("TZ", "Europe/Moscow")
DB_PATH = os.getenv("DB_PATH", "expenses.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not ALLOWED_USER_IDS_RAW:
    raise RuntimeError("ALLOWED_USER_IDS is not set")
if not CHAT_ID:
    raise RuntimeError("CHAT_ID is not set")

ALLOWED_USER_IDS = {
    int(x.strip()) for x in ALLOWED_USER_IDS_RAW.split(",") if x.strip()
}
CHAT_ID = int(CHAT_ID)
TZ = ZoneInfo(TZ_NAME)

CATEGORIES = ["Еда", "Самокат", "Фаст-фуд", "Матрешки"]

CATEGORY_CHOICE, AMOUNT_INPUT = range(2)

category_keyboard = ReplyKeyboardMarkup(
    [
        ["Еда", "Самокат"],
        ["Фаст-фуд", "Матрешки"],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS

def now_msk() -> datetime:
    return datetime.now(TZ)

def format_money(value: float) -> str:
    return f"{value:.0f} ₽" if value == int(value) else f"{value:.2f} ₽"

def add_expense(user_id: int, username: str, amount: float, category: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO expenses (user_id, username, amount, category, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, username, amount, category, now_msk().isoformat()),
    )
    conn.commit()
    conn.close()

def sum_between(start_dt: datetime, end_dt: datetime):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COALESCE(SUM(amount), 0) as total
        FROM expenses
        WHERE created_at >= ? AND created_at < ?
        """,
        (start_dt.isoformat(), end_dt.isoformat()),
    )
    total = cur.fetchone()["total"] or 0
    conn.close()
    return float(total)

def category_stats_between(start_dt: datetime, end_dt: datetime):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT category, COALESCE(SUM(amount), 0) as total
        FROM expenses
        WHERE created_at >= ? AND created_at < ?
        GROUP BY category
        ORDER BY total DESC
        """,
        (start_dt.isoformat(), end_dt.isoformat()),
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def user_stats_between(start_dt: datetime, end_dt: datetime):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COALESCE(username, CAST(user_id AS TEXT)) as name, COALESCE(SUM(amount), 0) as total
        FROM expenses
        WHERE created_at >= ? AND created_at < ?
        GROUP BY user_id, username
        ORDER BY total DESC
        """,
        (start_dt.isoformat(), end_dt.isoformat()),
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def last_expenses(limit: int = 10):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, user_id, username, amount, category, created_at
        FROM expenses
        ORDER BY datetime(created_at) DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def day_bounds(target: datetime):
    start = datetime.combine(target.date(), time.min, tzinfo=TZ)
    end = start + timedelta(days=1)
    return start, end

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_allowed(user.id):
        await update.message.reply_text("У тебя нет доступа к этому боту.")
        return

    text = (
        "Бот учета расходов готов.\n\n"
        "Добавить расход: /add\n\n"
        "Команды:\n"
        "/today — расходы за сегодня\n"
        "/week — расходы за 7 дней\n"
        "/month — расходы за месяц\n"
        "/last — последние записи\n"
        "/help — помощь"
    )
    await update.message.reply_text(text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_allowed(user.id):
        await update.message.reply_text("У тебя нет доступа.")
        return ConversationHandler.END

    await update.message.reply_text(
        "Выбери категорию:",
        reply_markup=category_keyboard
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
        f"Категория: {category}\nТеперь введи сумму, например: 350",
        reply_markup=ReplyKeyboardRemove()
    )
    return AMOUNT_INPUT

async def add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message or not is_allowed(user.id):
        return ConversationHandler.END

    text = update.message.text.strip().replace(",", ".").replace("₽", "")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите сумму числом, например: 350")
        return AMOUNT_INPUT

    category = context.user_data.get("new_expense_category")
    if not category:
        await update.message.reply_text("Категория потерялась, начни заново: /add")
        return ConversationHandler.END

    username = user.username or user.first_name or str(user.id)
    add_expense(user.id, username, amount, category)

    context.user_data.pop("new_expense_category", None)

    await update.message.reply_text(
        f"Записал: {format_money(amount)} — {category}"
    )
    return ConversationHandler.END

async def cancel_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_expense_category", None)
    await update.message.reply_text(
        "Добавление отменено.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_allowed(user.id):
        await update.message.reply_text("У тебя нет доступа.")
        return

    start, end = day_bounds(now_msk())
    total = sum_between(start, end)
    by_cat = category_stats_between(start, end)

    lines = [f"Сегодня: {format_money(total)}"]
    if by_cat:
        lines.append("")
        for row in by_cat:
            lines.append(f"{row['category']}: {format_money(float(row['total']))}")

    await update.message.reply_text("\n".join(lines))

async def week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_allowed(user.id):
        await update.message.reply_text("У тебя нет доступа.")
        return

    end = now_msk()
    start = end - timedelta(days=7)
    total = sum_between(start, end)
    by_cat = category_stats_between(start, end)
    by_user = user_stats_between(start, end)

    lines = [f"За 7 дней: {format_money(total)}", ""]
    lines.append("По категориям:")
    for row in by_cat:
        lines.append(f"{row['category']}: {format_money(float(row['total']))}")

    if by_user:
        lines.append("")
        lines.append("По людям:")
        for row in by_user:
            lines.append(f"{row['name']}: {format_money(float(row['total']))}")

    await update.message.reply_text("\n".join(lines))

async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_allowed(user.id):
        await update.message.reply_text("У тебя нет доступа.")
        return

    now = now_msk()
    start = datetime(now.year, now.month, 1, tzinfo=TZ)
    if now.month == 12:
        end = datetime(now.year + 1, 1, 1, tzinfo=TZ)
    else:
        end = datetime(now.year, now.month + 1, 1, tzinfo=TZ)

    total = sum_between(start, end)
    by_cat = category_stats_between(start, end)
    by_user = user_stats_between(start, end)

    lines = [f"За месяц: {format_money(total)}", ""]
    lines.append("По категориям:")
    for row in by_cat:
        lines.append(f"{row['category']}: {format_money(float(row['total']))}")

    if by_user:
        lines.append("")
        lines.append("По людям:")
        for row in by_user:
            lines.append(f"{row['name']}: {format_money(float(row['total']))}")

    await update.message.reply_text("\n".join(lines))

async def last_records(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_allowed(user.id):
        await update.message.reply_text("У тебя нет доступа.")
        return

    rows = last_expenses(10)
    if not rows:
        await update.message.reply_text("Пока расходов нет.")
        return

    lines = ["Последние 10 записей:"]
    for row in rows:
        dt = datetime.fromisoformat(row["created_at"]).astimezone(TZ)
        name = row["username"] or str(row["user_id"])
        lines.append(
            f"#{row['id']} | {dt.strftime('%d.%m %H:%M')} | {name} | {row['category']} | {format_money(float(row['amount']))}"
        )

    await update.message.reply_text("\n".join(lines))

async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_allowed(user.id):
        await update.message.reply_text("У тебя нет доступа.")
        return

    await update.message.reply_text(
        "Чтобы добавить расход, используй /add"
    )

async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    now = now_msk()
    day_to_report = now - timedelta(days=1)
    start, end = day_bounds(day_to_report)

    total = sum_between(start, end)
    by_cat = category_stats_between(start, end)
    by_user = user_stats_between(start, end)

    lines = [f"Итог за {day_to_report.strftime('%d.%m.%Y')}: {format_money(total)}"]

    if by_cat:
        lines.append("")
        lines.append("По категориям:")
        for row in by_cat:
            lines.append(f"{row['category']}: {format_money(float(row['total']))}")

    if by_user:
        lines.append("")
        lines.append("По людям:")
        for row in by_user:
            lines.append(f"{row['name']}: {format_money(float(row['total']))}")

    await context.bot.send_message(chat_id=CHAT_ID, text="\n".join(lines))

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            CATEGORY_CHOICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_category)
            ],
            AMOUNT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_add)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("week", week))
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("last", last_records))
    app.add_handler(add_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))

    app.job_queue.run_daily(
        send_daily_report,
        time=time(hour=0, minute=0, tzinfo=TZ),
        name="daily-report",
    )

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
