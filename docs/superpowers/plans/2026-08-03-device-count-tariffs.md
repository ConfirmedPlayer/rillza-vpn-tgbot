# Тарифы с числом устройств — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Человек может купить подписку на 4 устройства вместо 2, и
купленное число доезжает до панели, переживает продление и чинится
сверкой.

**Architecture:** Число устройств — колонка тарифа. Покупка выбирает
сначала число устройств, потом срок. Купленное значение ложится в
`subscriptions.max_devices` в той же транзакции, что дни и защёлка
идемпотентности, и уходит в панель явным числом одним PUT вместе с
датой. Сверка чинит расхождение по лимиту так же, как чинит дату.

**Tech Stack:** Python 3.13, aiogram 3, SQLAlchemy 2 async, Alembic,
PostgreSQL 17, pytest + pytest-asyncio, ruff.

**Спека:** [2026-08-03-device-count-tariffs-design.md](../specs/2026-08-03-device-count-tariffs-design.md)

## Global Constraints

- Ветка `master`. Она по умолчанию, не `main`.
- ruff, длина строки 79, одинарные кавычки. Кириллица в строках —
  норма, RUF001-003 отключены.
- Комментарии и docstring — по-английски, как во всём коде. Тексты для
  пользователя — по-русски.
- База — источник правды. Пишем и коммитим в базу, потом приводим
  панель в соответствие.
- Панель: `DELETE` не использовать никогда, `POST /api/users/{id}/enable`
  не использовать, `expireAt` всегда абсолютный, создание обязано слать
  `enabled: true` и `groups` одним POST.
- Интеграционные тесты требуют настоящий PostgreSQL. Прогон без
  `TEST_DATABASE_URL` зелёный и не проверяет ничего.
- Каждый тест обязан падать при откате **своего** куска, а не всей
  фичи. Проверяется руками: убрать правку, увидеть падение, вернуть.
- Формулировки для пользователя: «до N устройств», без обещания
  блокировки.

**Разовый Postgres для прогонов:**

```bash
docker run -d --rm --name pg-test -e POSTGRES_PASSWORD=test -e POSTGRES_USER=postgres -e POSTGRES_DB=rillza_test -p 5434:5432 postgres:17-alpine
```

**Полный прогон** (везде ниже `pytest` — это он):

```bash
TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/rillza_test uv run pytest
```

**Не пересобирать боевой контейнер, пока владелец что-то проверяет
вживую.** Пересборка посреди покупки убивает счёт без следа, и логи
старого контейнера удаляются вместе с ним.

---

## Карта файлов

| Файл | Что с ним происходит |
|---|---|
| `app/db/models.py` | +`Tariff.max_devices`, +`Subscription.max_devices` |
| `alembic/versions/c4e1f7a2b930_tariff_device_count.py` | создать: колонки + сид четырёх тарифов |
| `app/integrations/celerity/client.py` | `set_expiry` → `set_state`, `create_or_get_user` принимает `max_devices` |
| `app/services/subscription_service.py` | `create_pending` требует `max_devices`, `push_expiry` → `push_state`, `provision` доправляет и лимит |
| `app/repositories/payments.py` | +`newest_applied_paid_at` |
| `app/repositories/tariffs.py` | `list_active(max_devices)`, +`list_device_counts` |
| `app/services/payment_service.py` | `_apply_days` проставляет число устройств по последнему платежу |
| `app/services/trial_service.py` | триал — `DEFAULT_MAX_DEVICES` |
| `app/services/reconcile_service.py` | +`devices_fixed`, третья проверка |
| `app/services/support_service.py` | +`relay_composed` |
| `app/bot/keyboards.py` | экран выбора устройств, экран предупреждения, кнопки запроса |
| `app/bot/routers/buy.py` | шаг `devices:N`, предупреждение о понижении |
| `app/bot/routers/menu.py` | кнопка запроса на экране подписки |
| `app/bot/routers/admin.py` | `create_pending` с `DEFAULT_MAX_DEVICES` |
| `app/bot/texts/ru.py` | новые экраны, строка про устройства |
| `app/bot/texts/support.py` | два готовых запроса |
| `app/bot/texts/admin.py` | `render_tariff`/`render_tariffs` показывают лимит |
| `tests/fake_panel.py` | зеркалит новую сигнатуру клиента |
| `tests/integration/conftest.py` | сид из восьми тарифов |

---

### Task 1: Колонки `max_devices` и четыре новых тарифа

**Files:**
- Modify: `app/db/models.py:79-113` (`Tariff`), `app/db/models.py:115-157` (`Subscription`)
- Create: `alembic/versions/c4e1f7a2b930_tariff_device_count.py`
- Modify: `tests/integration/conftest.py:34-39`, `tests/integration/conftest.py:80-95`
- Test: `tests/integration/test_repositories.py`

**Interfaces:**
- Consumes: ничего
- Produces: `Tariff.max_devices: int`, `Subscription.max_devices: int`;
  коды новых тарифов `m1x4`, `m3x4`, `m6x4`, `m12x4`; фикстура
  `seeded_tariffs` возвращает восемь тарифов в порядке
  `m1, m3, m6, m12, m1x4, m3x4, m6x4, m12x4`

- [ ] **Step 1: Написать падающий тест**

В конец `tests/integration/test_repositories.py`:

```python
async def test_seeded_tariffs_carry_a_device_count(
    uow: UnitOfWork, seeded_tariffs
) -> None:
    """Four-device plans sit next to the existing ones, same durations."""
    two = await uow.tariffs.get_by_code('m1')
    four = await uow.tariffs.get_by_code('m1x4')

    assert two is not None
    assert four is not None
    assert two.max_devices == 2
    assert four.max_devices == 4
    assert four.duration_days == two.duration_days
    assert four.price_kopeks == 32_000
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/rillza_test uv run pytest tests/integration/test_repositories.py::test_seeded_tariffs_carry_a_device_count -v`

Expected: FAIL — `AttributeError: 'Tariff' object has no attribute 'max_devices'`

- [ ] **Step 3: Добавить колонки в модели**

В `app/db/models.py`, класс `Tariff`, после `duration_days`:

```python
    # 2 or 4. The panel is told this number explicitly instead of the
    # inherit-from-group 0, so what was sold is what it enforces.
    max_devices: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default='2'
    )
```

В классе `Subscription`, после `expires_at`:

```python
    # What the buyer paid for. Pushed to the panel as an explicit
    # number, so the account stops following the group's limit.
    max_devices: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default='2'
    )
```

- [ ] **Step 4: Написать миграцию**

Создать `alembic/versions/c4e1f7a2b930_tariff_device_count.py`:

```python
"""tariff device count

Adds the device count to tariffs and subscriptions and seeds the four
four-device plans next to the existing ones.

``server_default = '2'`` is the truth for every existing row, not a
guess: the four current tariffs sell two devices, and every live
subscription runs on a panel account with ``maxDevices = 0``, which
inherits the group's limit of two.

Prices are the existing ladder times 1.6. A flat multiplier keeps the
per-month discounts identical in both sets, so the "выгода N%" badges
read the same whichever list the buyer opens.

Revision ID: c4e1f7a2b930
Revises: dc026fb20f91
Create Date: 2026-08-03 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'c4e1f7a2b930'
down_revision: str | Sequence[str] | None = 'dc026fb20f91'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TARIFFS = (
    # code, title, days, price in kopeks, devices, order
    ('m1x4', '1 месяц', 30, 32_000, 4, 5),
    ('m3x4', '3 месяца', 90, 86_400, 4, 6),
    ('m6x4', '6 месяцев', 180, 153_600, 4, 7),
    ('m12x4', '12 месяцев', 365, 268_800, 4, 8),
)


def upgrade() -> None:
    op.add_column(
        'tariffs',
        sa.Column(
            'max_devices',
            sa.Integer(),
            nullable=False,
            server_default='2',
        ),
    )
    op.add_column(
        'subscriptions',
        sa.Column(
            'max_devices',
            sa.Integer(),
            nullable=False,
            server_default='2',
        ),
    )

    tariffs = sa.table(
        'tariffs',
        sa.column('code', sa.String),
        sa.column('title_ru', sa.String),
        sa.column('duration_days', sa.Integer),
        sa.column('price_kopeks', sa.Integer),
        sa.column('max_devices', sa.Integer),
        sa.column('sort_order', sa.Integer),
    )
    op.bulk_insert(
        tariffs,
        [
            {
                'code': code,
                'title_ru': title,
                'duration_days': days,
                'price_kopeks': price,
                'max_devices': devices,
                'sort_order': order,
            }
            for code, title, days, price, devices, order in TARIFFS
        ],
    )


def downgrade() -> None:
    # Deleting a tariff someone has bought raises on the payments FK.
    # That is the correct outcome: rolling back on a database with
    # sales must fail loudly rather than drop money records.
    codes = ', '.join(f"'{code}'" for code, *_ in TARIFFS)
    op.execute(f'DELETE FROM tariffs WHERE code IN ({codes})')
    op.drop_column('subscriptions', 'max_devices')
    op.drop_column('tariffs', 'max_devices')
```

