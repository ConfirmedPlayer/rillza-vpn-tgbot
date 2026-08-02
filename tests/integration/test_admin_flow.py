"""Admin panel, broadcasts and expiry reminders."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, SendMessage

from app.bot import keyboards
from app.core.enums import (
    PaymentStatus,
    SubscriptionOrigin,
    SubscriptionStatus,
)
from app.core.settings import Settings
from app.integrations.payments import PaymentRegistry
from app.main import build_dispatcher
from app.services.broadcast_service import BroadcastService
from app.services.notification_service import NotificationService
from app.services.subscription_service import SubscriptionService
from app.services.uow import UnitOfWork
from tests.conftest import BASE_ENV
from tests.fake_panel import FakePanel
from tests.fake_payments import FakeProvider
from tests.fake_session import FAKE_TOKEN, RecordingSession
from tests.integration.test_trial_flow import (
    callback_update,
    edited_texts,
    message_update,
)

ADMIN_ID = 42  # matches the id used by the shared update helpers
CUSTOMER_ID = 777


@pytest.fixture
def panel() -> FakePanel:
    return FakePanel()


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def admin_settings() -> Settings:
    return Settings(_env_file=None, admin_ids=str(ADMIN_ID), **BASE_ENV)  # type: ignore[arg-type]


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
def bot(session: RecordingSession) -> Bot:
    return Bot(token=FAKE_TOKEN, session=session)


@pytest_asyncio.fixture
async def dispatcher(admin_settings, session_factory, panel, provider):
    return build_dispatcher(
        admin_settings,
        session_factory,
        panel,
        PaymentRegistry({provider.name: provider}),
        storage=MemoryStorage(),
    )


@pytest_asyncio.fixture
async def outsider_dispatcher(session_factory, panel):
    """A deployment where nobody is an admin."""
    settings = Settings(_env_file=None, **BASE_ENV)  # type: ignore[arg-type]
    return build_dispatcher(
        settings,
        session_factory,
        panel,
        PaymentRegistry({}),
        storage=MemoryStorage(),
    )


def sent_texts(session: RecordingSession) -> list[str]:
    return [
        request.text
        for request in session.requests
        if isinstance(request, SendMessage)
    ]


def alerts(session: RecordingSession) -> list[str]:
    return [
        request.text or ''
        for request in session.requests
        if isinstance(request, AnswerCallbackQuery)
    ]


class TestAccess:
    async def test_admin_opens_the_panel(
        self, dispatcher, bot, session
    ) -> None:
        await dispatcher.feed_update(bot, message_update('/admin'))

        assert any('Админка' in text for text in sent_texts(session))

    async def test_non_admin_gets_no_panel(
        self, outsider_dispatcher, bot, session
    ) -> None:
        """Not an error message — the panel simply does not exist.

        They do get the same "я понимаю только кнопки" every unknown
        message gets, which is what makes /admin indistinguishable from
        any other typing: a silent command would be the tell.
        """
        await outsider_dispatcher.feed_update(bot, message_update('/admin'))
        panel_reply = sent_texts(session)

        await outsider_dispatcher.feed_update(bot, message_update('привет'))
        ordinary_reply = sent_texts(session)[len(panel_reply) :]

        assert not any('Админка' in text for text in panel_reply)
        assert panel_reply == ordinary_reply

    async def test_non_admin_cannot_use_admin_callbacks(
        self, outsider_dispatcher, bot, session
    ) -> None:
        await outsider_dispatcher.feed_update(
            bot, callback_update(f'{keyboards.ADMIN_GRANT_PREFIX}777:365')
        )

        assert session.requests == []

    async def test_ping(self, dispatcher, bot, session) -> None:
        await dispatcher.feed_update(bot, message_update('/ping'))

        assert sent_texts(session) == ['pong']


class TestStats:
    async def test_stats_report_revenue_and_health(
        self, dispatcher, bot, session, session_factory, seeded_tariffs
    ) -> None:
        async with UnitOfWork(session_factory) as uow:
            await uow.users.upsert(CUSTOMER_ID)
            from app.db.models import Payment

            await uow.payments.add(
                Payment(
                    user_id=CUSTOMER_ID,
                    tariff_id=seeded_tariffs[0].id,
                    provider='yoomoney',
                    status=PaymentStatus.PROVISIONED,
                    amount_kopeks=20_000,
                    paid_at=datetime.now(UTC),
                )
            )
            await uow.commit()

        await dispatcher.feed_update(
            bot, callback_update(keyboards.ADMIN_STATS)
        )

        text = edited_texts(session)[-1]
        assert 'Статистика' in text
        assert '200 ₽' in text
        # Panel health is part of the screen: an offline node is invisible
        # to users but must be visible here.
        assert 'Панель' in text

    async def test_stats_survive_a_panel_outage(
        self, dispatcher, bot, session, panel
    ) -> None:
        panel.offline = True

        await dispatcher.feed_update(
            bot, callback_update(keyboards.ADMIN_STATS)
        )

        assert 'не отвечает' in edited_texts(session)[-1]


class TestUserCard:
    async def test_card_shows_subscription_and_payments(
        self, dispatcher, bot, session, session_factory, panel, admin_settings
    ) -> None:
        async with UnitOfWork(session_factory) as uow:
            await uow.users.upsert(CUSTOMER_ID, username='ivan')
            await uow.commit()
            subscriptions = SubscriptionService(uow, panel, admin_settings)
            subscription = await subscriptions.create_pending(
                CUSTOMER_ID,
                expires_at=datetime.now(UTC) + timedelta(days=10),
                origin=SubscriptionOrigin.PURCHASE,
            )
            await subscriptions.provision(subscription)

        await dispatcher.feed_update(
            bot, callback_update(f'{keyboards.ADMIN_USER_PREFIX}{CUSTOMER_ID}')
        )

        text = edited_texts(session)[-1]
        assert str(CUSTOMER_ID) in text
        assert 'active' in text

    async def test_unknown_user(self, dispatcher, bot, session) -> None:
        await dispatcher.feed_update(
            bot, callback_update(f'{keyboards.ADMIN_USER_PREFIX}999999')
        )

        assert 'не найден' in edited_texts(session)[-1]

    async def test_grant_days_creates_and_extends(
        self, dispatcher, bot, session, session_factory, panel
    ) -> None:
        async with UnitOfWork(session_factory) as uow:
            await uow.users.upsert(CUSTOMER_ID)
            await uow.commit()

        await dispatcher.feed_update(
            bot,
            callback_update(f'{keyboards.ADMIN_GRANT_PREFIX}{CUSTOMER_ID}:30'),
        )
        async with UnitOfWork(session_factory) as uow:
            subscription = await uow.subscriptions.get_by_user(CUSTOMER_ID)
            assert subscription is not None
            first = subscription.expires_at
            assert subscription.origin == SubscriptionOrigin.ADMIN_GRANT
            assert subscription.subscription_token is not None

        await dispatcher.feed_update(
            bot,
            callback_update(f'{keyboards.ADMIN_GRANT_PREFIX}{CUSTOMER_ID}:90'),
        )
        async with UnitOfWork(session_factory) as uow:
            subscription = await uow.subscriptions.get_by_user(CUSTOMER_ID)
            assert subscription is not None
            # Added on top of what was left, not from today.
            assert (subscription.expires_at - first).days == 90

    async def test_revoke_disables_without_deleting(
        self, dispatcher, bot, session, session_factory, panel
    ) -> None:
        async with UnitOfWork(session_factory) as uow:
            await uow.users.upsert(CUSTOMER_ID)
            await uow.commit()
        await dispatcher.feed_update(
            bot,
            callback_update(f'{keyboards.ADMIN_GRANT_PREFIX}{CUSTOMER_ID}:30'),
        )

        await dispatcher.feed_update(
            bot,
            callback_update(f'{keyboards.ADMIN_REVOKE_PREFIX}{CUSTOMER_ID}'),
        )

        async with UnitOfWork(session_factory) as uow:
            subscription = await uow.subscriptions.get_by_user(CUSTOMER_ID)
            assert subscription is not None
            assert subscription.status == SubscriptionStatus.REVOKED
        # The panel account survives: deleting it would revive a leaked
        # hysteria2:// link, because the password derives from the id.
        assert str(CUSTOMER_ID) in panel.users
        assert panel.users[str(CUSTOMER_ID)].enabled is False

    async def test_resync_calls_the_panel(
        self, dispatcher, bot, session, panel
    ) -> None:
        await dispatcher.feed_update(
            bot,
            callback_update(f'{keyboards.ADMIN_RESYNC_PREFIX}{CUSTOMER_ID}'),
        )

        assert 'sync' in panel.calls
        assert any('инхронизация' in alert for alert in alerts(session))

    async def test_resync_is_refused_while_the_last_one_is_warm(
        self, dispatcher, bot, session, panel
    ) -> None:
        """A full config push to the whole fleet, on a button anyone can
        hold down. The client's own docstring asks for a cooldown."""
        for _ in range(3):
            await dispatcher.feed_update(
                bot,
                callback_update(
                    f'{keyboards.ADMIN_RESYNC_PREFIX}{CUSTOMER_ID}'
                ),
            )

        assert panel.calls.count('sync') == 1
        assert any('одождите' in alert for alert in alerts(session))


