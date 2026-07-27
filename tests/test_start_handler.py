"""End-to-end wiring check: an update goes in, an API call comes out."""

from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User

from app.main import build_dispatcher
from tests.fake_session import FAKE_TOKEN, RecordingSession


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
def bot(session: RecordingSession) -> Bot:
    return Bot(token=FAKE_TOKEN, session=session)


@pytest.fixture(scope='module')
def dispatcher():
    """Built once: aiogram routers are module singletons and a router
    cannot be attached to a second dispatcher."""
    from app.core.settings import Settings
    from tests.conftest import BASE_ENV

    settings = Settings(_env_file=None, **BASE_ENV)  # type: ignore[arg-type]
    return build_dispatcher(settings, storage=MemoryStorage())


def make_update(text: str, chat_type: str = 'private') -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=42, type=chat_type),
            from_user=User(id=42, is_bot=False, first_name='Тест'),
            text=text,
        ),
    )


async def test_start_replies_in_private_chat(dispatcher, bot, session) -> None:
    await dispatcher.feed_update(bot, make_update('/start'))

    assert len(session.sent_texts()) == 1
    assert 'Rillza VPN' in session.sent_texts()[0]


@pytest.mark.parametrize('chat_type', ['group', 'supergroup', 'channel'])
async def test_start_is_ignored_outside_private_chats(
    dispatcher, bot, session, chat_type
) -> None:
    await dispatcher.feed_update(bot, make_update('/start', chat_type))

    assert session.requests == []


async def test_unknown_message_is_not_answered(
    dispatcher, bot, session
) -> None:
    await dispatcher.feed_update(bot, make_update('просто текст'))

    assert session.requests == []