- [ ] **Step 5: Обновить сид в тестовой фикстуре**

В `tests/integration/conftest.py` заменить `SEED_TARIFFS` и фикстуру:

```python
SEED_TARIFFS = (
    # code, title, days, price in kopeks, devices, order
    ('m1', '1 месяц', 30, 20_000, 2, 1),
    ('m3', '3 месяца', 90, 54_000, 2, 2),
    ('m6', '6 месяцев', 180, 96_000, 2, 3),
    ('m12', '12 месяцев', 365, 168_000, 2, 4),
    ('m1x4', '1 месяц', 30, 32_000, 4, 5),
    ('m3x4', '3 месяца', 90, 86_400, 4, 6),
    ('m6x4', '6 месяцев', 180, 153_600, 4, 7),
    ('m12x4', '12 месяцев', 365, 268_800, 4, 8),
)
```

```python
@pytest_asyncio.fixture
async def seeded_tariffs(uow: UnitOfWork) -> list[Tariff]:
    """The eight tariffs the seed migrations install.

    Order matters: existing tests index the two-device plans by
    position, so the four-device ones are appended, never interleaved.
    """
    tariffs = [
        Tariff(
            code=code,
            title_ru=title,
            duration_days=days,
            price_kopeks=price,
            max_devices=devices,
            sort_order=order,
        )
        for code, title, days, price, devices, order in SEED_TARIFFS
    ]
    uow.session.add_all(tariffs)
    await uow.commit()
    return tariffs
```

- [ ] **Step 6: Убедиться, что тест проходит**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/rillza_test uv run pytest tests/integration/test_repositories.py -v`

Expected: PASS, включая новый тест.

- [ ] **Step 7: Проверить миграцию на чистой базе**

```bash
docker exec pg-test psql -U postgres -c 'create database rillza_drift'
```

Затем с `DATABASE_URL`, указывающим на `rillza_drift`:

```bash
uv run alembic upgrade head && uv run alembic check && uv run alembic downgrade -1 && uv run alembic upgrade head
```

Expected: `upgrade` без ошибок, `check` печатает «No new upgrade
operations detected», `downgrade`/`upgrade` проходят.

Если `alembic check` жалуется на `panel_api_key` — это не миграции, а
отсутствующий `.env`; переменные окружения нужны команде.

- [ ] **Step 8: Коммит**

```bash
git add app/db/models.py alembic/versions/c4e1f7a2b930_tariff_device_count.py tests/integration/conftest.py tests/integration/test_repositories.py
git commit -m "feat: tariffs and subscriptions carry a device count"
```

---

### Task 2: Панель получает число устройств одним PUT

**Files:**
- Modify: `app/integrations/celerity/client.py:243-289`
- Modify: `tests/fake_panel.py:65-105`
- Test: `tests/contract/test_celerity_client.py:196-290`

**Interfaces:**
- Consumes: `Subscription.max_devices` из Task 1
- Produces:
  - `CelerityClient.create_or_get_user(panel_user_id: str, expire_at: datetime | None, *, max_devices: int, username: str = '', group_id: str | None = None) -> tuple[PanelUser, bool]`
  - `CelerityClient.set_state(panel_user_id: str, expire_at: datetime | None, max_devices: int) -> PanelUser`
  - `FakePanel` с теми же сигнатурами

- [ ] **Step 1: Написать падающие тесты**

В `tests/contract/test_celerity_client.py`, класс `TestRenewAndRevoke`,
добавить:

```python
    async def test_renewal_carries_the_device_limit(
        self, client, mocked
    ) -> None:
        """One PUT, both fields.

        Two calls would leave "expiry updated, limit not" alive until
        the next reconcile — up to four hours.
        """
        target = NOW + timedelta(days=30)
        mocked.put(f'{BASE}/api/users/42', payload=user_payload())

        await client.set_state('42', target, 4)

        body = last_body(mocked)
        assert body == {'expireAt': target.isoformat(), 'maxDevices': 4}
        await client.close()
```

И заменить существующее утверждение в
`test_create_sends_enabled_and_groups_together` (строка 221):

```python
        assert body['maxDevices'] == 4
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/contract/test_celerity_client.py -v`

Expected: FAIL — `AttributeError: 'CelerityClient' object has no
attribute 'set_state'` и `assert 0 == 4`.

- [ ] **Step 3: Поправить клиент**

В `app/integrations/celerity/client.py` заменить `create_or_get_user` и
`set_expiry`:

```python
    async def create_or_get_user(
        self,
        panel_user_id: str,
        expire_at: datetime | None,
        *,
        max_devices: int,
        username: str = '',
        group_id: str | None = None,
    ) -> tuple[PanelUser, bool]:
        """Create the account, or return the existing one.

        Returns ``(user, created)``. A 409 carries the existing user in
        the body, which makes this a natural create-or-fetch.

        ``max_devices`` is keyword-only and has no default on purpose:
        a default would silently sell two devices to someone who paid
        for four.
        """
        body = {
            'userId': panel_user_id,
            'username': username,
            # Both of these must travel with the create request; see the
            # module docstring.
            'enabled': True,
            'groups': [group_id or await self.resolve_group_id()],
            'expireAt': _isoformat(expire_at),
            # We sell unlimited traffic; the device count is ours.
            'trafficLimit': 0,
            'maxDevices': max_devices,
        }
        try:
            payload = await self._request('POST', '/api/users', json=body)
        except PanelConflictError:
            existing = await self.get_user(panel_user_id)
            if existing is None:  # pragma: no cover - panel contradiction
                raise
            return existing, False
        return PanelUser.model_validate(payload), True

    async def set_state(
        self,
        panel_user_id: str,
        expire_at: datetime | None,
        max_devices: int,
    ) -> PanelUser:
        """Push the two fields the database owns. Re-enables a lapsed user.

        ``expire_at`` is absolute — the caller computes
        ``max(now, current) + duration`` once and reuses it on retries.

        Both fields go in one request. Sending them separately would
        leave "expiry updated, limit not" between the two calls, and
        the panel is not transactional, so that state survives until
        the next reconcile.
        """
        payload = await self._request(
            'PUT',
            f'/api/users/{panel_user_id}',
            json={
                'expireAt': _isoformat(expire_at),
                'maxDevices': max_devices,
            },
        )
        return PanelUser.model_validate(payload)
```

- [ ] **Step 4: Обновить FakePanel**

В `tests/fake_panel.py` заменить `create_or_get_user` и `set_expiry`:

```python
    async def create_or_get_user(
        self,
        panel_user_id: str,
        expire_at: datetime | None,
        *,
        max_devices: int,
        username: str = '',
        group_id: str | None = None,
    ) -> tuple[PanelUser, bool]:
        self._guard(f'create_or_get_user:{panel_user_id}')
        existing = self.users.get(panel_user_id)
        if existing is not None:
            return existing, False
        user = PanelUser(
            userId=panel_user_id,
            username=username,
            enabled=True,
            expireAt=expire_at,
            trafficLimit=0,
            maxDevices=max_devices,
            subscriptionToken=secrets.token_hex(8),
        )
        self.users[panel_user_id] = user
        return user, True

    async def set_state(
        self,
        panel_user_id: str,
        expire_at: datetime | None,
        max_devices: int,
    ) -> PanelUser:
        self._guard(f'set_state:{panel_user_id}')
        user = self._require(panel_user_id)
        updated = user.model_copy(
            update={
                'expire_at': expire_at,
                'max_devices': max_devices,
                'enabled': True,
            }
        )
        self.users[panel_user_id] = updated
        return updated
```

- [ ] **Step 5: Поправить остальные вызовы в контрактных тестах**

Строки 214, 239, 254 — добавить `max_devices`:

```python
        user, created = await client.create_or_get_user(
            '42', NOW, username='ivan', max_devices=4
        )
```

```python
        user, created = await client.create_or_get_user(
            '42', NOW, group_id=GROUP_ID, max_devices=2
        )
```

```python
        user, _ = await client.create_or_get_user(
            '42', None, group_id=GROUP_ID, max_devices=2
        )
```

Строки 271 и 284 — `set_expiry` → `set_state` с третьим аргументом:

```python
        await client.set_state('42', target, 2)