class TestBroadcast:
    async def test_broadcast_copies_to_every_reachable_user(
        self, session_factory, panel
    ) -> None:
        """Copying, not forwarding: the sender must stay invisible."""
        session = RecordingSession()
        bot = Bot(token=FAKE_TOKEN, session=session)
        async with UnitOfWork(session_factory) as uow:
            for telegram_id in (1, 2, 3):
                await uow.users.upsert(telegram_id)
            await uow.users.set_bot_blocked(2, True)
            await uow.commit()

            service = BroadcastService(uow, bot)
            broadcast = await service.create(ADMIN_ID, 500)
            report = await service.run(broadcast)

        from aiogram.methods import CopyMessage, ForwardMessage

        copies = [r for r in session.requests if isinstance(r, CopyMessage)]
        assert report.sent == 2
        assert {c.chat_id for c in copies} == {1, 3}
        assert not any(isinstance(r, ForwardMessage) for r in session.requests)

    async def test_broadcast_resumes_from_the_cursor(
        self, session_factory
    ) -> None:
        session = RecordingSession()
        bot = Bot(token=FAKE_TOKEN, session=session)
        async with UnitOfWork(session_factory) as uow:
            for telegram_id in (1, 2, 3):
                await uow.users.upsert(telegram_id)
            await uow.commit()

            service = BroadcastService(uow, bot)
            broadcast = await service.create(ADMIN_ID, 500)
            # Pretend the first two were already delivered before a restart.
            broadcast.last_user_id = 2
            await uow.commit()

            await service.run(broadcast)

        from aiogram.methods import CopyMessage

        copies = [r for r in session.requests if isinstance(r, CopyMessage)]
        assert [c.chat_id for c in copies] == [3]


