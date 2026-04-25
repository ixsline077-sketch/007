# Telegram Expense Bot

Простой Telegram-бот для учета расходов с хранением данных в PostgreSQL.

## Файлы проекта
- `bot.py`
- `requirements.txt`
- `.gitignore`
- `README.md`

## Переменные Railway
- `BOT_TOKEN`
- `DATABASE_URL`
- `ALLOWED_USER_IDS`
- `TZ` (необязательно)

## Запуск локально
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN="..."
export DATABASE_URL="..."
export ALLOWED_USER_IDS="123456789"
python bot.py
```

## Загрузка в GitHub
Положи эти файлы в репозиторий или в папку `telegram-expense-bot/` внутри существующего репозитория.

## Важно
Не загружай реальные токены, пароли и `.env` в GitHub.