```

В `test_renewal_is_idempotent_under_retry` (строка 284) утверждение о
теле остаётся тем же — оно читает только `expireAt`.

- [ ] **Step 6: Убедиться, что контрактные тесты проходят**

Run: `uv run pytest tests/contract/ -v`

Expected: PASS. Интеграционные пока падают — их вызовы чинит Task 3.

- [ ] **Step 7: Коммит**

```bash
git add app/integrations/celerity/client.py tests/fake_panel.py tests/contract/test_celerity_client.py
git commit -m "feat: the panel is told the device count explicitly"
```

---

### Task 3: Число устройств живёт весь жизненный цикл подписки

**Files:**
- Modify: `app/services/subscription_service.py:58-179`
- Modify: `app/services/trial_service.py:82-87`
- Modify: `app/services/payment_service.py:292-298`, `app/services/payment_service.py:320`
- Modify: `app/bot/routers/admin.py:274-279`
- Modify: `tests/integration/test_reconcile.py:41`, `:154`, `:175`
- Modify: `tests/integration/test_scheduler_jobs.py:200`, `:220`, `:246`
- Modify: `tests/integration/test_trial_flow.py:363`
- Modify: `tests/integration/test_support_flow.py:422`
- Modify: `tests/integration/test_admin_flow.py:195`, `:351`
- Modify: `tests/integration/test_payment_flow.py:535`
- Test: `tests/integration/test_trial_flow.py`, `tests/integration/test_reconcile.py`, `tests/integration/test_admin_flow.py`

**Interfaces:**
- Consumes: `CelerityClient.set_state`, `create_or_get_user(max_devices=...)` из Task 2
- Produces:
  - `subscription_service.DEFAULT_MAX_DEVICES = 2`
  - `SubscriptionService.create_pending(telegram_id, expires_at, origin, max_devices, commit=True)` — `max_devices` обязателен, идёт до `commit`
  - `SubscriptionService.push_state(subscription)` (был `push_expiry`)

- [ ] **Step 1: Написать падающие тесты**

В `tests/integration/test_trial_flow.py`, класс `TestTrialFlow`:

```python
    async def test_trial_is_two_devices(
        self, dispatcher, bot, session, panel, session_factory
    ) -> None:
        """The trial never inherits a paid plan's device count."""
        await dispatcher.feed_update(
            bot, callback_update(keyboards.TRIAL_CONFIRM)
        )

        assert panel.users[str(USER_ID)].max_devices == 2
        async with UnitOfWork(session_factory) as uow:
            subscription = await uow.subscriptions.get_by_user(USER_ID)
            assert subscription is not None
            assert subscription.max_devices == 2
```

В `tests/integration/test_reconcile.py` сперва расширить существующий
хелпер `make_subscription` (строка 38) — он зовётся из всех тестов
файла:

```python
async def make_subscription(
    uow, subscriptions, days=30, provision=True, max_devices=2
):
    await uow.users.upsert(USER_ID)
    await uow.commit()
    subscription = await subscriptions.create_pending(
        USER_ID,
        expires_at=datetime.now(UTC) + timedelta(days=days),
        origin=SubscriptionOrigin.PURCHASE,
        max_devices=max_devices,
    )
    if provision:
        await subscriptions.provision(subscription)
    return subscription
```

И добавить тест:

```python
async def test_provision_corrects_a_stale_device_limit(
    uow, subscriptions, panel
) -> None:
    """An existing panel account with the right date and the wrong
    limit used to be left alone: provision compared expiries only, so a
    returning buyer paid for four devices and kept two."""
    subscription = await make_subscription(
        uow, subscriptions, max_devices=4, provision=False
    )
    # The account predates this purchase: same date, old limit.
    await panel.create_or_get_user(
        str(USER_ID), expire_at=subscription.expires_at, max_devices=2
    )

    await subscriptions.provision(subscription)

    assert panel.users[str(USER_ID)].max_devices == 4
```

В `tests/integration/test_admin_flow.py` — админская выдача не трогает
число устройств. Фикстуры и `ADMIN_ID = 42` в файле уже есть:

```python
    async def test_a_grant_does_not_change_the_device_count(
        self, dispatcher, bot, session, session_factory, panel, admin_settings
    ) -> None:
        """More devices are sold, not granted: «➕ 30 дней» moves the
        date and nothing else."""
        async with UnitOfWork(session_factory) as uow:
            await uow.users.upsert(CUSTOMER_ID, username='ivan')
            await uow.commit()
            subscriptions = SubscriptionService(uow, panel, admin_settings)
            subscription = await subscriptions.create_pending(
                CUSTOMER_ID,
                expires_at=datetime.now(UTC) + timedelta(days=10),
                origin=SubscriptionOrigin.PURCHASE,
                max_devices=4,
            )
            await subscriptions.provision(subscription)

        await dispatcher.feed_update(
            bot,
            callback_update(f'{keyboards.ADMIN_GRANT_PREFIX}{CUSTOMER_ID}:30'),
        )

        async with UnitOfWork(session_factory) as uow:
            refreshed = await uow.subscriptions.get_by_user(CUSTOMER_ID)
            assert refreshed is not None
            assert refreshed.max_devices == 4
            assert refreshed.expires_at > datetime.now(UTC) + timedelta(
                days=39
            )
        assert panel.users[str(CUSTOMER_ID)].max_devices == 4
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/rillza_test uv run pytest tests/integration/test_trial_flow.py tests/integration/test_reconcile.py -v`

Expected: FAIL — `TypeError: create_pending() got an unexpected keyword
argument 'max_devices'`.

- [ ] **Step 3: Поправить SubscriptionService**

В `app/services/subscription_service.py` после `utcnow` добавить:

```python
#: What a subscription gets when nobody bought a device count: the
#: trial, and an admin grant to someone who had no subscription.
DEFAULT_MAX_DEVICES = 2
```

`create_pending` — новый обязательный параметр перед `commit`:

```python
    async def create_pending(
        self,
        telegram_id: int,
        expires_at: datetime,
        origin: SubscriptionOrigin,
        max_devices: int,
        commit: bool = True,
    ) -> Subscription:
        """Record the subscription before touching the panel.

        ``max_devices`` has no default deliberately. A default would
        eventually reach the trial or an admin grant silently, and the
        number in this column is what the panel will enforce.

        ``commit=False`` leaves the row flushed but uncommitted, for a
        caller that must land it together with something else. Payment
        provisioning needs that: committing the days here and the
        idempotency latch afterwards made them two transactions, and a
        crash in the gap left days granted with nothing to stop them
        being granted again.
        """
        subscription = Subscription(
            id=uuid.uuid4(),
            user_id=telegram_id,
            status=SubscriptionStatus.PENDING,
            origin=origin,
            expires_at=expires_at,
            max_devices=max_devices,
            panel_user_id=str(telegram_id),
        )
        await self._uow.subscriptions.add(subscription)
        if commit:
            await self._uow.commit()
        return subscription
```

`provision` — передать лимит и сравнивать оба поля:

```python
        panel_user, created = await self._panel.create_or_get_user(
            subscription.panel_user_id,
            expire_at=subscription.expires_at,
            max_devices=subscription.max_devices,
            username=username,
        )
        if not created and (
            panel_user.expire_at != subscription.expires_at
            or panel_user.max_devices != subscription.max_devices
        ):
            # The account predates this subscription (a returning user,
            # a half-finished attempt, or a plan with a different
            # device count): move it onto our values.
            panel_user = await self._panel.set_state(
                subscription.panel_user_id,
                subscription.expires_at,
                subscription.max_devices,
            )
```

`extend` — последняя строка становится `await self.push_state(subscription)`.

`push_expiry` → `push_state`:

```python
    async def push_state(self, subscription: Subscription) -> Subscription:
        """Make the panel agree with the row as it stands right now.

        Sends the subscription's own values rather than any caller-held
        ones, so a retry can never install an outdated expiry or an
        outdated device limit.
        """
        try:
            panel_user = await self._panel.set_state(
                subscription.panel_user_id,
                subscription.expires_at,
                subscription.max_devices,
            )
        except PanelNotFoundError:
            # No account yet (an earlier provisioning failed) or it was
            # removed in the panel: create it rather than lose the days.
            panel_user, created = await self._panel.create_or_get_user(
                subscription.panel_user_id,
                expire_at=subscription.expires_at,
                max_devices=subscription.max_devices,
            )
            if not created:
                # It existed after all, so the create carried no values
                # and the account still holds the old ones. Reporting
                # success here left the panel behind the database.
                panel_user = await self._panel.set_state(
                    subscription.panel_user_id,
                    subscription.expires_at,
                    subscription.max_devices,
                )
```

Остаток метода не меняется.

- [ ] **Step 4: Поправить вызывающих**

`app/services/trial_service.py:82`:

```python
        subscription = await self._subscriptions.create_pending(
            telegram_id,
            expires_at=trial_expiry(self._settings, now),
            origin=SubscriptionOrigin.TRIAL,
            max_devices=DEFAULT_MAX_DEVICES,
        )
```

Импорт: добавить `DEFAULT_MAX_DEVICES` в существующий импорт из
`app.services.subscription_service`.

`app/bot/routers/admin.py:274` — админская выдача не решает про
устройства:

```python
            subscription = await subscriptions.create_pending(
                telegram_id,
                expires_at=now + timedelta(days=days),
                origin=SubscriptionOrigin.ADMIN_GRANT,
                # A grant moves the date, never the device count. More
                # devices are sold, not granted.
                max_devices=DEFAULT_MAX_DEVICES,
            )
```

Импорт `DEFAULT_MAX_DEVICES` из `app.services.subscription_service`
рядом с существующими импортами файла.

`app/services/payment_service.py:320` — `push_expiry` → `push_state`.
Вызов `create_pending` в `_apply_days` правит Task 4; пока передать
`max_devices=tariff.max_devices` тем же keyword-аргументом, чтобы файл
импортировался.

- [ ] **Step 5: Поправить вызовы в тестах**

Во всех местах из списка Files добавить `max_devices=2` в
`create_pending`, а `panel.create_or_get_user` в
`tests/integration/test_reconcile.py:154` вызывать с
`max_devices=2`. Найти всё разом:

```bash
grep -rn "create_pending\|create_or_get_user\|push_expiry\|set_expiry" app tests scripts --include="*.py"
```

Ни одного вхождения `push_expiry` и `set_expiry` остаться не должно.

- [ ] **Step 6: Убедиться, что тесты проходят**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/rillza_test uv run pytest`

