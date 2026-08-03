"""Integration tests run against a real PostgreSQL.

SQLite is not an option here: it ignores ``FOR UPDATE``, so the locking
that keeps a double "проверить оплату" tap from provisioning twice would
pass vacuously (PLAN.md §1).

Point ``TEST_DATABASE_URL`` at a scratch database; the whole module is
skipped when it is unset, so a checkout without Postgres still runs the
unit suite.
"""

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)

from app.db.base import Base
from app.db.engine import build_session_factory
from app.db.models import Tariff
from app.services.uow import UnitOfWork

TEST_DATABASE_URL = os.getenv('TEST_DATABASE_URL')

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason='TEST_DATABASE_URL is not set'
)

#: What a database ends up with after every tariff migration has run.
#: The two-device rows stay first: several tests take seeded_tariffs[0]
#: and [1] positionally, meaning m1 and m3.
SEED_TARIFFS = (
    # code, title, days, price in kopeks, devices, order
    ('m1', '1 месяц · до 2 устройств', 30, 10_000, 2, 1),
    ('m3', '3 месяца · до 2 устройств', 90, 27_000, 2, 2),
    ('m6', '6 месяцев · до 2 устройств', 180, 48_000, 2, 3),
    ('m12', '12 месяцев · до 2 устройств', 365, 84_000, 2, 4),
    ('m1x3', '1 месяц · до 3 устройств', 30, 15_000, 3, 5),
    ('m3x3', '3 месяца · до 3 устройств', 90, 40_500, 3, 6),
    ('m6x3', '6 месяцев · до 3 устройств', 180, 72_000, 3, 7),
    ('m12x3', '12 месяцев · до 3 устройств', 365, 126_000, 3, 8),
    ('m1x4', '1 месяц · до 4 устройств', 30, 20_000, 4, 9),
    ('m3x4', '3 месяца · до 4 устройств', 90, 54_000, 4, 10),
    ('m6x4', '6 месяцев · до 4 устройств', 180, 96_000, 4, 11),
    ('m12x4', '12 месяцев · до 4 устройств', 365, 168_000, 4, 12),
    ('m1x6', '1 месяц · до 6 устройств', 30, 30_000, 6, 13),
    ('m3x6', '3 месяца · до 6 устройств', 90, 81_000, 6, 14),
    ('m6x6', '6 месяцев · до 6 устройств', 180, 144_000, 6, 15),
    ('m12x6', '12 месяцев · до 6 устройств', 365, 252_000, 6, 16),
    ('m1x8', '1 месяц · до 8 устройств', 30, 40_000, 8, 17),
    ('m3x8', '3 месяца · до 8 устройств', 90, 108_000, 8, 18),
    ('m6x8', '6 месяцев · до 8 устройств', 180, 192_000, 8, 19),
    ('m12x8', '12 месяцев · до 8 устройств', 365, 336_000, 8, 20),
)


@pytest.fixture(scope='session')
def database_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip('TEST_DATABASE_URL is not set')
    return TEST_DATABASE_URL


@pytest_asyncio.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """A fresh schema per test: created from the models, dropped after."""
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine):
    return build_session_factory(engine)


@pytest_asyncio.fixture
async def uow(session_factory) -> AsyncIterator[UnitOfWork]:
    async with UnitOfWork(session_factory) as unit:
        yield unit


@pytest_asyncio.fixture
async def session(uow: UnitOfWork) -> AsyncSession:
    return uow.session


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
