# План разработки: Rillza VPN Telegram Bot

Новый бот для продажи VPN-подписок «Rillza VPN». Наследует продуктовую идею
старого бота (`src/`), но пишется с нуля: собственная БД как источник правды,
панель **CELERITY** (уже развёрнута на `https://link.rillza-service.com`,
ботом не реализуется) как исполнитель, клиентское приложение — **Happ**
(роутинг-профили предопределены в панели и доставляются подписочной ссылкой
автоматически). Анти-шеринг функционала в боте нет.

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

Никакого блокирующего I/O в хендлерах и джобах.

## 2. Архитектура

Слои строго сверху вниз, роутеры не трогают SQLAlchemy и aiohttp напрямую:

```
bot/routers  →  services  →  repositories  →  db
                    ↓
             integrations (celerity, payments)
```

- **PostgreSQL — единственный источник правды**: пользователи, подписки,
  платежи, тарифы, рассылки. Панель — «исполнитель», к которому мы приводим
  желаемое состояние, и её содержимое можно пересоздать из БД.
- **Деньги — целые копейки** (`amount_kopeks INT`), никаких float в логике.
- **Enum'ы — TEXT + CHECK** (не нативные PG enum): добавить провайдера №3 —
  однострочная миграция.

## 3. Продуктовая модель

- **Уникальный ключ везде — Telegram ID.** `users.id = telegram_id` (без
  суррогатов), `userId` в CELERITY = `str(telegram_id)` — панель сама
  рекомендует такой ключ («Unique user ID (e.g. Telegram ID)» в её OpenAPI).
  Один Telegram-аккаунт = одна запись в панели = одна подписка.
- **Одна подписка на пользователя.** Несколько устройств — это `maxDevices`
  и HWID на стороне панели (см. §7), бот их не полисит.
- **Тарифы по срокам**: 1 / 3 / 6 / 12 месяцев со скидкой за длинные периоды.
  Хранятся в БД, цены и активность редактируются из админки без деплоя;
  сид-миграция кладёт стартовые значения (отправная точка — 200 ₽/мес
  старого бота), финальные цифры проставляются в админке при запуске.
- **Пробный период — 3 дня** (`TRIAL_DAYS=3`), один раз на пользователя
  навсегда (латч `users.trial_used_at`). Покупка во время триала продлевает
  ту же подписку: `new_expires = max(now, current_expires) + duration`.
- Любой оплаченный платёж = «создать-или-продлить подписку» — отдельной
  сущности «продление» нет.

## 4. Схема БД

Все таблицы: `created_at timestamptz default now()`, изменяемые — `updated_at`.

```
users
  id BIGINT PK                  -- = telegram_id
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

## 5. Платёжный конвейер

Статусы: `pending → paid → provisioned` (+ `expired`, `canceled`). Состояние
«деньги взяты, доступ не выдан» (`paid`) — явный статус в БД, он же очередь на
провижининг. Отдельной outbox-таблицы нет.

- **`check_and_finalize(payment_id)` — единственная воронка** и для кнопки
  «Проверить оплату», и для поллера. Берёт строку `SELECT ... FOR UPDATE SKIP
  LOCKED`: параллельный клик получает «проверяем…», а не виснет за HTTP.
- `mark_paid` в транзакции: `pending→paid` (CAS) + фиксация **абсолютного**
  `target_expires_at = max(now, sub.expires_at) + duration`. Ретраи никогда не
  пересчитывают срок — идемпотентно при любом падении.
- Провижининг: create-or-update пользователя панели с `expireAt =
  target_expires_at`, затем `paid→provisioned` + отправка ссылки. Упали
  между — watcher-джоба дожмёт.
- TTL счёта 30 минут → `expired`. **Досweep опоздавших денег**: ежедневная
  джоба перепроверяет вчерашние `expired`-счета; найденные деньги — алерт
  админу + кнопка ручного провижининга.

### 5.1. ЮMoney — свой клиент на том же API (p2p-кошелёк)

Схема оплаты остаётся прежней: перевод на личный кошелёк ЮMoney (без ИП,
самозанятости и эквайринговой комиссии). `aiomoney` не подключаем как
зависимость (последний релиз 12.2024, 4 вызова, ~200 строк) — пишем свою
обёртку на **тех же эндпоинтах**:

| Что | Вызов |
|---|---|
| Номер кошелька | `POST https://yoomoney.ru/api/account-info` (Bearer) — кэшируем на старте / берём из `YOOMONEY_WALLET` |
| Счёт | `GET https://yoomoney.ru/quickpay/confirm.xml` c `receiver`, `quickpay-form=button`, `paymentType=AC|PC`, `sum`, `label=<payment.id>`, `successURL` → итоговый URL редиректа = ссылка на оплату |
| Проверка | `POST https://yoomoney.ru/api/operation-history` (Bearer) с параметром **`label=<payment.id>`** |
| Сумма/детали | `POST https://yoomoney.ru/api/operation-details` при необходимости |