Expected: PASS, ноль skipped.

- [ ] **Step 7: Проверить, что новый тест действительно проверяет**

Вернуть в `provision` старое условие (`panel_user.expire_at !=
subscription.expires_at` без второй половины), прогнать
`test_provision_corrects_a_stale_device_limit` — должен упасть. Вернуть
правку.

- [ ] **Step 8: Коммит**

```bash
git add -A
git commit -m "feat: a subscription carries its device count to the panel"
```

---

### Task 4: Покупка проставляет число устройств по последнему платежу

**Files:**
- Modify: `app/repositories/payments.py` (добавить метод в конец класса)
- Modify: `app/services/payment_service.py:246-310`
- Test: `tests/integration/test_payment_flow.py`

**Interfaces:**
- Consumes: `Tariff.max_devices`, `create_pending(max_devices=...)`
- Produces: `PaymentsRepository.newest_applied_created_at(user_id: int, exclude: uuid.UUID) -> datetime | None`

- [ ] **Step 1: Написать падающие тесты**

В `tests/integration/test_payment_flow.py` новым классом в конце файла.
Фикстуры `payments`, `provider`, `panel`, `uow`, `seeded_tariffs` в
файле уже объявлены; идиома «выставить счёт → пометить оплаченным →
финализировать» повторяет то, что делают соседние классы.

```python
class TestDeviceCount:
    async def test_a_purchase_sets_the_device_count(
        self, payments, provider, uow, panel, seeded_tariffs
    ) -> None:
        """A four-device plan reaches both the row and the panel."""
        tariff = await uow.tariffs.get_by_code('m1x4')
        assert tariff is not None
        payment = await payments.create_invoice(USER_ID, tariff, 'yoomoney')
        provider.mark_paid(payment.id)

        await payments.check_and_finalize(payment.id)

        subscription = await uow.subscriptions.get_by_user(USER_ID)
        assert subscription is not None
        assert subscription.max_devices == 4
        assert panel.users[str(USER_ID)].max_devices == 4

    async def test_a_late_payment_does_not_undo_a_newer_count(
        self, payments, provider, uow, seeded_tariffs
    ) -> None:
        """Days add up whatever the order; a device count is assigned.

        A two-device invoice that was replaced by a four-device
        purchase and then arrived late through the sweep must not take
        the buyer back down to two.
        """
        two = await uow.tariffs.get_by_code('m1')
        four = await uow.tariffs.get_by_code('m1x4')
        assert two is not None and four is not None
        old = await payments.create_invoice(USER_ID, two, 'yoomoney')
        new = await payments.create_invoice(USER_ID, four, 'yoomoney')
        provider.mark_paid(old.id)
        provider.mark_paid(new.id)

        # The four-device purchase lands first, the older money later.
        await payments.check_and_finalize(new.id)
        await payments.check_and_finalize(old.id)

        subscription = await uow.subscriptions.get_by_user(USER_ID)
        assert subscription is not None
        assert subscription.max_devices == 4
        # The days still add up: both payments counted.
        assert subscription.expires_at > datetime.now(UTC) + timedelta(
            days=two.duration_days + four.duration_days - 1
        )

    async def test_an_explicit_downgrade_lowers_the_count(
        self, payments, provider, uow, seeded_tariffs
    ) -> None:
        """The guard above must not block a deliberate downgrade."""
        four = await uow.tariffs.get_by_code('m1x4')
        two = await uow.tariffs.get_by_code('m1')
        assert four is not None and two is not None

        first = await payments.create_invoice(USER_ID, four, 'yoomoney')
        provider.mark_paid(first.id)
        await payments.check_and_finalize(first.id)
        second = await payments.create_invoice(USER_ID, two, 'yoomoney')
        provider.mark_paid(second.id)
        await payments.check_and_finalize(second.id)

        subscription = await uow.subscriptions.get_by_user(USER_ID)
        assert subscription is not None
        assert subscription.max_devices == 2
```

**Почему сравнение идёт по `created_at`, а не по `paid_at`.**
`_mark_paid` ставит `paid_at` в момент, когда деньги **заметили**, а не
когда их отправили. У платежа, поднятого свипом через два дня,
`paid_at` окажется позже, чем у покупки, сделанной вчера, — сторож на
`paid_at` пропустил бы ровно тот случай, ради которого написан.

`created_at` — это момент выставления счёта, то есть порядок, в
котором человек нажимал «купить». Во втором тесте счёт на 2 устройства
создан первым, на 4 — вторым, и никакой подкрутки времени тесту не
требуется.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/rillza_test uv run pytest tests/integration/test_payment_flow.py -v -k device`

Expected: FAIL — `assert 2 == 4` в первом тесте.

- [ ] **Step 3: Добавить запрос в репозиторий платежей**

В конец `PaymentsRepository` в `app/repositories/payments.py`:

```python
    async def newest_applied_created_at(
        self, user_id: int, exclude: uuid.UUID
    ) -> datetime | None:
        """When this user's newest *applied* payment was invoiced.

        The device count is an assignment, not an addition, so unlike
        days it cannot be made order-independent: a payment swept up as
        late as seven days after the fact would write its own tariff's
        count over a newer purchase's.

        Ordered by ``created_at``, not ``paid_at``. ``paid_at`` records
        when the money was *noticed*, so late money carries a later
        stamp than the purchase that superseded it — sorting by it
        would invert exactly the case this guards. ``created_at`` is
        when the invoice was raised, which is the order the buyer
        pressed the buttons in.

        Covered by ix_payments_user_id_created_at.
        """
        result = await self._session.execute(
            select(func.max(Payment.created_at)).where(
                Payment.user_id == user_id,
                Payment.days_applied_at.is_not(None),
                Payment.id != exclude,
            )
        )
        return result.scalar_one_or_none()
```

Добавить `func` в импорт SQLAlchemy в шапке файла:

```python
from sqlalchemy import Update, func, select, update
```

- [ ] **Step 4: Проставлять число устройств в `_apply_days`**

В `app/services/payment_service.py`, метод `_apply_days`, заменить блок
после успешного захвата защёлки:

```python
        duration = timedelta(days=tariff.duration_days)
        if subscription is None:
            subscription = await self._subscriptions.create_pending(
                payment.user_id,
                expires_at=now + duration,
                origin=SubscriptionOrigin.PURCHASE,
                max_devices=tariff.max_devices,
                # Must land in the same transaction as the latch below.
                commit=False,
            )
        else:
            base = max(now, subscription.expires_at)
            subscription.expires_at = base + duration
            subscription.status = SubscriptionStatus.ACTIVE
            # A renewal restarts the reminder cycle.
            subscription.notified_stage = None
            # The device count follows the newest purchase, not the
            # last payment to be applied — see the repository method.
            newest = await self._uow.payments.newest_applied_created_at(
                payment.user_id, exclude=payment.id
            )
            if newest is None or payment.created_at >= newest:
                subscription.max_devices = tariff.max_devices
```

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/rillza_test uv run pytest tests/integration/test_payment_flow.py -v`

Expected: PASS.

- [ ] **Step 6: Проверить, что сторож проверяется**

Заменить условие на безусловное `subscription.max_devices =
tariff.max_devices` и прогнать
`test_a_late_payment_does_not_undo_a_newer_device_count` — должен
упасть с `assert 2 == 4`. Вернуть правку.

Затем заменить условие на `if newest is None:` (то есть никогда не
понижать) и прогнать
`test_an_explicit_downgrade_lowers_the_device_count` — должен упасть.
Вернуть правку. Два отката, потому что тесты стерегут сторож с двух
сторон.

- [ ] **Step 7: Коммит**

```bash
git add app/repositories/payments.py app/services/payment_service.py tests/integration/test_payment_flow.py
git commit -m "feat: a purchase sets the device count, a late one cannot undo it"
```

---

### Task 5: Экран выбора числа устройств

**Files:**
- Modify: `app/repositories/tariffs.py:52-59`
- Modify: `app/bot/keyboards.py:20-31`, `:128-145`
- Modify: `app/bot/routers/buy.py:23-56`, `:157-171`
- Modify: `app/bot/texts/ru.py:151-155`
- Modify: `tests/integration/test_repositories.py:131`, `:143`, `:158`
- Modify: `tests/integration/test_buy_flow.py:63-75`
- Test: `tests/integration/test_buy_flow.py`

**Interfaces:**
- Consumes: `Tariff.max_devices`
- Produces:
  - `TariffsRepository.list_active(max_devices: int) -> Sequence[Tariff]`
  - `TariffsRepository.list_device_counts() -> Sequence[int]`
  - `keyboards.DEVICES_PREFIX = 'devices:'`
  - `keyboards.devices(counts: Sequence[int]) -> InlineKeyboardMarkup`
  - `ru.BUY_CHOOSE_DEVICES`, `ru.BUY_CHOOSE_TARIFF` принимает `{devices}`

