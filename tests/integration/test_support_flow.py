"""Anonymous support: relaying, reply routing, anonymity and abuse."""

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    CopyMessage,
    ForwardMessage,
    SendMessage,
)
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TelegramUser

from app.bot import keyboards
from app.core.enums import SubscriptionOrigin, SupportDirection
from app.core.settings import Settings
from app.db.models import SupportMessage
from app.integrations.payments import PaymentRegistry
from app.main import build_dispatcher
from app.services.subscription_service import SubscriptionService
from app.services.uow import UnitOfWork
from tests.conftest import BASE_ENV
from tests.fake_panel import FakePanel
from tests.fake_session import FAKE_TOKEN, RecordingSession
from tests.integration.test_trial_flow import callback_update

ADMIN_ID = 100
CUSTOMER_ID = 42


class DenyingLimiter:
    async def allow(self, key: str, limit: int, window: int) -> bool:
        return False


@pytest.fixture
def panel() -> FakePanel:
    return FakePanel()


@pytest.fixture
def settings_with_admin() -> Settings:
    return Settings(_env_file=None, admin_ids=str(ADMIN_ID), **BASE_ENV)  # type: ignore[arg-type]


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
def bot(session: RecordingSession) -> Bot:
    return Bot(token=FAKE_TOKEN, session=session)


@pytest_asyncio.fixture
async def dispatcher(settings_with_admin, session_factory, panel):
    return build_dispatcher(
        settings_with_admin,
        session_factory,
        panel,
        PaymentRegistry({}),
        storage=MemoryStorage(),
    )


def user_message(
    text: str, user_id: int = CUSTOMER_ID, first_name: str = 'Иван'
) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=11,
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type='private'),
            from_user=TelegramUser(
                id=user_id,
                is_bot=False,
                first_name=first_name,
                username='ivan',
            ),
            text=text,
        ),
    )


def admin_reply(text: str, reply_to_message_id: int) -> Update:
    admin = TelegramUser(id=ADMIN_ID, is_bot=False, first_name='Админ')
    chat = Chat(id=ADMIN_ID, type='private')
    return Update(
        update_id=2,
        message=Message(
            message_id=99,
            date=datetime.now(UTC),
            chat=chat,
            from_user=admin,
            text=text,
            reply_to_message=Message(
                message_id=reply_to_message_id,
                date=datetime.now(UTC),
                chat=chat,
                text='карточка обращения',
            ),
        ),
    )


def copies(session: RecordingSession) -> list[CopyMessage]:
    return [r for r in session.requests if isinstance(r, CopyMessage)]


def texts_to(session: RecordingSession, chat_id: int) -> list[str]:
    return [
        r.text
        for r in session.requests
        if isinstance(r, SendMessage) and r.chat_id == chat_id
    ]


def alerts(session: RecordingSession) -> list[str]:
    return [
        r.text or ''
        for r in session.requests
        if isinstance(r, AnswerCallbackQuery)
    ]


async def open_support(dispatcher, bot, user_id: int = CUSTOMER_ID) -> None:
    await dispatcher.feed_update(
        bot, callback_update(keyboards.SUPPORT, user_id=user_id)
    )


async def card_ids(session_factory, user_id: int) -> list[int]:
    """Admin-side message ids that route back to this user."""
    from sqlalchemy import select

    async with UnitOfWork(session_factory) as uow:
        rows = await uow.session.execute(
            select(SupportMessage.admin_message_id)
            .where(
                SupportMessage.user_id == user_id,
                SupportMessage.direction == SupportDirection.IN,
            )
            .order_by(SupportMessage.id)
        )
        return list(rows.scalars().all())


