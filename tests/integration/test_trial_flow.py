"""Trial issuance and the subscription screen, end to end."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TelegramUser

from app.bot import keyboards
from app.core.enums import SubscriptionStatus
from app.core.settings import Settings
from app.main import build_dispatcher
from app.services.subscription_service import SubscriptionService
from app.services.trial_service import TrialOutcome, TrialService
from app.services.uow import UnitOfWork
from tests.conftest import BASE_ENV
from tests.fake_panel import FakePanel
from tests.fake_session import FAKE_TOKEN, RecordingSession

USER_ID = 42


@pytest.fixture
def panel() -> FakePanel:
    return FakePanel()


@pytest.fixture
def app_settings() -> Settings:
    return Settings(_env_file=None, **BASE_ENV)  # type: ignore[arg-type]


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
def bot(session: RecordingSession) -> Bot:
    return Bot(token=FAKE_TOKEN, session=session)


@pytest_asyncio.fixture
async def dispatcher(app_settings, session_factory, panel):
    return build_dispatcher(
        app_settings, session_factory, panel, storage=MemoryStorage()
    )


def message_update(text: str, chat_type: str = 'private') -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=USER_ID, type=chat_type),
            from_user=TelegramUser(
                id=USER_ID, is_bot=False, first_name='Тест'
            ),
            text=text,
        ),
    )


def callback_update(data: str, user_id: int = USER_ID) -> Update:
    user = TelegramUser(id=user_id, is_bot=False, first_name='Тест')
    return Update(
        update_id=2,
        callback_query=CallbackQuery(
            id='cb1',
            from_user=user,
            chat_instance='ci',
            data=data,
            message=Message(
                message_id=7,
                date=datetime.now(UTC),
                chat=Chat(id=user_id, type='private'),
                from_user=user,
                text='предыдущий экран',
            ),
        ),
    )


def edited_texts(session: RecordingSession) -> list[str]:
    return [
        request.text
        for request in session.requests
        if isinstance(request, EditMessageText)
    ]


def button_texts(session: RecordingSession) -> list[str]:
    markup = None
    for request in session.requests:
        if isinstance(request, SendMessage | EditMessageText):
            markup = request.reply_markup
    if markup is None:
        return []
    return [button.text for row in markup.inline_keyboard for button in row]


class TestMenu:
    async def test_start_offers_the_trial_to_a_new_user(
        self, dispatcher, bot, session
    ) -> None:
        await dispatcher.feed_update(bot, message_update('/start'))

        assert any('Rillza VPN' in text for text in session.sent_texts())
        assert any('3 дня бесплатно' in text for text in button_texts(session))

    async def test_group_chats_are_ignored(
        self, dispatcher, bot, session
    ) -> None:
        await dispatcher.feed_update(bot, message_update('/start', 'group'))

        assert session.requests == []


class TestTrialFlow:
    async def test_grant_creates_panel_account_and_shows_link(
        self, dispatcher, bot, session, panel, session_factory
    ) -> None:
        await dispatcher.feed_update(
            bot, callback_update(keyboards.TRIAL_CONFIRM)
        )

        # The panel account exists, enabled, with our absolute expiry.
        assert str(USER_ID) in panel.users
        panel_user = panel.users[str(USER_ID)]
        assert panel_user.enabled is True
        assert panel_user.expire_at is not None

        async with UnitOfWork(session_factory) as uow:
            subscription = await uow.subscriptions.get_by_user(USER_ID)
            assert subscription is not None
            assert subscription.status == SubscriptionStatus.ACTIVE
            assert subscription.subscription_token == (
                panel_user.subscription_token
            )
            # Three days, as configured.
            days = (subscription.expires_at - datetime.now(UTC)).days
            assert days == 2  # 3 days minus a few seconds

        assert any('Доступ выдан' in text for text in edited_texts(session))
        assert any(
            'Открыть подписку' in text for text in button_texts(session)
        )

    async def test_second_tap_does_not_grant_a_second_trial(
        self, dispatcher, bot, session, panel, session_factory
    ) -> None:
        await dispatcher.feed_update(
            bot, callback_update(keyboards.TRIAL_CONFIRM)
        )
        await dispatcher.feed_update(
            bot, callback_update(keyboards.TRIAL_CONFIRM)
        )

        async with UnitOfWork(session_factory) as uow:
            subscription = await uow.subscriptions.get_by_user(USER_ID)
            assert subscription is not None
            first_expiry = subscription.expires_at

        # No extra days, and only one panel account.
        assert len(panel.users) == 1
        assert panel.users[str(USER_ID)].expire_at == first_expiry
        assert any(
            'уже есть активная подписка' in text
            for text in edited_texts(session)
        )

    async def test_panel_outage_keeps_the_record_and_recovers(
        self, app_settings, session_factory, panel
    ) -> None:
        """The trial is recorded even if provisioning fails.

        The latch is spent, so nobody gets two trials; the pending row is
        finished on the next attempt instead.
        """
        panel.offline = True
        async with UnitOfWork(session_factory) as uow:
            await uow.users.upsert(USER_ID)
            await uow.commit()
            subscriptions = SubscriptionService(uow, panel, app_settings)
            trials = TrialService(uow, subscriptions, app_settings)

            failed = await trials.grant(USER_ID)

            assert failed.outcome is TrialOutcome.PENDING_PROVISIONING
            subscription = await uow.subscriptions.get_by_user(USER_ID)
            assert subscription is not None
            assert subscription.status == SubscriptionStatus.PENDING
            assert subscription.subscription_token is None

            panel.offline = False
            recovered = await trials.grant(USER_ID)

            assert recovered.outcome is TrialOutcome.GRANTED
            assert recovered.subscription is not None
            assert recovered.subscription.subscription_token is not None
            assert recovered.subscription.expires_at == subscription.expires_at

    async def test_trial_is_not_offered_twice(
        self, app_settings, session_factory, panel
    ) -> None:
        async with UnitOfWork(session_factory) as uow:
            await uow.users.upsert(USER_ID)
            await uow.commit()
            subscriptions = SubscriptionService(uow, panel, app_settings)
            trials = TrialService(uow, subscriptions, app_settings)

            assert await trials.is_available(USER_ID) is True
            await trials.grant(USER_ID)
            assert await trials.is_available(USER_ID) is False


class TestSubscriptionScreen:
    async def test_screen_without_subscription(
        self, dispatcher, bot, session
    ) -> None:
        await dispatcher.feed_update(
            bot, callback_update(keyboards.SUBSCRIPTION)
        )

        assert any('пока нет подписки' in t for t in edited_texts(session))

    async def test_screen_shows_dates_and_traffic(
        self, dispatcher, bot, session, panel
    ) -> None:
        panel.info_traffic = panel.info_traffic.model_copy(
            update={'used': 3 * 1024**3}
        )
        await dispatcher.feed_update(
            bot, callback_update(keyboards.TRIAL_CONFIRM)
        )
        session.requests.clear()

        await dispatcher.feed_update(
            bot, callback_update(keyboards.SUBSCRIPTION)
        )

        text = edited_texts(session)[-1]
        assert 'Активна до' in text
        assert '3.0 ГБ' in text
        assert 'трафик не ограничен' in text

    async def test_screen_survives_a_panel_outage(
        self, dispatcher, bot, session, panel
    ) -> None:
        """Our own dates are what the user came to see."""
        await dispatcher.feed_update(
            bot, callback_update(keyboards.TRIAL_CONFIRM)
        )
        session.requests.clear()
        panel.offline = True

        await dispatcher.feed_update(
            bot, callback_update(keyboards.SUBSCRIPTION)
        )

        text = edited_texts(session)[-1]
        assert 'Активна до' in text
        assert any(
            'Открыть подписку' in button for button in button_texts(session)
        )

    async def test_expired_subscription_reads_as_expired(
        self, app_settings, session_factory, panel
    ) -> None:
        async with UnitOfWork(session_factory) as uow:
            await uow.users.upsert(USER_ID)
            await uow.commit()
            subscriptions = SubscriptionService(uow, panel, app_settings)
            trials = TrialService(uow, subscriptions, app_settings)
            result = await trials.grant(USER_ID)
            assert result.subscription is not None
            result.subscription.expires_at = datetime.now(UTC) - timedelta(
                days=1
            )
            await uow.commit()

            from app.bot.routers.menu import render_subscription

            view = await subscriptions.describe(USER_ID)
            assert view is not None
            text = render_subscription(view, datetime.now(UTC))

        assert 'закончился' in text


class TestGuide:
    async def test_guide_lists_every_platform(
        self, dispatcher, bot, session
    ) -> None:
        await dispatcher.feed_update(
            bot, callback_update(keyboards.TRIAL_CONFIRM)
        )
        session.requests.clear()

        await dispatcher.feed_update(bot, callback_update(keyboards.GUIDE))

        buttons = button_texts(session)
        assert any('iPhone' in b for b in buttons)
        assert any('Android' in b for b in buttons)
        assert any('Windows' in b for b in buttons)
        text = edited_texts(session)[-1]
        assert 'три шага' in text
        assert 'HAPP' in text

    async def test_guide_without_subscription_points_at_the_trial(
        self, dispatcher, bot, session
    ) -> None:
        await dispatcher.feed_update(bot, callback_update(keyboards.GUIDE))

        assert any(
            'появится вместе с подпиской' in t for t in edited_texts(session)
        )


class TestCallbacksAlwaysAnswer:
    @pytest.mark.parametrize(
        'data',
        [
            keyboards.MENU,
            keyboards.TRIAL_OFFER,
            keyboards.TRIAL_CONFIRM,
            keyboards.SUBSCRIPTION,
            keyboards.GUIDE,
            keyboards.SUPPORT,
        ],
    )
    async def test_spinner_is_cleared(
        self, dispatcher, bot, session, data
    ) -> None:
        """An unanswered callback leaves the button spinning forever."""
        await dispatcher.feed_update(bot, callback_update(data))

        assert any(
            isinstance(request, AnswerCallbackQuery)
            for request in session.requests
        )


async def test_pending_subscription_does_not_read_as_expired(
    app_settings, session_factory, panel
) -> None:
    """Regression: a subscription awaiting the panel showed «истекла»
    next to a future date."""
    from app.bot.routers.menu import render_subscription
    from app.core.enums import SubscriptionOrigin

    panel.offline = True
    async with UnitOfWork(session_factory) as uow:
        await uow.users.upsert(USER_ID)
        await uow.commit()
        subscriptions = SubscriptionService(uow, panel, app_settings)
        await subscriptions.create_pending(
            USER_ID,
            expires_at=datetime.now(UTC) + timedelta(days=30),
            origin=SubscriptionOrigin.PURCHASE,
        )

        view = await subscriptions.describe(USER_ID)
        assert view is not None
        text = render_subscription(view, datetime.now(UTC))

    assert 'Выдаём доступ' in text
    assert 'закончился' not in text