- [ ] **Step 1: Написать падающие тесты**

В `tests/integration/test_buy_flow.py` заменить
`test_tariff_grid_shows_prices_and_savings` и добавить два теста:

```python
async def test_buy_asks_for_the_device_count_first(
    dispatcher, bot, session
) -> None:
    await dispatcher.feed_update(bot, callback_update(keyboards.BUY))

    buttons = button_texts(session)
    assert any('2 устройств' in b for b in buttons)
    assert any('4 устройств' in b for b in buttons)
    # The durations are one tap away, not on this screen.
    assert not any('месяц' in b for b in buttons)


async def test_tariff_grid_shows_prices_and_savings(
    dispatcher, bot, session
) -> None:
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.DEVICES_PREFIX}2')
    )

    buttons = button_texts(session)
    # The shortest plan is the reference and carries no badge; longer
    # ones advertise how much cheaper their month is against it.
    assert '1 месяц — 200 ₽' in buttons
    assert '3 месяца — 540 ₽ (выгода 10%)' in buttons
    assert '6 месяцев — 960 ₽ (выгода 20%)' in buttons
    assert any(b.startswith('12 месяцев — 1680 ₽ (выгода 3') for b in buttons)


async def test_the_four_device_grid_keeps_the_same_savings_ladder(
    dispatcher, bot, session
) -> None:
    """A flat multiplier is what makes the badges match between sets.

    A mixed list would compare a two-device month against a
    four-device one and print a nonsense discount.
    """
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.DEVICES_PREFIX}4')
    )

    buttons = button_texts(session)
    assert '1 месяц — 320 ₽' in buttons
    assert '3 месяца — 864 ₽ (выгода 10%)' in buttons
    assert '6 месяцев — 1536 ₽ (выгода 20%)' in buttons
    assert not any('200 ₽' in b for b in buttons)


async def test_a_withdrawn_four_device_tariff_cannot_be_bought(
    dispatcher, bot, session, session_factory, seeded_tariffs
) -> None:
    """The device screen must not become a second way in.

    Callback data is client-supplied, so taking m1x4 off sale has to
    stop sales of it on both steps, not just hide the button.
    """
    tariff = next(t for t in seeded_tariffs if t.code == 'm1x4')
    async with UnitOfWork(session_factory) as uow:
        await uow.tariffs.set_active(tariff.id, False)
        await uow.commit()
    session.requests.clear()

    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.DEVICES_PREFIX}4')
    )
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.TARIFF_PREFIX}{tariff.id}')
    )
    await dispatcher.feed_update(
        bot,
        callback_update(f'{keyboards.PROVIDER_PREFIX}{tariff.id}:yoomoney'),
    )

    assert not any('320 ₽' in b for b in button_texts(session))
    async with UnitOfWork(session_factory) as uow:
        assert await uow.payments.list_by_user(USER_ID) == []
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/rillza_test uv run pytest tests/integration/test_buy_flow.py -v`

Expected: FAIL — `AttributeError: module 'app.bot.keyboards' has no
attribute 'DEVICES_PREFIX'`.

- [ ] **Step 3: Поправить репозиторий тарифов**

В `app/repositories/tariffs.py` заменить `list_active` и добавить
`list_device_counts`:

```python
    async def list_active(self, max_devices: int) -> Sequence[Tariff]:
        """What one purchase screen shows, cheapest period first.

        ``max_devices`` is required rather than optional: the savings
        badge is computed against the most expensive month in the list
        shown, so a mixed list would tell a two-device buyer their
        month is 38% cheaper than a four-device one.
        """
        result = await self._session.execute(
            select(Tariff)
            .where(
                Tariff.is_active.is_(True),
                Tariff.is_archived.is_(False),
                Tariff.max_devices == max_devices,
            )
            .order_by(Tariff.sort_order, Tariff.duration_days)
        )
        return result.scalars().all()

    async def list_device_counts(self) -> Sequence[int]:
        """Device counts that have something on sale, ascending.

        Read from the table rather than hardcoded, so pausing every
        four-device plan from the admin screen removes the button, and
        adding a six-device set later is a migration and nothing else.
        """
        result = await self._session.execute(
            select(Tariff.max_devices)
            .where(Tariff.is_active.is_(True), Tariff.is_archived.is_(False))
            .distinct()
            .order_by(Tariff.max_devices)
        )
        return result.scalars().all()
```

- [ ] **Step 4: Добавить клавиатуру и текст**

В `app/bot/keyboards.py` рядом с `TARIFF_PREFIX`:

```python
DEVICES_PREFIX = 'devices:'
```

И новая функция рядом с `tariffs`:

```python
def devices(counts) -> InlineKeyboardMarkup:
    """Step one of a purchase: how many devices."""
    builder = InlineKeyboardBuilder()
    for count in counts:
        builder.button(
            text=f'👥 До {count} устройств',
            callback_data=f'{DEVICES_PREFIX}{count}',
        )
    builder.button(text='↩️ Главное меню', callback_data=MENU)
    builder.adjust(1)
    return builder.as_markup()
```

В `tariffs()` кнопка «Назад» должна вести на выбор устройств. Заменить
последнюю кнопку:

```python
def tariffs(items) -> InlineKeyboardMarkup:
    """One row per duration, with the per-month price to compare against."""
    builder = InlineKeyboardBuilder()
    # The shortest plan sets the reference month; longer ones show how
    # much cheaper their month is against it.
    reference = max((t.monthly_price_kopeks for t in items), default=0)
    for tariff in items:
        monthly = tariff.monthly_price_kopeks
        label = f'{tariff.title_ru} — {tariff.price_kopeks // 100} ₽'
        if reference and monthly < reference:
            discount = round((1 - monthly / reference) * 100)
            if discount:
                label += f' (выгода {discount}%)'
        builder.button(text=label, callback_data=f'{TARIFF_PREFIX}{tariff.id}')
    builder.button(text='↩️ Назад', callback_data=BUY)
    builder.adjust(1)
    return builder.as_markup()
```

В `app/bot/texts/ru.py`:

```python
BUY_CHOOSE_DEVICES = (
    '🛒 <b>Сколько устройств вам нужно?</b>\n\n'
    'Одна подписка работает на всех сразу — телефон, ноутбук, '
    'телевизор. Число можно поменять при следующей покупке.'
)

BUY_CHOOSE_TARIFF = (
    '🛒 <b>Выберите срок подписки</b>\n\n'
    'Тарифы до {devices} устройств. '
    'Чем дольше период, тем дешевле месяц. '
    'Оплата разовая, автосписаний нет.'
)
```

- [ ] **Step 5: Поправить роутер покупки**

В `app/bot/routers/buy.py` заменить `handle_buy` и добавить
`handle_devices`:

```python
async def handle_buy(
    query: CallbackQuery, uow: UnitOfWork, **_: object
) -> None:
    counts = await uow.tariffs.list_device_counts()
    if not counts:
        await _edit(query, ru.BUY_NO_PROVIDERS, keyboards.back_to_menu())
        await query.answer()
        return
    await _edit(query, ru.BUY_CHOOSE_DEVICES, keyboards.devices(counts))
    await query.answer()


async def handle_devices(
    query: CallbackQuery, uow: UnitOfWork, **_: object
) -> None:
    raw = (query.data or '').removeprefix(keyboards.DEVICES_PREFIX)
    count_raw, _, _flag = raw.partition(':')
    try:
        count = int(count_raw)
    except ValueError:
        await query.answer(ru.PAYMENT_UNKNOWN, show_alert=True)
        return

    # Callback data is client-supplied and need not match a button the
    # bot drew, so the number is checked against what is on sale.
    if count not in await uow.tariffs.list_device_counts():
        await query.answer(ru.PAYMENT_UNKNOWN, show_alert=True)
        return

    tariffs = await uow.tariffs.list_active(count)
    await _edit(
        query,
        ru.BUY_CHOOSE_TARIFF.format(devices=count),
        keyboards.tariffs(tariffs),
    )
    await query.answer()
```

И зарегистрировать в `build_router`, **до** `handle_tariff`:

```python
    router.callback_query.register(
        handle_devices, F.data.startswith(keyboards.DEVICES_PREFIX)
    )
```

- [ ] **Step 6: Поправить тесты репозитория**

`tests/integration/test_repositories.py` строки 131, 143, 158 —
`list_active()` теперь требует аргумент. Передать `2` там, где тест
говорит про существующие тарифы, и добавить проверку фильтра:

```python
    async def test_list_active_filters_by_device_count(
        self, uow: UnitOfWork, seeded_tariffs
    ) -> None:
        two = {t.code for t in await uow.tariffs.list_active(2)}
        four = {t.code for t in await uow.tariffs.list_active(4)}

        assert two == {'m1', 'm3', 'm6', 'm12'}
        assert four == {'m1x4', 'm3x4', 'm6x4', 'm12x4'}
        assert await uow.tariffs.list_device_counts() == [2, 4]
```

