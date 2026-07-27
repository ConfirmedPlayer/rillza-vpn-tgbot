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

SEED_TARIFFS = (
    ('m1', '1 месяц', 30, 20_000, 1),
    ('m3', '3 месяца', 90, 54_000, 2),
    ('m6', '6 месяцев', 180, 96_000, 3),
    ('m12', '12 месяцев', 365, 168_000, 4),
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
    """The four tariffs the seed migration installs."""
    tariffs = [
        Tariff(
            code=code,
            title_ru=title,
            duration_days=days,
            price_kopeks=price,
            sort_order=order,
        )
        for code, title, days, price, order in SEED_TARIFFS
    ]
    uow.session.add_all(tariffs)
    await uow.commit()
    return tariffs
