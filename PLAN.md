# План разработки: Rillza VPN Telegram Bot

Новый бот для продажи VPN-подписок «Rillza VPN». Наследует продуктовую идею
старого бота (`src/`), но пишется с нуля: собственная БД как источник правды,
панель **CELERITY** (уже развёрнута, ботом не реализуется) как исполнитель,
клиентское приложение — **Happ** (роутинг-профили предопределены в панели и
доставляются подписочной ссылкой автоматически). Анти-шеринг функционала в
боте нет и не будет.

---

## 1. Стек (полностью асинхронный)

| Слой | Технология |
|---|---|
| Телеграм | aiogram 3 (long polling, HTML parse mode) |
| БД | PostgreSQL 17, SQLAlchemy 2.x **async** + драйвер **asyncpg** (`postgresql+asyncpg://`), Alembic |
| FSM / кэш | Redis (`redis.asyncio`, RedisStorage + orjson) |
| HTTP к панели и платёжкам | aiohttp (общая сессия) |
| Планировщик | APScheduler (AsyncIOScheduler) |
| Конфиг | pydantic-settings (.env) |
| Логи | loguru → stderr + Telegram-чат (второй бот, корректная разбивка по 4096) |
| Пакеты / линт | uv, ruff; Python 3.13 |
| Тесты | pytest + pytest-asyncio; реальный Postgres в CI (service container), **не** SQLite — иначе `FOR UPDATE` не тестируется |
| CI | GitHub Actions: ruff check + format, pytest, `alembic upgrade head` smoke |
| Деплой | Docker Compose: bot + postgres + redis (healthchecks, named volumes) |

Правило: никакого блокирующего I/O в хендлерах и джобах. Библиотека
`aiomoney` не используется (заброшена) — своя тонкая aiohttp-обёртка над API
ЮMoney; CryptoBot — тоже своя обёртка.

## 2. Архитектура

Слои строго сверху вниз, роутеры не трогают SQLAlchemy и aiohttp напрямую:

```
bot/routers  →  services  →  repositories  →  db
                    ↓
             integrations (celerity, payments)
```

- **PostgreSQL — единственный источник правды**: пользователи, подписки,
  платежи, тарифы, рассылки. Панель — «исполнитель», к которому мы приводим
  желаемое состояние (converge), и её можно пересоздать из БД.
- **Деньги — целые копейки** (`amount_kopeks INT`), никаких float/NUMERIC в
  бизнес-логике.
- **Enum'ы — TEXT + CHECK** (не нативные PG enum): добавление провайдера №3 —
  однострочная миграция.

## 3. Продуктовая модель

- **Одна подписка на пользователя Telegram.** `userId` в CELERITY = Telegram ID
  (панель сама это рекомендует). Несколько устройств — это `maxDevices`/HWID
  на стороне панели (наследуется от серверной группы), бот их не полисит.
  Это отличие от старого бота («1 подписка = 1 устройство», много именованных
  подписок) — упрощает всё: UX, схему, реконсиляцию. ⚠️ Решение подтвердить.
- **Тарифы по срокам**: 1 / 3 / 6 / 12 месяцев со скидкой за длинные периоды.
  Хранятся в БД, редактируются из админки без деплоя.
- **Пробный период**: `TRIAL_DAYS` (env) один раз на пользователя навсегда
  (латч `users.trial_used_at`). Покупка во время триала продлевает ту же
  подписку: `new_expires = max(now, current_expires) + duration`.
- Любой оплаченный платёж = «создать-или-продлить подписку пользователя» —
  отдельной сущности «продление» нет.

## 4. Схема БД

Все таблицы: `created_at timestamptz default now()`, изменяемые — `updated_at`.