class TestExpiryReminders:
    async def _make_subscription(self, uow, panel, settings, days: float):
        await uow.users.upsert(CUSTOMER_ID)
        await uow.commit()
        subscriptions = SubscriptionService(uow, panel, settings)
        subscription = await subscriptions.create_pending(
            CUSTOMER_ID,
            expires_at=datetime.now(UTC) + timedelta(days=days),
            origin=SubscriptionOrigin.PURCHASE,
        )
        await subscriptions.provision(subscription)
        return subscription

    @pytest.mark.parametrize(
        'days_left, expected', [(0.5, 1), (2.5, 1), (5, 0), (0.9, 1)]
    )
    async def test_reminders_fire_inside_the_windows(
        self, session_factory, panel, admin_settings, days_left, expected
    ) -> None:
        session = RecordingSession()
        bot = Bot(token=FAKE_TOKEN, session=session)
        async with UnitOfWork(session_factory) as uow:
            await self._make_subscription(
                uow, panel, admin_settings, days_left
            )
            report = await NotificationService(
                uow, bot
            ).send_expiry_reminders()

        assert report.sent == expected
        assert len(sent_texts(session)) == expected

    async def test_a_reminder_is_never_sent_twice(
        self, session_factory, panel, admin_settings
    ) -> None:
        session = RecordingSession()
        bot = Bot(token=FAKE_TOKEN, session=session)
        async with UnitOfWork(session_factory) as uow:
            await self._make_subscription(uow, panel, admin_settings, 2.5)
            service = NotificationService(uow, bot)

            first = await service.send_expiry_reminders()
            second = await service.send_expiry_reminders()

        assert (first.sent, second.sent) == (1, 0)
        assert len(sent_texts(session)) == 1

    async def test_renewal_restarts_the_reminder_cycle(
        self, session_factory, panel, admin_settings
    ) -> None:
        session = RecordingSession()
        bot = Bot(token=FAKE_TOKEN, session=session)
        async with UnitOfWork(session_factory) as uow:
            subscription = await self._make_subscription(
                uow, panel, admin_settings, 2.5
            )
            service = NotificationService(uow, bot)
            await service.send_expiry_reminders()

            subscriptions = SubscriptionService(uow, panel, admin_settings)
            await subscriptions.extend(
                subscription, datetime.now(UTC) + timedelta(days=2.5)
            )

            assert subscription.notified_stage is None
            again = await service.send_expiry_reminders()

        assert again.sent == 1

    async def test_blocked_user_is_recorded_not_retried(
        self, session_factory, panel, admin_settings
    ) -> None:
        from aiogram.exceptions import TelegramForbiddenError

        class BlockingSession(RecordingSession):
            # Signature fixed by aiogram's BaseSession contract.
            async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
                raise TelegramForbiddenError(method=method, message='blocked')

        bot = Bot(token=FAKE_TOKEN, session=BlockingSession())
        async with UnitOfWork(session_factory) as uow:
            await self._make_subscription(uow, panel, admin_settings, 0.5)

            report = await NotificationService(
                uow, bot
            ).send_expiry_reminders()

            user = await uow.users.get(CUSTOMER_ID)
            assert user is not None
            assert user.is_bot_blocked is True
        assert report.blocked == 1