- [ ] **Step 7: Убедиться, что тесты проходят**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/rillza_test uv run pytest`

Expected: PASS, ноль skipped.

- [ ] **Step 8: Коммит**

```bash
git add -A
git commit -m "feat: pick the device count before the duration"
```

---

### Task 6: Предупреждение о понижении

**Files:**
- Modify: `app/bot/routers/buy.py` (`handle_devices`)
- Modify: `app/bot/keyboards.py` (новая клавиатура)
- Modify: `app/bot/texts/ru.py`
- Test: `tests/integration/test_buy_flow.py`

**Interfaces:**
- Consumes: `keyboards.DEVICES_PREFIX`, `handle_devices` из Task 5
- Produces:
  - `keyboards.devices_downgrade(chosen: int, current: int) -> InlineKeyboardMarkup`
  - callback `devices:{n}:ok` — подтверждённое понижение
  - `ru.BUY_DOWNGRADE_WARNING`

- [ ] **Step 1: Написать падающие тесты**

В `tests/integration/test_buy_flow.py`:

```python
async def test_a_downgrade_is_warned_about_before_the_tariffs(
    dispatcher, bot, session, session_factory, seeded_tariffs, provider
) -> None:
    """Days add up, the device count does not: the remaining paid days
    drop to the new number too, and the buyer is told so."""
    four = next(t for t in seeded_tariffs if t.code == 'm1x4')
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.PROVIDER_PREFIX}{four.id}:yoomoney')
    )
    async with UnitOfWork(session_factory) as uow:
        payment = (await uow.payments.list_by_user(USER_ID))[0]
    provider.mark_paid(payment.id)
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.CHECK_PREFIX}{payment.id}')
    )
    session.requests.clear()

    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.DEVICES_PREFIX}2')
    )

    text = edited_texts(session)[-1]
    assert 'до 4 устройств' in text
    buttons = button_texts(session)
    assert any('Всё равно продолжить' in b for b in buttons)
    # The tariff list is not on this screen yet.
    assert not any('200 ₽' in b for b in buttons)


async def test_a_confirmed_downgrade_reaches_the_tariffs(
    dispatcher, bot, session, session_factory, seeded_tariffs, provider
) -> None:
    four = next(t for t in seeded_tariffs if t.code == 'm1x4')
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.PROVIDER_PREFIX}{four.id}:yoomoney')
    )
    async with UnitOfWork(session_factory) as uow:
        payment = (await uow.payments.list_by_user(USER_ID))[0]
    provider.mark_paid(payment.id)
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.CHECK_PREFIX}{payment.id}')
    )
    session.requests.clear()

    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.DEVICES_PREFIX}2:ok')
    )

    assert '1 месяц — 200 ₽' in button_texts(session)


