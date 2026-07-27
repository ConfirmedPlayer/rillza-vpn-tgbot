# Rillza VPN — Telegram bot

Телеграм-бот для продажи VPN-подписок. База данных — источник правды,
панель [CELERITY](https://github.com/ClickDevTech/CELERITY-panel) —
исполнитель, клиентское приложение — [Happ](https://happ.su).

План разработки и все принятые решения: [PLAN.md](PLAN.md).

> Проект в разработке. Готовы этап 1 (каркас: конфигурация, логирование,
> `/start`, Docker Compose, CI) и этап 2 (база данных: модели, миграции,
> репозитории, unit of work). Дальше — по порядку из PLAN.md §14.

## Стек

Python 3.13 · aiogram 3 · PostgreSQL + SQLAlchemy 2 (asyncpg) · Redis ·
APScheduler · loguru · uv · Docker Compose

## Быстрый старт

```bash
cp .env.example .env
# заполнить BOT_TOKEN, POSTGRES_PASSWORD, DATABASE_URL, PANEL_API_KEY
docker compose up -d --build
docker compose logs -f bot
```

## Локальная разработка

```bash
uv sync                    # поставить зависимости
uv run ruff check .        # линт
uv run ruff format .       # форматирование
uv run pytest              # тесты (интеграционные пропускаются без БД)
uv run alembic upgrade head  # применить миграции
uv run python -m app       # запуск бота (нужны Postgres и Redis)
```

Интеграционные тесты требуют настоящий PostgreSQL — SQLite игнорирует
`FOR UPDATE`, и проверки блокировки платежей проходили бы впустую:

```bash
createdb rillza_test
TEST_DATABASE_URL=postgresql+asyncpg://postgres@localhost:5432/rillza_test \
  uv run pytest
```

Миграции применяются автоматически при старте контейнера
(`docker-entrypoint.sh`). Новые создаются так:

```bash
uv run alembic revision --autogenerate -m "что изменилось"
uv run alembic check   # проверить, что модели и миграции сошлись
```

## Проверка панели

Проверяет, что API-ключ работает и `PANEL_GROUP_NAME` совпадает с реальной
серверной группой. Делает только GET-запросы, ничего не меняет:

```bash
uv run python -m scripts.check_panel
docker compose exec bot python -m scripts.check_panel   # в контейнере
```

## Настройки

Все переменные окружения перечислены в [.env.example](.env.example).
Обязательные: `BOT_TOKEN`, `DATABASE_URL`, `PANEL_BASE_URL`,
`PANEL_API_KEY`. Логирование в Telegram-чат включается опционально
парой `LOG_BOT_TOKEN` + `LOG_CHAT_ID`.

## Разовая настройка панели CELERITY

Панель разворачивается отдельно; боту нужно от неё следующее:

1. **API-ключ** — Settings → Security → API Keys, скоупы `users:read`,
   `users:write`, `stats:read`. Значение в `PANEL_API_KEY`.
2. **Серверная группа** (по умолчанию `Rillza`) с `maxDevices = 2` —
   новые пользователи привязываются к ней, имя в `PANEL_GROUP_NAME`.
3. **Кнопка Happ** — Settings → Subscription → Buttons → кнопка `HAPP`
   с URL `happ://add/{url}`. Через неё подписка импортируется в
   приложение в одно нажатие.
4. **HWID** — Settings → Subscription → Happ → hwid mode `permissive`.
5. **Soft-block** (рекомендуется) — истёкшая подписка показывает
   понятный текст вместо ошибки.

## Структура

```
app/
├── core/           настройки, логирование, перечисления
├── db/             модели и движок SQLAlchemy
├── repositories/   запросы к БД
├── services/       unit of work
├── bot/            роутеры, тексты, middlewares
└── main.py         сборка и запуск
alembic/            миграции
tests/              unit + integration (нужен Postgres)
src/                старый бот (удаляется на финальном этапе)
```

## Лицензия

[GPL-3.0](LICENSE)