def _confirm_callback(session: RecordingSession) -> str:
    """The callback data of the newest «Отправить всем» button."""
    for request in reversed(session.requests):
        markup = getattr(request, 'reply_markup', None)
        for row in getattr(markup, 'inline_keyboard', []) or []:
            for button in row:
                data = button.callback_data or ''
                if data.startswith(keyboards.ADMIN_BROADCAST_GO_PREFIX):
                    return data
    raise AssertionError('no broadcast confirm button was sent')


class TestBroadcastRobustness:
    """Regressions from the pre-launch review."""

    async def test_double_tap_starts_one_broadcast(
        self, dispatcher, bot, session, session_factory
    ) -> None:
        from aiogram.methods import CopyMessage

        async with UnitOfWork(session_factory) as uow:
            for telegram_id in (1, 2, 3):
                await uow.users.upsert(telegram_id)
            await uow.commit()

        await dispatcher.feed_update(
            bot, callback_update(keyboards.ADMIN_BROADCAST)
        )
        await dispatcher.feed_update(bot, message_update('всем привет'))
        go = _confirm_callback(session)
        session.requests.clear()

        await dispatcher.feed_update(bot, callback_update(go))
        first_pass = len(
            [r for r in session.requests if isinstance(r, CopyMessage)]
        )
        await dispatcher.feed_update(bot, callback_update(go))

        copies = [r for r in session.requests if isinstance(r, CopyMessage)]
        # The second tap sends nothing more: nobody is messaged twice.
        assert len(copies) == first_pass

    async def test_claiming_a_draft_twice_wins_once(
        self, session_factory
    ) -> None:
        """The guard for two taps racing with the same draft in hand."""
        from app.services.broadcast_service import BroadcastService

        session = RecordingSession()
        bot = Bot(token=FAKE_TOKEN, session=session)
        async with UnitOfWork(session_factory) as uow:
            service = BroadcastService(uow, bot)
            draft = await service.create(ADMIN_ID, 500)

            first = await service.claim(draft.id)
            second = await service.claim(draft.id)

        assert first is not None
        assert second is None

    async def test_interrupted_broadcast_is_resumed(
        self, session_factory
    ) -> None:
        """A restart mid-run must not abandon the rest of the audience."""
        from datetime import timedelta

        from aiogram.methods import CopyMessage

        from app.core.enums import BroadcastStatus
        from app.services.broadcast_service import BroadcastService

        session = RecordingSession()
        bot = Bot(token=FAKE_TOKEN, session=session)
        async with UnitOfWork(session_factory) as uow:
            for telegram_id in (1, 2, 3):
                await uow.users.upsert(telegram_id)
            await uow.commit()

            service = BroadcastService(uow, bot)
            broadcast = await service.create(ADMIN_ID, 500)
            # Left behind by a restart: running, stalled after user 1.
            broadcast.status = BroadcastStatus.RUNNING
            broadcast.last_user_id = 1
            broadcast.updated_at = datetime.now(UTC) - timedelta(hours=1)
            await uow.commit()

            reports = await service.resume_stale()

            assert len(reports) == 1
            assert broadcast.status == BroadcastStatus.DONE

        copies = [r for r in session.requests if isinstance(r, CopyMessage)]
        # Only the audience past the cursor, and each exactly once.
        assert [c.chat_id for c in copies] == [2, 3]

    async def test_a_live_broadcast_is_not_stolen_by_the_resumer(
        self, session_factory
    ) -> None:
        from app.core.enums import BroadcastStatus
        from app.services.broadcast_service import BroadcastService

        session = RecordingSession()
        bot = Bot(token=FAKE_TOKEN, session=session)
        async with UnitOfWork(session_factory) as uow:
            await uow.users.upsert(1)
            await uow.commit()
            service = BroadcastService(uow, bot)
            broadcast = await service.create(ADMIN_ID, 500)
            broadcast.status = BroadcastStatus.RUNNING
            await uow.commit()

            # Freshly touched: someone is working on it right now.
            assert await service.resume_stale() == []