```
users
  id BIGINT PK                  -- = telegram_id, без суррогатов
  username TEXT NULL, first_name TEXT NULL
  is_bot_blocked BOOL DEFAULT false      -- ставится при Forbidden в рассылке
  trial_used_at timestamptz NULL         -- латч «триал выдан»

tariffs
  id SERIAL PK, code TEXT UNIQUE         -- 'm1','m3','m6','m12'
  title_ru TEXT, duration_days INT
  price_kopeks INT                       -- одна цена в рублях; крипта - fiat=RUB
  sort_order INT, is_active BOOL, is_archived BOOL   -- никогда не удаляются

subscriptions                            -- 1:1 с users
  id UUID PK
  user_id BIGINT UNIQUE FK->users
  status TEXT CHECK IN ('pending','active','expired','revoked')
  origin TEXT CHECK IN ('trial','purchase','admin_grant')
  expires_at timestamptz NOT NULL
  panel_user_id TEXT NOT NULL            -- = str(telegram_id)
  subscription_token TEXT NULL           -- кэш из ответа панели, для ссылки
  provisioned_at timestamptz NULL, last_synced_at timestamptz NULL
  notified_stage TEXT NULL               -- '3d' | '1d' — антидубль напоминаний
  INDEX (status, expires_at)

payments                                 -- неизменяемая денежная запись
  id UUID PK                             -- = label (ЮMoney) / payload (CryptoBot)
  user_id FK->users, tariff_id FK->tariffs
  provider TEXT CHECK IN ('yoomoney','cryptobot')
  status TEXT CHECK IN ('pending','paid','provisioned','expired','canceled')
  amount_kopeks INT
  paid_amount_kopeks INT NULL, paid_currency TEXT NULL   -- снапшот факта
  provider_invoice_id TEXT NULL, UNIQUE(provider, provider_invoice_id)
  target_expires_at timestamptz NULL     -- вычислен ОДИН раз при mark_paid
  invoice_url TEXT, invoice_expires_at timestamptz       -- TTL счёта 30 мин
  paid_at, provisioned_at timestamptz NULL
  INDEX (status), INDEX (user_id, created_at)

broadcasts                               -- резюмируемые рассылки
  id SERIAL PK, content_chat_id BIGINT, content_message_id BIGINT
  status TEXT CHECK IN ('draft','running','done','canceled')
  last_user_id BIGINT NULL               -- курсор, чекпоинт каждые 25 отправок
  sent INT, failed INT, blocked INT

job_heartbeats                           -- «мёртвая рука» фоновых задач
  job_name TEXT PK, last_success_at, last_error TEXT NULL, last_error_at
```

## 5. Платёжный конвейер (главный инвариант корректности)

Статусы платежа: `pending → paid → provisioned` (+ `expired`, `canceled`).
Состояние «деньги взяты, доступ не выдан» (`paid`) — **явный запрашиваемый
статус в БД**, он же — очередь на провижининг. Отдельной outbox-таблицы нет.

- **`check_and_finalize(payment_id)` — единственная воронка** и для кнопки
  «Проверить оплату», и для поллера. Берёт строку `SELECT ... FOR UPDATE SKIP
  LOCKED`: параллельный клик мгновенно получает «проверяем…», а не блокируется
  за HTTP-вызовом.
- Внутри транзакции `mark_paid`: статус `pending→paid` (CAS), вычисляется и
  фиксируется **абсолютный** `target_expires_at = max(now, sub.expires_at) +
  duration`. Ретраи провижининга никогда не пересчитывают срок — идемпотентно
  при любом падении.
- Провижининг: create-or-update пользователя панели с `expireAt =
  target_expires_at` (абсолютная дата — ретрай не «доливает» дни), затем
  `paid→provisioned` + отправка ссылки. Упали между — watcher-джоба дожмёт.
- **Верификация ЮMoney**: платёж подтверждаем по `label` + статусу операции
  (operation history), фактические суммы (с учётом комиссии) пишем в
  `paid_amount_kopeks` информативно, НЕ сравниваем «>= цены» (комиссия ломает
  такое сравнение). **CryptoBot**: счёт `currency_type=fiat, fiat=RUB` —
  конверсию делает CryptoBot, у нас одна цена и вся выручка в рублях.
- TTL счёта 30 минут → `expired`. **Досweep опоздавших денег**: ежедневная
  джоба перепроверяет вчерашние `expired`-счета один раз; найденные деньги —
  алерт админу + кнопка ручного провижининга. Ручной refund-процесс — в README.

## 6. Интеграция с CELERITY