Три вещи, которые чиним относительно aiomoney (реальные баги, а не стиль):

1. **Фильтр по `label` уходит на сервер.** aiomoney запрашивает историю без
   параметров и фильтрует ответ в Python, а API отдаёт только первую страницу
   (по умолчанию 30 операций) — при потоке платежей оплата «уезжает» со
   страницы и **не находится вообще**. Передаём `label` в запрос.
2. **Проверяем `direction == "in"`**, а не только `status == "success"` —
   иначе исходящая операция с тем же label может быть засчитана как оплата.
3. **Сумма — с копейками** (`sum` с двумя знаками), aiomoney принимает
   только целые рубли.

Правило подтверждения: платёж засчитан по `label` + `direction=in` +
`status=success`. Фактическую сумму пишем в `paid_amount_kopeks`
информативно и **не** сравниваем «>= цены»: при p2p-переводах с карты
комиссия удерживается с отправителя, и такое сравнение отвергало бы
легитимные платежи. Расхождения — в лог и админ-алерт.

**CryptoBot**: `createInvoice` c `currency_type=fiat, fiat=RUB` — конверсию
делает CryptoBot, у нас одна цена и вся выручка в рублях, никакого FX-кода.

## 6. Интеграция с CELERITY

Панель: `https://link.rillza-service.com` (админка — `/panel`, API — `/api`).
Аутентификация: API-ключ `ck_…` в заголовке `X-API-Key`, скоупы
`users:read + users:write + stats:read`. Лимит по умолчанию 60 req/min →
клиент уважает 429 и `X-RateLimit-Remaining`, ретраи с backoff,
типизированные ошибки.

| Действие бота | Вызов |
|---|---|
| Выдать/создать | `POST /api/users` `{userId=tg_id, username, groups:[GROUP_ID], enabled:true, expireAt, trafficLimit:0, maxDevices:0}`; **409 возвращает существующего** → готовый create-or-fetch |
| Продлить | `PUT /api/users/{tg_id}` `{expireAt: <абсолютная ISO>}` — панель сама реактивирует отключённого (`recomputeEnabled`) |
| Отозвать (админ) | `POST /api/users/{tg_id}/disable` |
| Прочитать | `GET /api/users/{tg_id}` (реконсиляция, поиск в админке) |
| Список | `GET /api/users?page=&limit=` (реконсиляция) |
| Группы | `GET /api/groups` — один раз на старте резолвим `PANEL_GROUP_ID` по имени |
| Здоровье | `GET /health` (публичный) — в админ-статистику |
| Ссылка юзеру | `https://link.rillza-service.com/api/files/{subscription_token}` |
| Статус подписки | `GET /api/info/{token}` — публичный JSON, не тратит лимит ключа |

Панель **сама** отключает истёкших (`expireScheduler`) — своего «выключателя»
боту не нужно; его `expiry_sync` только переводит статус в БД.

Бот не управляет нодами, группами и роутингом, не держит webhook-приёмник
(вебхуки панели fire-and-forget без ретраев; бот на long polling без открытых
портов — для денег только поллинг + реконсиляция).

## 7. HWID / лимит устройств — работает без платного Happ Provider ID

Проверено по коду панели (`src/routes/subscription.js:2668-2744`,
`src/utils/hwidHeaders.js`, `src/services/hwidDeviceService.js`):

- Гейт HWID зависит **только** от трёх вещей: режима
  `settings.subscription.happ.hwid.mode` (`off|permissive|strict`, плюс
  переопределение `hwidMode` на пользователе), эффективного лимита
  (`user.maxDevices`, при `0` — минимум из его групп, `-1` = безлимит) и
  заголовка `x-hwid`, который **Happ шлёт сам** (в коде: «HAPP almost never
  hits this branch (it always sends x-hwid)»).
- `happProviderId` (платный) гейтит **только косметику**: заголовки
  `providerid`, `hide-settings`, `notification-subs-expire`,
  `subscription-always-hwid-enable`, `ping-type`/`check-url-via-proxy`,
  `color-profile`.
