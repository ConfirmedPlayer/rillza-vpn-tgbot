# Rillza VPN — Telegram bot

Телеграм-бот для продажи VPN-подписок. База данных — источник правды,
панель [CELERITY](https://github.com/ClickDevTech/CELERITY-panel) —
исполнитель, клиентское приложение — [Happ](https://happ.su).

План разработки и все принятые решения: [PLAN.md](PLAN.md).

> Проект в разработке. Готов этап 1 (каркас): конфигурация, логирование,
> `/start`, Docker Compose, CI. Работа над остальными этапами идёт в
> порядке, описанном в PLAN.md §13.

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
uv sync                 # поставить зависимости
uv run ruff check .     # линт
uv run ruff format .    # форматирование
uv run pytest           # тесты
uv run python -m app    # запуск бота (нужен Redis из compose)
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
├── core/      настройки, логирование
├── bot/       роутеры, тексты
└── main.py    сборка и запуск
tests/         pytest
src/           старый бот (удаляется на финальном этапе)
```

## Лицензия

[GPL-3.0](LICENSE)
