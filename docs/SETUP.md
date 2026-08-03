# Настройка: откуда брать переменные окружения

Все переменные живут в `.env` рядом с `compose.yaml`. Файл в
`.gitignore` — в репозиторий он не попадает и попасть не должен.

```bash
cp .env.example .env
```

Ниже — только то, что нужно заполнить руками. Остальное в
[.env.example](../.env.example) уже имеет рабочие значения.

## Обязательные

### `BOT_TOKEN`

[@BotFather](https://t.me/BotFather) → `/newbot` → имя и username.
В ответ придёт строка вида `8123456789:AAH...`. Если бот уже создан:
`/mybots` → выбрать → **API Token**.

Заодно там же стоит выставить `/setdescription` и `/setuserpic` — их
видит человек, открывший бота впервые.

### `ADMIN_IDS`

Числовой Telegram ID, а не username. Узнать свой:
[@userinfobot](https://t.me/userinfobot) → он ответит числом вроде
`123456789`.

Несколько админов через запятую: `ADMIN_IDS=123456789,987654321`.
Пустое значение означает, что админки нет ни у кого — включая вас.

### `POSTGRES_PASSWORD` и `DATABASE_URL`

Пароль вы придумываете сами; база поднимается в докере и наружу не
торчит. Сгенерировать:

```bash
openssl rand -base64 24
```

**Пароль должен совпадать в двух местах.** `POSTGRES_PASSWORD` создаёт
пользователя базы, `DATABASE_URL` под ним подключается:

```
POSTGRES_PASSWORD=сгенерированный
DATABASE_URL=postgresql+asyncpg://rillza:сгенерированный@postgres:5432/rillza
```

Если в пароле есть `@`, `/`, `:` или `#` — в `DATABASE_URL` их нужно
процентно закодировать (`@` → `%40`), иначе строка подключения
разберётся неправильно. Проще сгенерировать пароль без них:

```bash
openssl rand -hex 24
```

### `PANEL_API_KEY`

Панель CELERITY → **Settings → Security → API Keys** → создать ключ со
скоупами `users:read`, `users:write`, `stats:read`. Значение показывают
один раз, начинается с `ck_`.

`PANEL_BASE_URL` и `PANEL_GROUP_NAME` в `.env.example` уже заполнены под
текущую панель.

## Оплата

Провайдер без ключа просто не показывается на экране покупки — можно
запуститься с одним и добавить второй позже. Но если не задан ни один,
покупка недоступна и работает только пробный период.

### `YOOMONEY_ACCESS_TOKEN`

Токен личного кошелька ЮMoney, мерчант-аккаунт не нужен. Порядок:

1. Зарегистрировать приложение: <https://yoomoney.ru/myservices/new>.
   Redirect URI можно указать любой, например `https://example.com/` —
   он нужен только чтобы поймать код на следующем шаге. Запишите
   `client_id`.
2. Открыть в браузере, подставив свои значения:

   ```
   https://yoomoney.ru/oauth/authorize
     ?client_id=ВАШ_CLIENT_ID
     &response_type=code
     &redirect_uri=https://example.com/
     &scope=account-info%20operation-history
   ```

   Подтвердить доступ. Браузер уйдёт на redirect URI, и в адресной
   строке будет `?code=XXXXX` — это одноразовый код, живёт недолго.
3. Обменять код на токен:

   ```bash
   curl -s https://yoomoney.ru/oauth/token \
     -d code=XXXXX \
     -d client_id=ВАШ_CLIENT_ID \
     -d grant_type=authorization_code \
     -d redirect_uri=https://example.com/
   ```

   В ответе `{"access_token":"..."}` — это и есть
   `YOOMONEY_ACCESS_TOKEN`.

Оба скоупа обязательны: `account-info` — узнать номер кошелька,
`operation-history` — увидеть входящий платёж. Токен только с первым
выставляет счета безупречно и не подтверждает ни одного: деньги
приходят, доступ не выдаётся. `check_payments` проверяет оба и говорит
об этом прямо — в успешном выводе есть `operation-history readable`.

`YOOMONEY_WALLET` можно оставить пустым — номер прочитается из API при
старте.

### `BOT_URL`

Куда ЮMoney вернёт плательщика после оплаты: `https://t.me/ваш_бот`
(тот самый username из BotFather). Если оставить пустым, человек после
оплаты останется на странице ЮMoney и будет гадать, что дальше.

### `CRYPTOBOT_TOKEN`

[@CryptoBot](https://t.me/CryptoBot) → **Crypto Pay** → **Create App** →
выбрать `Mainnet`. Токен вида `12345:AA...` показывается сразу.

Счета выставляются в рублях (`currency_type=fiat`), конвертацию делает
сам CryptoBot — отдельная цена в крипте не нужна.

## Необязательное

`LOG_BOT_TOKEN` + `LOG_CHAT_ID` включают зеркалирование логов в
Telegram. Второй бот создаётся так же через BotFather; ID чата (или
канала, куда бот добавлен админом) даст
[@userinfobot](https://t.me/userinfobot) — у каналов он отрицательный,
вида `-1001234567890`. Задавать нужно **обе** переменные: с одной
логирование остаётся выключенным.

Уровень по умолчанию `WARNING` — приходят только проблемы, не каждый шаг.

## Проверка перед запуском

Три команды, все read-only: ничего не создают, не меняют и не списывают.

```bash
# 1. Настройки читаются, обязательные поля на месте
uv run python -c "from app.core.settings import get_settings; \
  s = get_settings(); print('ok:', s.panel_base_url, s.admin_ids)"

# 2. Панель: доступность, ключ, группа, лимит устройств
uv run python -m scripts.check_panel

# 3. Платёжки: кому принадлежат токены
uv run python -m scripts.check_payments
```

Если бот уже поднят в докере, то же самое внутри контейнера:

```bash
docker compose exec bot python -m scripts.check_panel
docker compose exec bot python -m scripts.check_payments
```

Ожидаемый вывод второй и третьей:

```
[  OK  ] health: status='ok'
[  OK  ] api key accepted, 1 active group(s):
         Celerity Primary Access <- PANEL_GROUP_NAME
[  OK  ] group 'Celerity Primary Access' resolved
[  OK  ] devices: group 'Celerity Primary Access' caps at 2 device(s)

[  OK  ] yoomoney: wallet 4100…, balance 0.00 643, operation-history readable
[  OK  ] cryptobot: app Rillza via @CryptoBot
```

Обе завершаются кодом `0` при успехе и `1` при проблеме, так что их
можно ставить в CI или в скрипт деплоя.

## Запуск и первая проверка вживую

```bash
docker compose up -d --build
docker compose logs -f bot
```

В логах при здоровом старте: применение миграций и строка
`Rillza VPN bot started polling; payment providers: ...` — в ней
перечислено, какие способы оплаты реально поднялись.

Дальше — сквозная проверка руками:

1. Открыть бота, `/start` → «🎁 Пробный период» → доступ на 3 дня.
2. «🌐 Моя подписка» → «🔗 Открыть подписку» → на странице кнопка
   **HAPP** → приложение настроится само.
3. Ещё раз `check_panel`. Число аккаунтов вырастет — это ожидаемо, и
   список «с явным maxDevices» вырастет вместе с ним: бот теперь
   ставит `maxDevices` явно (2 у триала, 2 или 4 у купленных тарифов)
   на каждом аккаунте, которым управляет, так что рост этого списка
   сам по себе ни о чём не говорит.

То, что `groups` доехал до `POST /api/users`, уже проверено шагом 2:
если HAPP показал рабочие сервера — VLESS доставлен. Если подключения
нет, а `check_panel` при этом не показывает `FAIL`, проверьте `groups`
у конкретного аккаунта напрямую — `GET /api/users/{tg_id}` — поле не
должно быть пустым.

Проверка покупки на реальные деньги: поставьте временно цену в 1 ₽
прямо в базе, купите, верните обратно.

```bash
docker compose exec postgres psql -U rillza rillza \
  -c "update tariffs set price_kopeks = 100 where code = 'm1';"
# ... купить, проверить выдачу доступа ...
docker compose exec postgres psql -U rillza rillza \
  -c "update tariffs set price_kopeks = 20000 where code = 'm1';"
```

Цены живут в базе именно для этого — деплой ради смены цены не нужен.
