"""Background jobs as the scheduler actually runs them.

The services underneath are covered elsewhere; what matters here is the
wiring: a job owns its unit of work, records a heartbeat whatever
happens, and — for money — tells the user what it did on their behalf.
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.methods import SendMessage

from app.core.enums import PaymentStatus
from app.core.settings import Settings
from app.db.models import JobHeartbeat
from app.integrations.payments import PaymentRegistry
from app.scheduler.jobs import PAYMENT_POLLER, PROVISIONING_WATCHER, JobRunner
from app.services.payment_service import PaymentService
from app.services.subscription_service import SubscriptionService
from app.services.uow import UnitOfWork
from tests.conftest import BASE_ENV
from tests.fake_panel import FakePanel
from tests.fake_payments import FakeProvider
from tests.fake_session import FAKE_TOKEN, RecordingSession

USER_ID = 42


@pytest.fixture
def app_settings() -> Settings:
    return Settings(_env_file=None, **BASE_ENV)  # type: ignore[arg-type]


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def panel() -> FakePanel:
    return FakePanel()


@pytest.fixture
def registry(provider: FakeProvider) -> PaymentRegistry:
    return PaymentRegistry({provider.name: provider})


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
def bot(session: RecordingSession) -> Bot:
    return Bot(token=FAKE_TOKEN, session=session)


@pytest_asyncio.fixture
async def runner(
    session_factory, app_settings, panel, registry, bot
) -> JobRunner:
    return JobRunner(session_factory, app_settings, panel, registry, bot)


def sent_to(session: RecordingSession, telegram_id: int) -> list[str]:
    return [
        request.text
        for request in session.requests
        if isinstance(request, SendMessage) and request.chat_id == telegram_id
    ]


@pytest_asyncio.fixture
async def paid_invoice(uow, panel, registry, app_settings, seeded_tariffs):
    """An invoice the user has paid but never came back to check."""
    await uow.users.upsert(USER_ID)
    await uow.commit()
    subscriptions = SubscriptionService(uow, panel, app_settings)
    service = PaymentService(uow, registry, subscriptions, app_settings)
    payment = await service.create_invoice(
        USER_ID, seeded_tariffs[0], 'yoomoney'
    )
    return payment


class TestThePollerTellsTheUser:
    async def test_a_payment_finalised_in_the_background_is_announced(
        self, runner, paid_invoice, provider, session, session_factory
    ) -> None:
        """The user paid and closed the bot; nobody would tell them."""
        provider.mark_paid(paid_invoice.id)

        await runner.poll_payments()

        messages = sent_to(session, USER_ID)
        assert len(messages) == 1
        assert 'Оплата получена' in messages[0]

        async with UnitOfWork(session_factory) as fresh:
            stored = await fresh.payments.get(paid_invoice.id)
            assert stored is not None
            assert stored.status == PaymentStatus.PROVISIONED

    async def test_an_unpaid_invoice_says_nothing(
        self, runner, paid_invoice, session
    ) -> None:
        await runner.poll_payments()

        assert sent_to(session, USER_ID) == []

    async def test_the_watcher_announces_what_it_rescues(
        self,
        runner,
        paid_invoice,
        provider,
        panel,
        session,
        uow,
        app_settings,
        registry,
    ) -> None:
        """Money taken while the panel was down, delivered later."""
        provider.mark_paid(paid_invoice.id)
        panel.offline = True
        subscriptions = SubscriptionService(uow, panel, app_settings)
        service = PaymentService(uow, registry, subscriptions, app_settings)
        await service.check_and_finalize(paid_invoice.id)
        panel.offline = False
        session.requests.clear()

        await runner.finish_provisioning()

        messages = sent_to(session, USER_ID)
        assert len(messages) == 1
        assert 'Оплата получена' in messages[0]

    async def test_a_user_who_blocked_the_bot_is_recorded_not_retried(
        self, runner, paid_invoice, provider, session, session_factory
    ) -> None:
        provider.mark_paid(paid_invoice.id)
        session.forbidden = True

        await runner.poll_payments()

        async with UnitOfWork(session_factory) as fresh:
            user = await fresh.users.get(USER_ID)
            assert user is not None
            assert user.is_bot_blocked is True
            stored = await fresh.payments.get(paid_invoice.id)
            assert stored is not None
            # The access was still delivered; only the telling failed.
            assert stored.status == PaymentStatus.PROVISIONED


class TestHeartbeats:
    async def test_a_successful_run_records_its_heartbeat(
        self, runner, session_factory, seeded_tariffs
    ) -> None:
        await runner.poll_payments()

        async with UnitOfWork(session_factory) as fresh:
            beat = await fresh.session.get(JobHeartbeat, PAYMENT_POLLER)
            assert beat is not None
            assert beat.last_success_at is not None
            assert beat.last_error is None

    async def test_a_failing_job_is_recorded_not_raised(
        self, runner, session_factory, seeded_tariffs, monkeypatch
    ) -> None:
        """A job must never kill the scheduler loop."""

        async def explode(self) -> None:
            raise RuntimeError('panel exploded')

        monkeypatch.setattr(PaymentService, 'finish_provisioning', explode)

        await runner.finish_provisioning()

        async with UnitOfWork(session_factory) as fresh:
            beat = await fresh.session.get(JobHeartbeat, PROVISIONING_WATCHER)
            assert beat is not None
            assert beat.last_error is not None
            assert 'panel exploded' in beat.last_error
            assert beat.last_error_at is not None
            assert beat.last_success_at is None


class TestExpiryReminderSkipsAFreshTrial:
    async def test_a_three_day_trial_is_not_nagged_on_its_first_day(
        self, runner, uow, panel, app_settings, session, session_factory
    ) -> None:
        """The 3-day window swallows a 3-day trial an hour after it starts."""
        from app.core.enums import SubscriptionOrigin, SubscriptionStatus

        await uow.users.upsert(USER_ID)
        subscriptions = SubscriptionService(uow, panel, app_settings)
        subscription = await subscriptions.create_pending(
            USER_ID,
            expires_at=datetime.now(UTC) + timedelta(days=3),
            origin=SubscriptionOrigin.TRIAL,
            max_devices=2,
        )
        subscription.status = SubscriptionStatus.ACTIVE
        await uow.commit()

        await runner.send_expiry_reminders()

        assert sent_to(session, USER_ID) == []

    async def test_the_same_trial_is_still_told_the_day_before(
        self, runner, uow, panel, app_settings, session
    ) -> None:
        """Suppressing the far notice must not mute the near one."""
        from app.core.enums import SubscriptionOrigin, SubscriptionStatus

        await uow.users.upsert(USER_ID)
        subscriptions = SubscriptionService(uow, panel, app_settings)
        subscription = await subscriptions.create_pending(
            USER_ID,
            expires_at=datetime.now(UTC) + timedelta(hours=12),
            origin=SubscriptionOrigin.TRIAL,
            max_devices=2,
        )
        subscription.status = SubscriptionStatus.ACTIVE
        # Granted two and a half days ago: a real three-day trial that
        # is now twelve hours from ending.
        subscription.created_at = datetime.now(UTC) - timedelta(
            days=2, hours=12
        )
        await uow.commit()

        await runner.send_expiry_reminders()

        messages = sent_to(session, USER_ID)
        assert len(messages) == 1
        assert 'заканчивается' in messages[0]

    async def test_a_purchase_keeps_its_three_day_warning(
        self, runner, uow, panel, app_settings, session
    ) -> None:
        from app.core.enums import SubscriptionOrigin, SubscriptionStatus

        await uow.users.upsert(USER_ID)
        subscriptions = SubscriptionService(uow, panel, app_settings)
        subscription = await subscriptions.create_pending(
            USER_ID,
            expires_at=datetime.now(UTC) + timedelta(days=2, hours=12),
            origin=SubscriptionOrigin.PURCHASE,
            max_devices=2,
        )
        subscription.status = SubscriptionStatus.ACTIVE
        await uow.commit()

        await runner.send_expiry_reminders()

        messages = sent_to(session, USER_ID)
        assert len(messages) == 1
        assert 'заканчивается' in messages[0]