Аутентификация: API-ключ `ck_…` в заголовке `X-API-Key`, скоупы
`users:read + users:write + stats:read`. Лимит по умолчанию 60 req/min →
клиент обязан уважать 429/`X-RateLimit-Remaining`, ретраи с backoff (tenacity),
типизированные ошибки (`PanelUnavailable`, `PanelUserNotFound`…).

Используемые эндпоинты (все `{"error": "..."}`-конвенция):

| Действие бота | Вызов |
|---|---|
| Выдать/создать | `POST /api/users` `{userId=tg_id, username, groups:[GROUP_ID], enabled:true, expireAt, trafficLimit:0, maxDevices:0}`; **409 возвращает существующего** → create-or-fetch |
| Продлить | `PUT /api/users/{tg_id}` `{expireAt: <абсолютная ISO>}` — панель сама реактивирует отключённого (`recomputeEnabled`) |
| Отозвать (админ) | `POST /api/users/{tg_id}/disable` |
| Прочитать | `GET /api/users/{tg_id}` (реконсиляция, поиск в админке) |
| Список | `GET /api/users?page=&limit=` (реконсиляция) |
| Группы | `GET /api/groups` — один раз на старте резолвим `PANEL_GROUP_ID` по имени |
| Здоровье | `GET /health` (публичный) — в админ-статистику |
| Ссылка юзеру | `https://{PANEL}/api/files/{subscription_token}` — публичная; Happ получает роутинг-профили заголовками автоматически |
| Статус для «Моей подписки» | `GET /api/info/{token}` — публичный JSON, не тратит лимит ключа |

Что бот **не** делает: не управляет нодами/группами/роутингом, не полисит
устройства (HWID — панельный), не держит webhook-приёмник (вебхуки панели
fire-and-forget без ретраев, бот на long polling без портов — для денег
только поллинг + реконсиляция).

Панель **сама** отключает истёкших (expireScheduler) — боту не нужен свой
«выключатель», его expirer только переводит статус в БД и шлёт напоминания.

## 7. Фоновые задачи (APScheduler, все `max_instances=1, coalesce=True`)

| Задача | Интервал | Что делает |
|---|---|---|
| payment_poller | 30 c | `check_and_finalize` для всех `pending` с неистёкшим TTL |
| provisioning_watcher | 60 c | дожимает `paid` → панель → `provisioned` |
| invoice_expirer | 5 мин | `pending` с истёкшим TTL → `expired`, уведомление |
| expiry_sync | 10 мин | `active` с `expires_at < now` → `expired` в БД |
| expiry_notifier | 1 ч | напоминания за 3 дня / 1 день (`notified_stage` — антидубль) |
| late_payment_sweep | ежедневно | перепроверка вчерашних `expired`-счетов |
| reconciler | 4 ч | diff БД ↔ `GET /api/users`: чинит `expireAt`/enabled в панели по БД; **сирот в панели только репортит, не трогает** |
| pg_backup напоминание | — | бэкапы вне бота: pg_dump cron + off-box копия (runbook в README) |

Каждая джоба пишет `job_heartbeats`; админ-экран показывает «последний
успех/ошибка» — молчаливо умершая джоба видна сразу.

## 8. UX бота (все тексты RU, HTML)

Пользователь:
- `/start` → главное меню: «🚀 Попробовать бесплатно» (если триал не
  использован) / «🛒 Купить подписку» / «🌐 Моя подписка» / «❓ Помощь».
- Покупка: выбор тарифа (инлайн-сетка из БД) → счёт (ЮMoney | CryptoBot) →
  «Оплатить» / «Проверить оплату» / «Отменить».
- «Моя подписка»: статус и срок из БД (+ живой трафик из `/api/info/{token}`),
  кнопка-CopyText со ссылкой, кнопка «Продлить», инструкции Happ по платформам
  (Android / iOS / macOS / Windows).
- FSM-минимум: имён подписок больше нет — состояний почти не остаётся
  (только админские сценарии). Ошибочные/устаревшие callback'и гасятся
  error-middleware'ом с мягким «Меню устарело, нажмите /start».

Админ (`ADMIN_IDS`, фильтр по id):
- 📊 Статистика: выручка (день/неделя/месяц из `payments`), активные/триал
  подписки, конверсия триала, heartbeats джоб, `/health` панели.