- **Не** требуют Provider ID: сам лимит устройств, попап `announce` при
  превышении лимита (ставится внутри HWID-гейта), заголовок `routing` с
  роутинг-профилями, `Subscription-Userinfo`, `Profile-Title`.
- Единственная потеря без Provider ID — флаг `subscription-always-hwid-enable`
  («принудительно всегда слать HWID»); поскольку Happ шлёт `x-hwid` по
  умолчанию, на практике это не мешает. Режим `strict` дополнительно
  отсекает клиенты без `x-hwid` (другие приложения), `permissive` —
  ограничивает только тех, кто HWID шлёт.
- Ограничение честное: гейт срабатывает при **скачивании подписки**, а не в
  момент коннекта. Рантайм-лимит по IP — это `maxDevices` на стороне
  Hysteria-авторизации панели. И то и другое настраивается в панели, бот
  ничего не полисит.

Вывод: включаем HWID-лимит в панели (режим и `maxDevices` на серверной
группе) — Provider ID покупать не нужно.

## 8. Фоновые задачи (APScheduler, все `max_instances=1, coalesce=True`)

| Задача | Интервал | Что делает |
|---|---|---|
| payment_poller | 30 c | `check_and_finalize` для всех `pending` с неистёкшим TTL |
| provisioning_watcher | 60 c | дожимает `paid` → панель → `provisioned` |
| invoice_expirer | 5 мин | `pending` с истёкшим TTL → `expired`, уведомление |
| expiry_sync | 10 мин | `active` с `expires_at < now` → `expired` в БД |
| **expiry_notifier** | 1 ч | **напоминания за 3 дня и за 1 день** до конца подписки (`notified_stage` — антидубль), с кнопкой «Продлить» |
| late_payment_sweep | ежедневно | перепроверка вчерашних `expired`-счетов |
| reconciler | 4 ч | diff БД ↔ `GET /api/users`: чинит `expireAt`/`enabled` в панели по БД; **сирот в панели только репортит, не трогает** |

Каждая джоба пишет `job_heartbeats`; админ-экран показывает «последний
успех/ошибка» — молчаливо умершая джоба видна сразу. Бэкапы БД — вне бота:
pg_dump по cron + копия вне сервера (runbook в README).

## 9. UX бота (RU, HTML)

Пользователь:
- `/start` → меню: «🎁 3 дня бесплатно» (если триал не использован) /
  «🛒 Купить подписку» / «🌐 Моя подписка» / «📖 Как подключить» / «❓ Помощь».
- Покупка: сетка тарифов из БД → выбор способа (ЮMoney | CryptoBot) → счёт с
  кнопками «Оплатить» / «Проверить оплату» / «Отменить».
- «Моя подписка»: статус и срок из БД + живой трафик из `/api/info/{token}`,
  кнопки «🔗 Открыть мою подписку», «📋 Скопировать ссылку» (CopyTextButton),
  «🛒 Продлить», «📖 Как подключить».
- Напоминания за 3 и 1 день до окончания — с кнопкой продления.
- FSM-минимум: имён подписок нет, состояния остались только в админских
  сценариях. Устаревшие callback'и гасит error-middleware: «Меню устарело,
  нажмите /start».

Админ (`ADMIN_IDS`):
- 📊 Статистика: выручка (день/неделя/месяц), активные/триальные подписки,
  конверсия триала, heartbeats джоб, `/health` панели.
- 👤 Пользователь по tg id/username: подписка, платежи, выдать/продлить/
  отозвать N дней, «Повторить провижининг» для зависших `paid`.
- 🧾 Тарифы: цены и активность прямо из бота.
- 📣 Рассылка: превью → подтверждение → резюмируемая отправка с курсором,
  обработкой RetryAfter и учётом заблокировавших.

## 10. Гайд подключения (видео убраны, расчёт на самого неопытного)

Видеоинструкции удаляются полностью. Вместо них — три коротких шага и, по
возможности, импорт подписки **в одно нажатие**.

**Шаг 1. Скачать Happ** — кнопки по платформам:

| Платформа | Ссылка |
|---|---|
| iPhone / iPad / macOS | App Store — `https://apps.apple.com/app/id6504287215` |
| Android | Google Play — `https://play.google.com/store/apps/details?id=com.happproxy` |
| Windows / Linux / Android TV / Apple TV | Официальный сайт — `https://happ.su` (раздел «Скачать») |