class TestUserSide:
    async def test_message_reaches_the_admin_with_a_card(
        self, dispatcher, bot, session, session_factory
    ) -> None:
        await open_support(dispatcher, bot)
        session.requests.clear()

        await dispatcher.feed_update(bot, user_message('не подключается'))

        # A context card, then a copy of what the user actually sent.
        admin_texts = texts_to(session, ADMIN_ID)
        assert any('Обращение' in text for text in admin_texts)
        assert any('ivan' in text for text in admin_texts)
        assert [c.chat_id for c in copies(session)] == [ADMIN_ID]
        assert any(
            'Сообщение отправлено' in text
            for text in texts_to(session, CUSTOMER_ID)
        )

        ids = await card_ids(session_factory, CUSTOMER_ID)
        assert len(ids) == 2
        async with UnitOfWork(session_factory) as uow:
            recipient = await uow.support.find_recipient(ADMIN_ID, ids[0])
            assert recipient == CUSTOMER_ID

    async def test_nothing_is_ever_forwarded(
        self, dispatcher, bot, session
    ) -> None:
        """A forward would name the sender — the one thing to avoid."""
        await open_support(dispatcher, bot)
        await dispatcher.feed_update(bot, user_message('привет'))

        assert not any(isinstance(r, ForwardMessage) for r in session.requests)

    async def test_messages_keep_flowing_until_the_user_leaves(
        self, dispatcher, bot, session
    ) -> None:
        await open_support(dispatcher, bot)
        await dispatcher.feed_update(bot, user_message('первое'))
        await dispatcher.feed_update(bot, user_message('второе'))

        assert len(copies(session)) == 2

        await dispatcher.feed_update(bot, callback_update(keyboards.MENU))
        session.requests.clear()
        await dispatcher.feed_update(bot, user_message('уже не в поддержке'))

        assert copies(session) == []

    async def test_rate_limit_refuses_politely(
        self, settings_with_admin, session_factory, panel, bot, session
    ) -> None:
        dispatcher = build_dispatcher(
            settings_with_admin,
            session_factory,
            panel,
            PaymentRegistry({}),
            storage=MemoryStorage(),
            limiter=DenyingLimiter(),
        )
        await open_support(dispatcher, bot)
        session.requests.clear()

        await dispatcher.feed_update(bot, user_message('спам'))

        assert copies(session) == []
        assert any(
            'Слишком много' in text for text in texts_to(session, CUSTOMER_ID)
        )

    async def test_blocked_user_is_turned_away(
        self, dispatcher, bot, session, session_factory
    ) -> None:
        await open_support(dispatcher, bot)
        async with UnitOfWork(session_factory) as uow:
            await uow.users.set_support_blocked(CUSTOMER_ID, datetime.now(UTC))
            await uow.commit()
        session.requests.clear()

        await dispatcher.feed_update(bot, user_message('всё равно пишу'))

        assert copies(session) == []
        assert any(
            'отключены' in text for text in texts_to(session, CUSTOMER_ID)
        )