class TestTariffEditing:
    """PLAN.md §2: "цены и активность редактируются из админки без
    деплоя". The screen was read-only, so a price change meant SQL on
    the production database — the one place the plan says to avoid.
    """

    async def test_the_tariff_list_offers_a_button_per_tariff(
        self, dispatcher, bot, session, seeded_tariffs
    ) -> None:
        from tests.integration.test_trial_flow import button_texts

        await dispatcher.feed_update(
            bot, callback_update(keyboards.ADMIN_TARIFFS)
        )

        buttons = button_texts(session)
        for tariff in seeded_tariffs:
            assert any(tariff.title_ru in text for text in buttons)

    async def test_a_new_price_reaches_the_database_and_the_shop(
        self, dispatcher, bot, session, session_factory, seeded_tariffs
    ) -> None:
        tariff = seeded_tariffs[0]

        await dispatcher.feed_update(
            bot, callback_update(f'{keyboards.ADMIN_TARIFF_PREFIX}{tariff.id}')
        )
        await dispatcher.feed_update(
            bot,
            callback_update(
                f'{keyboards.ADMIN_TARIFF_PRICE_PREFIX}{tariff.id}'
            ),
        )
        await dispatcher.feed_update(bot, message_update('249'))

        async with UnitOfWork(session_factory) as uow:
            stored = await uow.tariffs.get(tariff.id)
            assert stored is not None
            # Rubles in, kopeks stored: money stays integer.
            assert stored.price_kopeks == 24_900

        session.requests.clear()
        await dispatcher.feed_update(bot, callback_update(keyboards.BUY))
        from tests.integration.test_trial_flow import button_texts

        assert any('249' in text for text in button_texts(session))

    async def test_a_price_that_is_not_a_number_is_refused(
        self, dispatcher, bot, session, session_factory, seeded_tariffs
    ) -> None:
        tariff = seeded_tariffs[0]
        before = tariff.price_kopeks

        await dispatcher.feed_update(
            bot,
            callback_update(
                f'{keyboards.ADMIN_TARIFF_PRICE_PREFIX}{tariff.id}'
            ),
        )
        await dispatcher.feed_update(bot, message_update('дёшево'))

        async with UnitOfWork(session_factory) as uow:
            stored = await uow.tariffs.get(tariff.id)
            assert stored is not None
            assert stored.price_kopeks == before
        assert any('число' in text for text in sent_texts(session))

    async def test_a_disabled_tariff_leaves_the_shop(
        self, dispatcher, bot, session, session_factory, seeded_tariffs
    ) -> None:
        from tests.integration.test_trial_flow import button_texts

        tariff = seeded_tariffs[0]

        await dispatcher.feed_update(
            bot,
            callback_update(
                f'{keyboards.ADMIN_TARIFF_TOGGLE_PREFIX}{tariff.id}'
            ),
        )

        async with UnitOfWork(session_factory) as uow:
            stored = await uow.tariffs.get(tariff.id)
            assert stored is not None
            assert stored.is_active is False

        session.requests.clear()
        await dispatcher.feed_update(bot, callback_update(keyboards.BUY))
        assert not any(
            tariff.title_ru in text for text in button_texts(session)
        )