Ссылки хранятся в конфиге (не в коде) и сверяются с happ.su при запуске:
у проекта нет стабильных прямых ссылок на десктопные сборки, поэтому для
Windows/Linux/TV ведём на официальную страницу загрузок.

**Шаг 2. «🔗 Открыть мою подписку»** — https-кнопка на страницу панели
`https://link.rillza-service.com/api/files/{token}`. Панель отдаёт браузеру
готовую страницу: QR-код, срок действия, остаток трафика, кнопка копирования
и сетка кнопок приложений.

**Шаг 3. На странице нажать «HAPP»** — кнопка ведёт на `happ://add/{url}`,
Happ открывается и импортирует подписку сам. Дальше пользователю остаётся
нажать кнопку подключения в приложении.

Почему через страницу панели, а не кнопкой в боте: Telegram разрешает в
inline-кнопках только `http`/`https`/`tg`, кастомную схему `happ://` в
кнопку не положить. Страница панели — легальный https-мост, и она уже умеет
всё нужное.

**Запасной путь** (если что-то пошло не так): «📋 Скопировать ссылку» в боте
→ в Happ «+» → «Вставить из буфера». Плюс QR на той же странице — для
установки со второго устройства.

**Разовая настройка панели владельцем** (без неё шаг 3 не работает):
Settings → Subscription → Buttons → добавить кнопку `HAPP` с URL
`happ://add/{url}` (шаблон уже есть в UI панели). Там же полезно включить
Soft-block для истёкших: вместо ошибки 403 пользователь увидит подписку с
поясняющим текстом («Подписка закончилась — продлите в боте»).

## 11. Структура проекта

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

Старый код `src/` (вместе с `src/assets/*.mp4`, ~100 МБ видео) удаляется в
финальной фазе.

## 12. Этапы

1. **Каркас**: uv, ruff, CI, compose (bot+pg+redis), settings, loguru с
   починенной разбивкой, пустой aiogram-бот с `/start`. CI зелёный.
2. **БД**: модели, Alembic, репозитории, UoW, сид тарифов; интеграционные
   тесты на реальном Postgres.
3. **Клиент CELERITY**: create-or-fetch (409), extend (PUT, абсолютный
   `expireAt`), disable, info; ретраи и лимиты; contract-тесты на моках.
4. **Триал + «Моя подписка» + гайд**: выдача 3 дней end-to-end (панель →
   ссылка → импорт в Happ), экран подписки, экран подключения. Бот полезен
   ещё до денег.
5. **Платежи**: свои клиенты ЮMoney и CryptoBot, `check_and_finalize`,
   поллер/watcher/expirer; тесты гонок (двойной клик, рестарт между `paid` и
   `provisioned`, поздний платёж).
6. **Админка, рассылки, напоминания 3д/1д.**
7. **Реконсилятор, heartbeats, README** (установка, создание API-ключа и
   кнопки HAPP в панели, runbook: бэкапы, ручной возврат, «панель
   недоступна»), `.env.example`; удаление `src/`.

## 13. Env

```
BOT_TOKEN=            ADMIN_IDS=[...]         TRIAL_DAYS=3
DATABASE_URL=postgresql+asyncpg://rillza:***@postgres:5432/rillza
REDIS_URL=redis://redis:6379/0
PANEL_BASE_URL=https://link.rillza-service.com
PANEL_API_KEY=ck_...  PANEL_GROUP_NAME=Rillza   # резолвится в _id на старте
YOOMONEY_ACCESS_TOKEN=  YOOMONEY_WALLET=        # кошелёк; иначе account-info
YOOMONEY_PAYMENT_TYPE=AC                        # AC=карта, PC=кошелёк
CRYPTOBOT_TOKEN=
INVOICE_TTL_MINUTES=30
HAPP_IOS_URL=... HAPP_ANDROID_URL=... HAPP_SITE_URL=https://happ.su
LOG_BOT_TOKEN=  LOG_CHAT_ID=
```

## 14. Статус решений

Подтверждено: ключ — Telegram ID; одна подписка на пользователя; сетка
1/3/6/12 мес; триал 3 дня; переноса из 3x-ui нет; видеогайды убраны, вместо
них ссылки на Happ и импорт в одно нажатие; напоминания 3д/1д остаются;
ЮMoney — тот же p2p-API своим клиентом; HWID — без покупки Provider ID.

Осталось решить перед запуском: конкретные цены m1/m3/m6/m12; режим HWID
(`permissive` или `strict`) и `maxDevices` на серверной группе; включать ли
Soft-block для истёкших подписок.
