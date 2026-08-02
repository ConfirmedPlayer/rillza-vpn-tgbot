"""The dispatcher wiring against a real database."""

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.fsm.storage.memory import MemoryStorage

from app.core.settings import Settings
from app.main import build_dispatcher
from app.services.uow import UnitOfWork
from tests.conftest import BASE_ENV
from tests.fake_panel import FakePanel
from tests.fake_session import FAKE_TOKEN, RecordingSession
from tests.integration.test_trial_flow import message_update


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
def bot(session: RecordingSession) -> Bot:
    return Bot(token=FAKE_TOKEN, session=session)


@pytest_asyncio.fixture
async def wired_dispatcher(session_factory):
    settings = Settings(_env_file=None, **BASE_ENV)  # type: ignore[arg-type]
    return build_dispatcher(
        settings, session_factory, FakePanel(), storage=MemoryStorage()
    )


async def test_start_records_the_user(
    wired_dispatcher, bot, session, session_factory
) -> None:
    await wired_dispatcher.feed_update(bot, message_update('/start'))

    assert len(session.sent_texts()) == 1
    async with UnitOfWork(session_factory) as uow:
        user = await uow.users.get(42)
        assert user is not None
        assert user.first_name == 'Тест'


async def test_repeated_updates_do_not_duplicate_or_reset(
    wired_dispatcher, bot, session_factory
) -> None:
    """A profile refresh on every update must not clear the trial latch."""
    await wired_dispatcher.feed_update(bot, message_update('/start'))
    async with UnitOfWork(session_factory) as uow:
        await uow.users.mark_trial_used(42, datetime.now(UTC))
        await uow.commit()

    await wired_dispatcher.feed_update(bot, message_update('/start'))

    async with UnitOfWork(session_factory) as uow:
        assert await uow.users.count() == 1
        user = await uow.users.get(42)
        assert user is not None and user.trial_used is True


async def test_group_updates_are_dropped_before_the_database(
    wired_dispatcher, bot, session, session_factory
) -> None:
    await wired_dispatcher.feed_update(bot, message_update('/start', 'group'))

    assert session.requests == []
    async with UnitOfWork(session_factory) as uow:
        # The upsert middleware still runs for any update it can attribute
        # to a user; what must not happen is a reply.
        assert await uow.subscriptions.get_by_user(42) is None


async def test_writing_again_clears_the_blocked_flag(
    wired_dispatcher, bot, session_factory
) -> None:
    """The flag is set when Telegram refuses a send and was never
    cleared, so anyone who blocked the bot once and came back stayed
    out of every broadcast for good. A message from them is proof they
    are reachable again.
    """
    await wired_dispatcher.feed_update(bot, message_update('/start'))
    async with UnitOfWork(session_factory) as uow:
        await uow.users.set_bot_blocked(42, True)
        await uow.commit()

    await wired_dispatcher.feed_update(bot, message_update('/start'))

    async with UnitOfWork(session_factory) as uow:
        user = await uow.users.get(42)
        assert user is not None
        assert user.is_bot_blocked is False
        targets = await uow.users.iter_broadcast_targets()
        assert 42 in [u.id for u in targets]