class TestGrantSurvivesAPanelOutage:
    """The days are committed before the panel is called, so a panel
    failure does not mean the grant did not happen. Reporting only
    «Панель недоступна» and leaving the stale card on screen invited the
    admin to tap again, and the second tap added the days a second time.
    """

    async def test_the_admin_is_told_the_days_landed(
        self, dispatcher, bot, session, session_factory, panel
    ) -> None:
        async with UnitOfWork(session_factory) as uow:
            await uow.users.upsert(CUSTOMER_ID)
            await uow.commit()
        panel.offline = True

        await dispatcher.feed_update(
            bot,
            callback_update(f'{keyboards.ADMIN_GRANT_PREFIX}{CUSTOMER_ID}:30'),
        )

        # The days are in the database whatever the panel did.
        async with UnitOfWork(session_factory) as uow:
            subscription = await uow.subscriptions.get_by_user(CUSTOMER_ID)
            assert subscription is not None
            assert (subscription.expires_at - datetime.now(UTC)).days == 29
        # And the admin is told so, rather than being invited to retry.
        assert any('ачислен' in alert for alert in alerts(session))

    async def test_the_card_is_refreshed_so_the_new_date_is_visible(
        self, dispatcher, bot, session, session_factory, panel
    ) -> None:
        async with UnitOfWork(session_factory) as uow:
            await uow.users.upsert(CUSTOMER_ID)
            await uow.commit()
        panel.offline = True

        await dispatcher.feed_update(
            bot,
            callback_update(f'{keyboards.ADMIN_GRANT_PREFIX}{CUSTOMER_ID}:30'),
        )

        assert any('Подписка' in text for text in edited_texts(session))


class TestAnAdminIsStillAUser:
    """The admin router is included first and its message filter matches
    every private message from an admin, so a catch-all registered there
    swallows the user-side commands too — /start included, which is how
    an admin reaches the menu to buy or take a trial.
    """

    async def test_start_still_opens_the_menu(
        self, dispatcher, bot, session
    ) -> None:
        await dispatcher.feed_update(bot, message_update('/start'))

        texts_seen = sent_texts(session)
        assert not any('никуда не ушло' in text for text in texts_seen)
        assert any(
            'Rillza' in text or 'подписк' in text for text in texts_seen
        )

    async def test_prose_still_stops_at_the_admin_router(
        self, dispatcher, bot, session
    ) -> None:
        """The fix must not reopen the self-ticket hole."""
        await dispatcher.feed_update(bot, message_update('просто текст'))

        assert any('никуда не ушло' in t for t in sent_texts(session))