- 👤 Пользователь по tg id/username: подписка, платежи, выдать/продлить/отозвать
  N дней, кнопка «Повторить провижининг» для зависших `paid`.
- 🧾 Тарифы: список/цена/активность прямо из бота.
- 📣 Рассылка: превью → подтверждение → резюмируемая отправка с курсором,
  обработкой RetryAfter и учётом заблокировавших.

## 9. Структура проекта

```
app/
├── __main__.py, main.py            # wiring, DI-контейнер, startup/shutdown
├── core/    settings.py, logging.py, enums.py
├── db/      engine.py, models/ (user, tariff, subscription, payment, broadcast, heartbeat)
├── repositories/  users, tariffs, subscriptions, payments, broadcasts, stats
├── services/ uow.py, payment_service, provisioning_service, subscription_service,
│             trial_service, tariff_service, stats_service, broadcast_service,
│             reconcile_service
├── integrations/
│   ├── celerity/ client.py, schemas.py, errors.py
│   └── payments/ base.py (Protocol + закрытый enum статусов), yoomoney.py,
│                 cryptobot.py, registry.py
├── bot/     routers/{user,admin}/, keyboards/ (CallbackData-фабрики),
│            middlewares/ (db-session, user-upsert, errors), filters.py, texts/ru.py
├── scheduler/jobs.py
tests/       unit/ (fake panel, fake providers), integration/ (Postgres),
             contract/ (aioresponses для ЮMoney/CryptoBot/CELERITY)
alembic/, compose.yaml, Dockerfile, .github/workflows/ci.yml, .env.example, README.md
```

Старый код `src/` живёт до финальной фазы, затем удаляется.

## 10. Этапы (каждый — отдельно проверяемый)

1. **Каркас**: uv, ruff, CI, compose (bot+pg+redis), settings, loguru с
   починенной разбивкой, пустой aiogram-бот с /start. ✅ CI зелёный.
2. **БД**: модели, Alembic, репозитории, UoW; интеграционные тесты на Postgres.
3. **Клиент CELERITY**: create-or-fetch (409), extend (PUT, абсолютный
   expireAt), disable, info; ретраи/лимиты; contract-тесты на моках.
4. **Триал + «Моя подписка»**: выдача триала end-to-end (панель → ссылка →
   Happ), экран подписки. Бот уже полезен без денег.
5. **Платежи**: обёртки ЮMoney и CryptoBot (fiat=RUB), `check_and_finalize`,
   поллер/watcher/expirer, тесты гонок (двойной клик, рестарт между paid и
   provisioned, поздний платёж).
6. **Админка + рассылки + напоминания об истечении.**
7. **Реконсилятор, heartbeats, README** (установка, создание API-ключа в
   панели, runbook: бэкапы pg_dump, ручной refund, «панель недоступна»),
   `.env.example`; удаление `src/`; перезалив видео-гайдов файлами (file_id
   старого бота не переносятся — они привязаны к токену).

## 11. Env (основное)

```
BOT_TOKEN=            ADMIN_IDS=[...]         TRIAL_DAYS=3
DATABASE_URL=postgresql+asyncpg://...          REDIS_URL=redis://...
PANEL_BASE_URL=https://panel.example.com
PANEL_API_KEY=ck_...  PANEL_GROUP_NAME=Rillza  # резолвится в _id на старте
YOOMONEY_ACCESS_TOKEN=  YOOMONEY_WALLET=
CRYPTOBOT_TOKEN=
INVOICE_TTL_MINUTES=30
LOG_BOT_TOKEN=  LOG_CHAT_ID=
```

## 12. Решения, требующие подтверждения владельца

1. **Одна подписка на пользователя** (вместо нескольких именованных, как в
   старом боте). Устройства — HWID-лимит серверной группы панели.
2. Цены тарифной сетки m1/m3/m6/m12 (в плане — плейсхолдеры).
3. `TRIAL_DAYS` и `maxDevices` по умолчанию (план: наследовать от группы).
4. Нужен ли импорт клиентов старого бота (3x-ui) — план исходит из «нет,
   старт с чистого листа».
5. Новые видео-гайды под Happ (старые ролики под v2rayNG/Throne устарели).
