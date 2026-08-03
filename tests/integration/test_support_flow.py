"""Anonymous support: relaying, reply routing, anonymity and abuse."""

from datetime import UTC, datetime, timedelta

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
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TelegramUser
from sqlalchemy import select

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


DRAFT_MESSAGE_ID = 777


def admin_message(text: str, message_id: int = DRAFT_MESSAGE_ID) -> Update:
    return Update(
        update_id=3,
        message=Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=ADMIN_ID, type='private'),
            from_user=TelegramUser(
                id=ADMIN_ID, is_bot=False, first_name='Админ'
            ),
            text=text,
        ),
    )


def admin_callback(data: str) -> Update:
    return Update(
        update_id=4,
        callback_query=CallbackQuery(
            id='cb-admin',
            from_user=TelegramUser(
                id=ADMIN_ID, is_bot=False, first_name='Админ'
            ),
            chat_instance='ci',
            data=data,
            message=Message(
                message_id=888,
                date=datetime.now(UTC),
                chat=Chat(id=ADMIN_ID, type='private'),
                text='карточка',
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
                max_devices=2,
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


class TestBroadcastDoesNotSwallowSupportReplies:
    """The admin drafts a broadcast, then a support card arrives and the
    admin answers it. Both used to break: the reply became a new draft
    instead of reaching the user, and it silently replaced what the
    already-visible confirm button would send.
    """

    async def _draft_a_broadcast(self, dispatcher, bot, session) -> str:
        """Draft one, and return its confirm button's callback data."""
        await dispatcher.feed_update(
            bot, admin_callback(keyboards.ADMIN_BROADCAST)
        )
        await dispatcher.feed_update(bot, admin_message('всем привет'))
        for request in reversed(session.requests):
            markup = getattr(request, 'reply_markup', None)
            for row in getattr(markup, 'inline_keyboard', []) or []:
                for button in row:
                    data = button.callback_data or ''
                    if data.startswith(keyboards.ADMIN_BROADCAST_GO_PREFIX):
                        return data
        raise AssertionError('no confirm button was offered')

    async def test_a_reply_after_drafting_reaches_the_user(
        self, dispatcher, bot, session, session_factory
    ) -> None:
        await self._draft_a_broadcast(dispatcher, bot, session)

        await open_support(dispatcher, bot)
        await dispatcher.feed_update(bot, user_message('не подключается'))
        card, _copy = await card_ids(session_factory, CUSTOMER_ID)
        session.requests.clear()

        await dispatcher.feed_update(bot, admin_reply('перезайдите', card))

        # The answer was copied to the user, not swallowed as a draft.
        assert [c.chat_id for c in copies(session)] == [CUSTOMER_ID]

    async def test_confirming_sends_the_draft_that_card_promised(
        self, dispatcher, bot, session, session_factory
    ) -> None:
        async with UnitOfWork(session_factory) as uow:
            for telegram_id in (1, 2, 3):
                await uow.users.upsert(telegram_id)
            await uow.commit()

        go = await self._draft_a_broadcast(dispatcher, bot, session)

        # A private answer to somebody, typed after the draft.
        await open_support(dispatcher, bot)
        await dispatcher.feed_update(bot, user_message('не подключается'))
        card, _copy = await card_ids(session_factory, CUSTOMER_ID)
        await dispatcher.feed_update(bot, admin_reply('секретный ответ', card))
        session.requests.clear()

        await dispatcher.feed_update(bot, admin_callback(go))

        # Whatever went out, it is the draft, and the private answer was
        # not copied to anyone but the person who asked.
        broadcast = [c for c in copies(session)]
        assert all(c.from_chat_id == ADMIN_ID for c in broadcast)
        assert {c.message_id for c in broadcast} == {DRAFT_MESSAGE_ID}


class TestAnAdminCannotFileTicketsWithThemself:
    """The admin's private chat is the support inbox.

    Pressing «Поддержка» once leaves Support.writing in Redis with
    nothing on screen to say so, and from then on every plain message
    the admin types is relayed as a *new ticket from them* — delivered
    to the admins, which is themselves. It reads exactly like "my answer
    went to the first person", and the user who was actually waiting
    gets nothing while the admin believes they answered.
    """

    async def test_a_plain_message_is_not_relayed_as_a_ticket(
        self, dispatcher, bot, session, session_factory
    ) -> None:
        await open_support(dispatcher, bot, user_id=ADMIN_ID)
        session.requests.clear()

        await dispatcher.feed_update(
            bot, user_message('это ответ клиенту', ADMIN_ID)
        )

        # Nothing was copied anywhere: no ticket was created.
        assert copies(session) == []
        async with UnitOfWork(session_factory) as uow:
            rows = await uow.support.find_recipient(ADMIN_ID, 11)
            assert rows is None

    async def test_the_admin_is_told_how_to_answer(
        self, dispatcher, bot, session
    ) -> None:
        await open_support(dispatcher, bot, user_id=ADMIN_ID)
        session.requests.clear()

        await dispatcher.feed_update(
            bot, user_message('это ответ клиенту', ADMIN_ID)
        )

        assert any('Ответить' in text for text in texts_to(session, ADMIN_ID))

    async def test_replying_still_reaches_the_user(
        self, dispatcher, bot, session, session_factory
    ) -> None:
        """The fix must not break the one way that does work."""
        await open_support(dispatcher, bot)
        await dispatcher.feed_update(bot, user_message('помогите'))
        card = (await card_ids(session_factory, CUSTOMER_ID))[0]
        session.requests.clear()

        await dispatcher.feed_update(bot, admin_reply('держите', card))

        assert [c.chat_id for c in copies(session)] == [CUSTOMER_ID]


class TestAnAnswerToSomeoneWhoBlockedTheBot:
    async def test_the_admin_is_told_and_the_user_is_marked(
        self, dispatcher, bot, session, session_factory
    ) -> None:
        """Surfacing this as the generic error handler sent the admin
        hunting for a bug that is not there, and left the user counted
        as reachable in every future broadcast."""
        await open_support(dispatcher, bot)
        await dispatcher.feed_update(bot, user_message('помогите'))
        card = (await card_ids(session_factory, CUSTOMER_ID))[0]
        session.requests.clear()
        session.forbidden_chats = {CUSTOMER_ID}

        await dispatcher.feed_update(bot, admin_reply('держите', card))

        session.forbidden_chats = set()
        assert any(
            'заблокировал бота' in t for t in texts_to(session, ADMIN_ID)
        )
        async with UnitOfWork(session_factory) as uow:
            user = await uow.users.get(CUSTOMER_ID)
            assert user is not None
            assert user.is_bot_blocked is True


async def test_typing_outside_the_flow_gets_an_answer(
    dispatcher, bot, session
) -> None:
    """No reply at all is indistinguishable from the bot being down."""
    await dispatcher.feed_update(bot, user_message('привет'))

    assert any('кнопки' in text for text in texts_to(session, CUSTOMER_ID))


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

        assert any('устройств' in text for text in texts_to(session, ADMIN_ID))
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
            await uow.users.set_support_blocked(CUSTOMER_ID, datetime.now(UTC))
            await uow.commit()
        session.requests.clear()

        await dispatcher.feed_update(
            bot, callback_update(keyboards.SUPPORT_DEVICES)
        )

        assert texts_to(session, ADMIN_ID) == []
        assert any('отключены' in alert for alert in alerts(session))

    async def test_the_more_devices_flavour_names_the_count_with_a_noun(
        self, dispatcher, bot, session
    ) -> None:
        """'Сейчас подписка до 2.' reads like a cut-off sentence — the
        noun after the number is what makes it a device count."""
        await dispatcher.feed_update(
            bot, callback_update(keyboards.SUPPORT_DEVICES)
        )

        body = texts_to(session, ADMIN_ID)[-1]
        assert 'до 2 устройств' in body

    async def test_the_downgrade_flavour_names_both_numbers(
        self,
        dispatcher,
        bot,
        session,
        session_factory,
        panel,
        settings_with_admin,
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

    async def test_the_downgrade_flavour_names_the_current_count_with_a_noun(
        self,
        dispatcher,
        bot,
        session,
        session_factory,
        panel,
        settings_with_admin,
    ) -> None:
        """'оплачено до 4 до 01.10.2026' reads as a single broken
        number — the noun after {current} is what tells it apart from
        the date that follows."""
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
        assert 'до 4 устройств' in body

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