class TestAdminSide:
    async def test_reply_reaches_the_user_anonymously(
        self, dispatcher, bot, session, session_factory
    ) -> None:
        await open_support(dispatcher, bot)
        await dispatcher.feed_update(bot, user_message('помогите'))
        card, _copy = await card_ids(session_factory, CUSTOMER_ID)
        session.requests.clear()

        await dispatcher.feed_update(bot, admin_reply('сейчас поможем', card))

        delivered = [c for c in copies(session) if c.chat_id == CUSTOMER_ID]
        assert len(delivered) == 1
        # Copied from the admin's chat, so the user sees only the bot.
        assert delivered[0].from_chat_id == ADMIN_ID
        assert not any(isinstance(r, ForwardMessage) for r in session.requests)
        assert any(
            'Ответ поддержки' in text
            for text in texts_to(session, CUSTOMER_ID)
        )

    async def test_reply_to_the_copy_also_routes(
        self, dispatcher, bot, session, session_factory
    ) -> None:
        """Either the card or the copy is a valid thing to reply to."""
        await open_support(dispatcher, bot)
        await dispatcher.feed_update(bot, user_message('вопрос'))
        _card, copy_id = await card_ids(session_factory, CUSTOMER_ID)
        session.requests.clear()

        await dispatcher.feed_update(bot, admin_reply('ответ', copy_id))

        assert [c.chat_id for c in copies(session)] == [CUSTOMER_ID]

    async def test_two_users_do_not_get_mixed_up(
        self, dispatcher, bot, session, session_factory
    ) -> None:
        """The old bot's global "current dialog" sent answers to the
        wrong person whenever two people wrote at once."""
        await open_support(dispatcher, bot)
        await dispatcher.feed_update(bot, user_message('первый', CUSTOMER_ID))

        other = 777
        await open_support(dispatcher, bot, user_id=other)
        await dispatcher.feed_update(bot, user_message('второй', other))
        first_card = (await card_ids(session_factory, CUSTOMER_ID))[0]
        session.requests.clear()

        await dispatcher.feed_update(
            bot, admin_reply('только первому', first_card)
        )

        assert [c.chat_id for c in copies(session)] == [CUSTOMER_ID]

    async def test_reply_without_a_thread_is_reported(
        self, dispatcher, bot, session
    ) -> None:
        await dispatcher.feed_update(bot, admin_reply('в пустоту', 4242))

        assert copies(session) == []
        assert any(
            'Не понимаю, кому' in text for text in texts_to(session, ADMIN_ID)
        )

    async def test_block_button_stops_further_messages(
        self, dispatcher, bot, session, session_factory
    ) -> None:
        await open_support(dispatcher, bot)
        await dispatcher.feed_update(bot, user_message('привет'))

        await dispatcher.feed_update(
            bot,
            callback_update(
                f'{keyboards.SUPPORT_BLOCK_PREFIX}{CUSTOMER_ID}',
                user_id=ADMIN_ID,
            ),
        )

        async with UnitOfWork(session_factory) as uow:
            user = await uow.users.get(CUSTOMER_ID)
            assert user is not None
            assert user.support_blocked is True
        assert any('больше не может' in alert for alert in alerts(session))

    async def test_unblock_restores_access(
        self, dispatcher, bot, session, session_factory
    ) -> None:
        async with UnitOfWork(session_factory) as uow:
            await uow.users.upsert(CUSTOMER_ID)
            await uow.users.set_support_blocked(CUSTOMER_ID, datetime.now(UTC))
            await uow.commit()

        await dispatcher.feed_update(
            bot,
            callback_update(
                f'{keyboards.SUPPORT_UNBLOCK_PREFIX}{CUSTOMER_ID}',
                user_id=ADMIN_ID,
            ),
        )

        async with UnitOfWork(session_factory) as uow:
            user = await uow.users.get(CUSTOMER_ID)
            assert user is not None
            assert user.support_blocked is False


class TestThreadHistory:
    async def test_both_directions_are_recorded(
        self, dispatcher, bot, session_factory
    ) -> None:
        await open_support(dispatcher, bot)
        await dispatcher.feed_update(bot, user_message('вопрос'))
        card = (await card_ids(session_factory, CUSTOMER_ID))[0]
        await dispatcher.feed_update(bot, admin_reply('ответ', card))

        async with UnitOfWork(session_factory) as uow:
            from sqlalchemy import select

            rows = (
                (await uow.session.execute(select(SupportMessage)))
                .scalars()
                .all()
            )

        directions = [row.direction for row in rows]
        assert directions.count(SupportDirection.IN) == 2
        assert directions.count(SupportDirection.OUT) == 1

    async def test_card_shows_the_subscription_state(
        self,
        dispatcher,
        bot,
        session,
        session_factory,
        panel,
        settings_with_admin,
    ) -> None:
        async with UnitOfWork(session_factory) as uow:
            await uow.users.upsert(CUSTOMER_ID, username='ivan')
            await uow.commit()
            subscriptions = SubscriptionService(
                uow, panel, settings_with_admin
            )
            subscription = await subscriptions.create_pending(
                CUSTOMER_ID,
                expires_at=datetime.now(UTC).replace(microsecond=0),
                origin=SubscriptionOrigin.PURCHASE,
            )
            await subscriptions.provision(subscription)

        await open_support(dispatcher, bot)
        session.requests.clear()
        await dispatcher.feed_update(bot, user_message('не работает'))

        card = texts_to(session, ADMIN_ID)[0]
        assert 'Подписка: active' in card


async def test_display_names_cannot_inject_html(
    dispatcher, bot, session, session_factory
) -> None:
    """A name is attacker-controlled and lands in an HTML message."""
    hostile = '<b>x</b><a href="http://evil">tap</a>'

    await open_support(dispatcher, bot)
    session.requests.clear()
    await dispatcher.feed_update(
        bot, user_message('привет', first_name=hostile)
    )

    card = texts_to(session, ADMIN_ID)[0]
    assert '<a href' not in card
    assert '&lt;b&gt;' in card