async def test_an_upgrade_is_not_warned_about(
    dispatcher, bot, session, session_factory, seeded_tariffs, provider
) -> None:
    """Only losing devices needs a warning; buying more never does."""
    two = next(t for t in seeded_tariffs if t.code == 'm1')
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.PROVIDER_PREFIX}{two.id}:yoomoney')
    )
    async with UnitOfWork(session_factory) as uow:
        payment = (await uow.payments.list_by_user(USER_ID))[0]
    provider.mark_paid(payment.id)
    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.CHECK_PREFIX}{payment.id}')
    )
    session.requests.clear()

    await dispatcher.feed_update(
        bot, callback_update(f'{keyboards.DEVICES_PREFIX}4')
    )

    assert '1 месяц — 320 ₽' in button_texts(session)
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/rillza_test uv run pytest tests/integration/test_buy_flow.py -v -k downgrade`

Expected: FAIL — экран сразу показывает тарифы, «Всё равно продолжить»
среди кнопок нет.

- [ ] **Step 3: Добавить текст**

В `app/bot/texts/ru.py`:

```python
BUY_DOWNGRADE_WARNING = (
    '⚠️ <b>Станет меньше устройств</b>\n\n'
    'Сейчас у вас оплачено до {current} устройств до {until} '
    '({left}).\n\n'
    'Если купить тариф до {chosen} устройств, до {chosen} станет и на '
    'оставшийся оплаченный срок: дни складываются, а число устройств '
    'задаёт последняя покупка.'
)
```

- [ ] **Step 4: Добавить клавиатуру**

В `app/bot/keyboards.py` рядом с `devices`:

```python
def devices_downgrade(chosen: int, current: int) -> InlineKeyboardMarkup:
    """Offered before a purchase that lowers the device count."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text='✅ Всё равно продолжить',
        callback_data=f'{DEVICES_PREFIX}{chosen}:ok',
    )
    builder.button(
        text=f'👥 Смотреть тарифы до {current} устройств',
        callback_data=f'{DEVICES_PREFIX}{current}',
    )
    builder.button(text='↩️ Назад', callback_data=BUY)
    builder.adjust(1)
    return builder.as_markup()
```

- [ ] **Step 5: Показывать предупреждение**

В `app/bot/routers/buy.py`, в `handle_devices`, после проверки `count`
и до вызова `list_active`:

```python
    confirmed = _flag == 'ok'
    subscription = await uow.subscriptions.get_by_user(query.from_user.id)
    now = utcnow()
    if (
        not confirmed
        and subscription is not None
        and subscription.is_active_at(now)
        and subscription.max_devices > count
    ):
        # is_active_at, not status == ACTIVE: an expired subscription
        # has nothing left to lose, so there is nothing to warn about.
        await _edit(
            query,
            ru.BUY_DOWNGRADE_WARNING.format(
                current=subscription.max_devices,
                chosen=count,
                until=ru.format_date(subscription.expires_at),
                left=ru.format_left(subscription.expires_at, now),
            ),
            keyboards.devices_downgrade(count, subscription.max_devices),
        )
        await query.answer()
        return
```

Импорт: `from app.services.subscription_service import utcnow` рядом с
существующим импортом `SubscriptionService`.

- [ ] **Step 6: Убедиться, что тесты проходят**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/rillza_test uv run pytest tests/integration/test_buy_flow.py -v`

Expected: PASS.

- [ ] **Step 7: Проверить, что тест проверяет**

Убрать `and subscription.max_devices > count` из условия — упадёт
`test_an_upgrade_is_not_warned_about`. Вернуть. Убрать `not confirmed`
— упадёт `test_a_confirmed_downgrade_reaches_the_tariffs`. Вернуть.

- [ ] **Step 8: Коммит**

```bash
git add -A
git commit -m "feat: warn before a purchase takes devices away"
```

---

### Task 7: Готовый запрос в поддержку

**Files:**
- Modify: `app/services/support_service.py:112-152`
- Modify: `app/bot/texts/support.py`
- Modify: `app/bot/keyboards.py`
- Modify: `app/bot/routers/support.py`
- Modify: `app/bot/routers/buy.py` (кнопка на экране предупреждения)
- Modify: `app/bot/routers/menu.py` (кнопка на экране подписки)
- Test: `tests/integration/test_support_flow.py`

**Interfaces:**
- Consumes: `keyboards.devices_downgrade` из Task 6
- Produces:
  - `SupportService.relay_composed(telegram_id: int, text: str) -> RelayResult`
  - `keyboards.SUPPORT_DEVICES = 'support:devices'`
  - `support.DEVICES_MORE`, `support.DEVICES_BEFORE_DOWNGRADE`

- [ ] **Step 1: Написать падающие тесты**

В `tests/integration/test_support_flow.py` новым классом. В файле уже
есть `ADMIN_ID = 100`, `CUSTOMER_ID = 42`, `DenyingLimiter`, хелперы
`alerts`, `texts_to`, `copies` и импорт `SupportMessage`.

```python
class TestComposedRequest:
    async def test_it_reaches_the_admin_and_routes_back(
        self, dispatcher, bot, session, session_factory
    ) -> None:
        """The canned request has no user message to copy, so it goes
        as a composed send — and a reply to it must still find its way
        back, which means the SupportMessage rows have to be written."""
        await dispatcher.feed_update(
            bot, callback_update(keyboards.SUPPORT_DEVICES)
        )

        assert any(
            'устройств' in text for text in texts_to(session, ADMIN_ID)
        )
        # Nothing was copied: there was no user message to copy.
        assert copies(session) == []

        async with UnitOfWork(session_factory) as uow:
            rows = (
                (
                    await uow.session.execute(
                        select(SupportMessage).where(
                            SupportMessage.user_id == CUSTOMER_ID
                        )
                    )
                )
                .scalars()
                .all()
            )
            # The card and the body: replying to either must route.
            assert len(rows) == 2
            assert {row.direction for row in rows} == {SupportDirection.IN}

    async def test_it_respects_the_support_block(
        self, dispatcher, bot, session, session_factory
    ) -> None:
        async with UnitOfWork(session_factory) as uow:
            await uow.users.upsert(CUSTOMER_ID)
            await uow.users.set_support_blocked(
                CUSTOMER_ID, datetime.now(UTC)
            )
            await uow.commit()
        session.requests.clear()

        await dispatcher.feed_update(
            bot, callback_update(keyboards.SUPPORT_DEVICES)
        )

        assert texts_to(session, ADMIN_ID) == []
        assert any('отключены' in alert for alert in alerts(session))

    async def test_the_downgrade_flavour_names_both_numbers(
        self, dispatcher, bot, session, session_factory, panel,
        settings_with_admin
    ) -> None:
        """From the warning screen nothing has been bought yet, so the
        ticket asks how to proceed rather than "I need more"."""
        async with UnitOfWork(session_factory) as uow:
            await uow.users.upsert(CUSTOMER_ID)
            await uow.commit()
            subscriptions = SubscriptionService(
                uow, panel, settings_with_admin
            )
            subscription = await subscriptions.create_pending(
                CUSTOMER_ID,
                expires_at=datetime.now(UTC) + timedelta(days=59),
                origin=SubscriptionOrigin.PURCHASE,
                max_devices=4,
            )
            await subscriptions.provision(subscription)
        session.requests.clear()

        await dispatcher.feed_update(
            bot, callback_update(f'{keyboards.SUPPORT_DEVICES}:2')
        )

        body = texts_to(session, ADMIN_ID)[-1]
        assert 'до 2 устройств' in body
        assert 'до 4' in body

    async def test_it_is_rate_limited(
        self, settings_with_admin, session_factory, panel, bot, session
    ) -> None:
        """Otherwise the button is a way around the typing limit."""
        dispatcher = build_dispatcher(
            settings_with_admin,
            session_factory,
            panel,
            PaymentRegistry({}),
            storage=MemoryStorage(),
            limiter=DenyingLimiter(),
        )

        await dispatcher.feed_update(
            bot, callback_update(keyboards.SUPPORT_DEVICES)
        )

        assert texts_to(session, ADMIN_ID) == []
        assert any('Слишком много' in alert for alert in alerts(session))
```

В шапке файла не хватает двух импортов: `select` из `sqlalchemy` и
`timedelta` в существующей строке `from datetime import UTC, datetime`.
`SubscriptionService`, `SubscriptionOrigin`, `SupportDirection` и
`SupportMessage` там уже есть.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/rillza_test uv run pytest tests/integration/test_support_flow.py -v -k Composed`

Expected: FAIL — `AttributeError: module 'app.bot.keyboards' has no
attribute 'SUPPORT_DEVICES'`.

- [ ] **Step 3: Добавить путь доставки в сервис**

В `app/services/support_service.py` выделить общую часть и добавить
`relay_composed`. `_deliver_to_admin` разбивается на две:

```python
    async def relay_composed(
        self, telegram_id: int, text: str
    ) -> RelayResult:
        """File a ticket the bot wrote on the user's behalf.

        ``relay_from_user`` copies a message the user actually sent;
        a canned request has none. It travels as a plain send instead,
        which is safe in this direction: forwarding is banned because
        it would name the owner when *answering*, and the admin card
        already names the person writing in.

        Everything else is the same gate: the support block, the rate
        limiter — otherwise the button becomes a way around it — and
        the same SupportMessage rows, so replies route by reply.
        """
        user = await self._uow.users.get(telegram_id)
        if user is not None and user.support_blocked:
            return RelayResult(RelayOutcome.BLOCKED)

        allowed = await self._limiter.allow(
            f'support:{telegram_id}', RATE_LIMIT, RATE_WINDOW_SECONDS
        )
        if not allowed:
            return RelayResult(RelayOutcome.TOO_FAST)

        card = await self._render_card(telegram_id)
        delivered = 0
        for admin_id in self._settings.admin_ids:
            if await self._deliver_composed(admin_id, telegram_id, card, text):
                delivered += 1

        await self._uow.commit()
        if delivered == 0:
            logger.warning(
                'Composed request from {} reached no admin', telegram_id
            )
            return RelayResult(RelayOutcome.UNDELIVERED)
        return RelayResult(RelayOutcome.SENT, delivered)

    async def _deliver_composed(
        self, admin_id: int, telegram_id: int, card: str, text: str
    ) -> bool:
        try:
            header = await self._bot.send_message(
                admin_id,
                card,
                reply_markup=keyboards.support_card(telegram_id),
            )
            body = await self._bot.send_message(admin_id, text)
        except TelegramAPIError as error:
            logger.warning(
                'Composed delivery to admin {} failed: {}', admin_id, error
            )
            return False

        self._remember(telegram_id, admin_id, header.message_id, body.message_id)
        return True

    def _remember(
        self,
        telegram_id: int,
        admin_id: int,
        *message_ids: int,
    ) -> None:
        """Replying to any of these must route back to the same user."""
        for admin_message_id in message_ids:
            self._uow.session.add(
                SupportMessage(
                    user_id=telegram_id,
                    admin_chat_id=admin_id,
                    admin_message_id=admin_message_id,
                    direction=SupportDirection.IN,
                )
            )
```

В `_deliver_to_admin` заменить хвостовой цикл на
`self._remember(telegram_id, admin_id, header.message_id, copy.message_id)`.

- [ ] **Step 4: Добавить тексты**

В `app/bot/texts/support.py`:

```python
DEVICES_MORE = (
    'Мне нужно больше устройств. Сейчас подписка до {current}.'
)
DEVICES_BEFORE_DOWNGRADE = (
    'Хочу купить тариф до {chosen} устройств, но у меня оплачено до '
    '{current} до {until}. Как лучше поступить?'
)
```

В `app/bot/texts/ru.py`:

```python
SUPPORT_REQUEST_SENT = (
    '✅ Отправили вопрос в поддержку. Ответ придёт сюда же.'
)
```

- [ ] **Step 5: Добавить кнопку и обработчик**

В `app/bot/keyboards.py`:

```python
#: Bare — "I want more devices", from the subscription screen.
#: With a ``:{chosen}`` suffix — "I am about to buy fewer", from the
#: downgrade warning. One handler, two questions, because the person
#: asking has not bought anything yet in the second case.
SUPPORT_DEVICES = 'support:devices'
```

В `devices_downgrade` добавить перед «Назад» — с числом, которое
человек собирался купить:

```python
    builder.button(
        text='💬 Спросить в поддержке',
        callback_data=f'{SUPPORT_DEVICES}:{chosen}',
    )
```

В `subscription(url)` добавить перед «Главное меню»:

```python
    builder.row(
        InlineKeyboardButton(
            text='💬 Нужно больше устройств', callback_data=SUPPORT_DEVICES
        )
    )
```

В `app/bot/routers/support.py` — обработчик, который сам собирает
текст из состояния подписки:

```python
async def handle_devices_request(
    query: CallbackQuery,
    uow: UnitOfWork,
    support: SupportService,
    **_: object,
) -> None:
    """A canned ticket about device counts, carrying the numbers.

    Two questions share one handler: "I need more" from the
    subscription screen, and "I am about to buy fewer, how should I do
    it" from the downgrade warning, where nothing has been bought yet.
    The chosen count rides in the callback data of the second.
    """
    chosen = (query.data or '').removeprefix(
        f'{keyboards.SUPPORT_DEVICES}:'
    )
    subscription = await uow.subscriptions.get_by_user(query.from_user.id)
    current = (
        subscription.max_devices
        if subscription is not None
        else DEFAULT_MAX_DEVICES
    )

    if chosen.isdigit() and subscription is not None:
        text = texts.DEVICES_BEFORE_DOWNGRADE.format(
            chosen=int(chosen),
            current=current,
            until=ru.format_date(subscription.expires_at),
        )
    else:
        text = texts.DEVICES_MORE.format(current=current)

    result = await support.relay_composed(query.from_user.id, text)
    if result.outcome is RelayOutcome.SENT:
        await query.answer(ru.SUPPORT_REQUEST_SENT, show_alert=True)
        return
    await query.answer(OUTCOME_TEXTS[result.outcome], show_alert=True)
```

Регистрация в `build_router` — по префиксу, чтобы поймать оба варианта:

```python
    router.callback_query.register(
        handle_devices_request,
        F.data.startswith(keyboards.SUPPORT_DEVICES),
    )
```

`support:block:` и `support:unblock:` не пересекаются с
`support:devices`, а сам `SUPPORT` ловится точным сравнением, так что
порядок регистрации значения не имеет.

Импорты в файле: `UnitOfWork`, `ru`, `DEFAULT_MAX_DEVICES` из
`app.services.subscription_service`.

- [ ] **Step 6: Убедиться, что тесты проходят**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/rillza_test uv run pytest`

Expected: PASS, ноль skipped.

- [ ] **Step 7: Проверить, что тесты проверяют**

Три отката, по одному на тест:

- убрать проверку `support_blocked` → падает
  `test_it_respects_the_support_block`;
- убрать вызов `self._limiter.allow` → падает `test_it_is_rate_limited`;
- убрать `self._remember(...)` из `_deliver_composed` → падает
  `test_it_reaches_the_admin_and_routes_back` на `len(rows) == 2`.

После каждого — вернуть правку.

- [ ] **Step 8: Коммит**

```bash
git add -A
git commit -m "feat: a ready-made support request about device counts"
```

---

### Task 8: Сверка чинит расхождение по числу устройств

**Files:**
- Modify: `app/services/reconcile_service.py:30-41`, `:136-152`
- Test: `tests/integration/test_reconcile.py`

**Interfaces:**
- Consumes: `SubscriptionService.push_state` из Task 3
- Produces: `ReconcileReport.devices_fixed: int`

- [ ] **Step 1: Написать падающие тесты**

В `tests/integration/test_reconcile.py`:

```python
async def test_a_device_limit_drift_is_pushed_back(
    uow, subscriptions, reconciler, panel
) -> None:
    """The panel has no reconciliation of its own; a limit that never
    landed would stay wrong until the customer complained."""
    await make_subscription(uow, subscriptions, max_devices=4)
    # Someone lowered it in the panel by hand.
    panel.users[str(USER_ID)] = panel.users[str(USER_ID)].model_copy(
        update={'max_devices': 2}
    )

    report = await reconciler.run()

    assert report.devices_fixed == 1
    assert panel.users[str(USER_ID)].max_devices == 4


async def test_fixing_a_device_limit_does_not_restart_the_reminders(
    uow, subscriptions, reconciler, panel
) -> None:
    """extend() clears notified_stage, so repairing a limit through it
    would send "остался день" to the same person twice."""
    subscription = await make_subscription(
        uow, subscriptions, days=1, max_devices=4
    )
    subscription.notified_stage = '1d'
    await uow.commit()
    panel.users[str(USER_ID)] = panel.users[str(USER_ID)].model_copy(
        update={'max_devices': 2}
    )

    await reconciler.run()

    refreshed = await uow.subscriptions.get_by_user(USER_ID)
    assert refreshed is not None
    assert refreshed.notified_stage == '1d'
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/rillza_test uv run pytest tests/integration/test_reconcile.py -v -k device`

Expected: FAIL — `AttributeError: 'ReconcileReport' object has no
attribute 'devices_fixed'`.

- [ ] **Step 3: Расширить отчёт**

В `app/services/reconcile_service.py`:

```python
@dataclass(slots=True)
class ReconcileReport:
    checked: int = 0
    created: int = 0
    expiry_fixed: int = 0
    devices_fixed: int = 0
    re_disabled: int = 0
    failed: int = 0
    orphans: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return (
            self.created
            + self.expiry_fixed
            + self.devices_fixed
            + self.re_disabled
        )
```

- [ ] **Step 4: Добавить проверку**

В `_reconcile_one`, сразу после блока `if self._expiry_differs(...)`:

```python
        if panel_user.max_devices != subscription.max_devices:
            # push_state, not extend: extend clears notified_stage, and
            # a limit repair must not make someone receive "остался
            # день" a second time. An expiry drift above already
            # carries the limit with it, so only the limit-only case
            # reaches here.
            await self._subscriptions.push_state(subscription)
            report.devices_fixed += 1
            return
```

Обе проверки стоят после ранних выходов по `revoked`: у отозванной и
истёкшей подписки аккаунт выключен, и лимит на нём ничего не значит.

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/rillza_test uv run pytest tests/integration/test_reconcile.py -v`

Expected: PASS.

- [ ] **Step 6: Проверить, что второй тест проверяет**

Заменить `push_state` на `extend(subscription, subscription.expires_at)`
— упадёт `test_fixing_a_device_limit_does_not_restart_the_reminders`.
Вернуть.

- [ ] **Step 7: Коммит**

```bash
git add app/services/reconcile_service.py tests/integration/test_reconcile.py
git commit -m "fix: reconcile repairs a device limit without resetting reminders"
```

---

### Task 9: Тексты называют число устройств

**Files:**
- Modify: `app/bot/texts/ru.py:14-20` (`START`), `:56-75`
- Modify: `app/bot/routers/menu.py:29-51` (`render_subscription`)
- Modify: `app/bot/texts/admin.py:131-156`
- Test: `tests/integration/test_trial_flow.py`, `tests/integration/test_admin_flow.py`

**Interfaces:**
- Consumes: `Subscription.max_devices`, `Tariff.max_devices`
- Produces: ничего для последующих задач

- [ ] **Step 1: Написать падающие тесты**

В `tests/integration/test_trial_flow.py`, класс с экраном подписки:

```python
    async def test_the_subscription_screen_names_the_device_count(
        self, dispatcher, bot, session
    ) -> None:
        """The owner asked for this: nothing in the bot said how many
        devices a subscription covers."""
        await dispatcher.feed_update(
            bot, callback_update(keyboards.TRIAL_CONFIRM)
        )
        session.requests.clear()

        await dispatcher.feed_update(
            bot, callback_update(keyboards.SUBSCRIPTION)
        )

        text = edited_texts(session)[-1]
        assert 'до 2 устройств' in text
```

В `tests/integration/test_admin_flow.py`, к тестам экрана тарифов:

```python
    async def test_the_tariff_screen_names_the_device_count(
        self, dispatcher, bot, session, seeded_tariffs
    ) -> None:
        four = next(t for t in seeded_tariffs if t.code == 'm1x4')

        await dispatcher.feed_update(
            bot,
            callback_update(
                f'{keyboards.ADMIN_TARIFF_PREFIX}{four.id}',
                user_id=ADMIN_ID,
            ),
        )

        assert any(
            'до 4 устройств' in text for text in edited_texts(session)
        )
```

Имя константы админа (`ADMIN_ID`) взять из файла.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/rillza_test uv run pytest tests/integration/test_trial_flow.py tests/integration/test_admin_flow.py -v -k device`

Expected: FAIL — `assert 'до 2 устройств' in text`.

- [ ] **Step 3: Поправить пользовательские тексты**

В `app/bot/texts/ru.py`:

```python
START = (
    '<b>Rillza VPN</b>\n\n'
    'Быстрый VPN без ограничений по трафику: '
    'YouTube, соцсети и любые сервисы работают как обычно.\n\n'
    'Подписка работает до 2 устройств, а если нужно больше — '
    'есть тарифы до 4.\n\n'
    'Выберите действие в меню ниже.'
)
```

Добавить рядом с `SUBSCRIPTION_TRAFFIC`:

```python
SUBSCRIPTION_DEVICES = '\nУстройств: <b>до {devices}</b>'
```

Формулировка мягкая намеренно: лимит в панели не жёсткий (PLAN.md §7.1),
и обещать блокировку нельзя.

- [ ] **Step 4: Показать строку на экране подписки**

В `app/bot/routers/menu.py`, `render_subscription`, после блока
`SUBSCRIPTION_TRAFFIC`:

```python
    text += ru.SUBSCRIPTION_DEVICES.format(devices=subscription.max_devices)
```

- [ ] **Step 5: Поправить админские экраны**

В `app/bot/texts/admin.py`, `render_tariffs` — добавить лимит в строку:

```python
        lines.append(
            f'{state} <code>{tariff.code}</code> — {tariff.title_ru}, '
            f'{rubles(tariff.price_kopeks)} за {tariff.duration_days} дн. '
            f'({rubles(tariff.monthly_price_kopeks)}/мес), '
            f'до {tariff.max_devices} устройств'
        )
```

И в `render_tariff` — строку перед статусом:

```python
            f'Устройств: до {tariff.max_devices}',
```

- [ ] **Step 6: Убедиться, что тесты проходят**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/rillza_test uv run pytest`

Expected: PASS, ноль skipped.

- [ ] **Step 7: Прогнать линтер и полный набор проверок**

```bash
uv run ruff check . && uv run ruff format --check . && uv run alembic check
```

Expected: всё чисто. `alembic check` требует переменных окружения —
запускать с рабочим `.env` или экспортированным `DATABASE_URL`.

- [ ] **Step 8: Коммит**

```bash
git add -A
git commit -m "feat: the bot says how many devices a subscription covers"
```

---

## Приёмка перед выкаткой

- [ ] Полный прогон с `TEST_DATABASE_URL`: 0 skipped, число passed
      выросло относительно 259 на количество новых тестов
- [ ] `uv run ruff check .` и `uv run ruff format --check .` чисты
- [ ] `alembic upgrade head → check → downgrade -1 → upgrade head` на
      чистой базе без расхождений
- [ ] `grep -rn "push_expiry\|set_expiry" app tests scripts` — пусто
- [ ] `uv run python -m scripts.check_panel` — зелёный (только GET,
      боевую панель не меняет)
- [ ] Прочитать `git diff master` глазами: ни один вызов
      `create_pending` не остался без `max_devices`

## После выкатки на боевом

1. Спросить у владельца разрешение на пересборку — она убивает
   счета, выставленные прямо сейчас.
2. `docker compose up -d --build`, миграция накатится сама.
3. Проверить сид:
   `docker compose exec -T postgres psql -U rillza rillza -c "select code, price_kopeks/100, max_devices from tariffs order by sort_order"`
4. Дождаться сверки и убедиться, что подписка владельца получила явную
   двойку вместо панельного нуля. Эффективный лимит при этом не
   меняется: ноль наследовал ту же двойку у группы.
5. Купить тариф до 4 устройств живыми деньгами, сверить с панелью.

## Что план намеренно не делает

Варианты на 6 и 8 устройств, создание тарифов из админки, общий список
частых вопросов в поддержке, жёсткую блокировку лишних устройств,
отложенное понижение «после окончания оплаченного», перенос цен под
комиссию ЮMoney.
