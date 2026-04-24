import logging
import os
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import psycopg
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
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not ALLOWED_USER_IDS_RAW:
    raise RuntimeError("ALLOWED_USER_IDS is not set")
if not CHAT_ID:
    raise RuntimeError("CHAT_ID is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

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
    return psycopg.connect(DATABASE_URL)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS expenses (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    amount NUMERIC(12, 2) NOT NULL,
                    category TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        conn.commit()

def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS

def now_msk() -> datetime:
    return datetime.now(TZ)

def format_money(value: float) -> str:
    return f"{value:.0f} ₽" if value == int(value) else f"{value:.2f} ₽"

def add_expense(user_id: int, username: str, amount: float, category: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO expenses (user_id, username, amount, category, created_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, username, amount, category, now_msk()),
            )
        conn.commit()

def sum_between(start_dt: datetime, end_dt: datetime):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(amount), 0)
                FROM expenses
                WHERE created_at >= %s AND created_at < %s
                """,
                (start_dt, end_dt),
            )
            row = cur.fetchone()
            return float(row[0] or 0)

def category_stats_between(start_dt: datetime, end_dt: datetime):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT category, COALESCE(SUM(amount), 0) AS total
                FROM expenses
                WHERE created_at >= %s AND created_at < %s
                GROUP BY category
                ORDER BY total DESC
                """,
                (start_dt, end_dt),
            )
            rows = cur.fetchall()
            return rows

def user_stats_between(start_dt: datetime, end_dt: datetime):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(username, user_id::text) AS name, COALESCE(SUM(amount), 0) AS total
                FROM expenses
                WHERE created_at >= %s AND created_at < %s
                GROUP BY user_id, username
                ORDER BY total DESC
                """,
                (start_dt, end_dt),
            )
            rows = cur.fetchall()
            return rows

def last_expenses(limit: int = 10):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, username, amount, category, created_at
                FROM expenses
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
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
        amount 
