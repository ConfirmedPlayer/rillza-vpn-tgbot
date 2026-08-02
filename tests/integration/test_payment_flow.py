"""Payment finalisation: races, retries and idempotency."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.core.enums import (
    PaymentStatus,
    SubscriptionOrigin,
    SubscriptionStatus,
)
from app.core.settings import Settings
from app.db.models import Payment
from app.integrations.payments import PaymentError, PaymentRegistry
from app.services.payment_service import FinalizeOutcome, PaymentService
from app.services.subscription_service import SubscriptionService
from app.services.uow import UnitOfWork
from tests.conftest import BASE_ENV
from tests.fake_panel import FakePanel
from tests.fake_payments import FakeProvider

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
def registry(provider) -> PaymentRegistry:
    return PaymentRegistry({provider.name: provider})


def build_service(uow, panel, registry, settings) -> PaymentService:
    subscriptions = SubscriptionService(uow, panel, settings)
    return PaymentService(uow, registry, subscriptions, settings)


@pytest_asyncio.fixture
async def payments(uow, panel, registry, app_settings, seeded_tariffs):
    await uow.users.upsert(USER_ID)
    await uow.commit()
    return build_service(uow, panel, registry, app_settings)


@pytest_asyncio.fixture
async def tariff(seeded_tariffs):
    return seeded_tariffs[0]  # m1: 30 days, 200 rubles


class TestInvoice:
    async def test_invoice_is_recorded_before_the_user_can_pay(
        self, payments, tariff, provider, uow
    ) -> None:
        payment = await payments.create_invoice(USER_ID, tariff, 'yoomoney')

        assert payment.status == PaymentStatus.PENDING
        assert payment.amount_kopeks == 20_000
        assert payment.invoice_url.endswith(str(payment.id))
        assert payment.provider_invoice_id == f'inv-{payment.id}'
        assert payment.invoice_expires_at > datetime.now(UTC)
        assert str(payment.id) in provider.invoices

    async def test_unknown_provider_is_refused(self, payments, tariff) -> None:
        from app.integrations.payments import PaymentError

        with pytest.raises(PaymentError):
            await payments.create_invoice(USER_ID, tariff, 'nonexistent')


class TestFinalize:
    async def test_unpaid_invoice_stays_pending(
        self, payments, tariff, uow
    ) -> None:
        payment = await payments.create_invoice(USER_ID, tariff, 'yoomoney')

        result = await payments.check_and_finalize(payment.id)

        assert result.outcome is FinalizeOutcome.NOT_PAID
        assert await uow.subscriptions.get_by_user(USER_ID) is None

    async def test_payment_grants_access(
        self, payments, tariff, provider, panel, uow
    ) -> None:
        payment = await payments.create_invoice(USER_ID, tariff, 'yoomoney')
        provider.mark_paid(payment.id)

        result = await payments.check_and_finalize(payment.id)

        assert result.outcome is FinalizeOutcome.PROVISIONED
        subscription = await uow.subscriptions.get_by_user(USER_ID)
        assert subscription is not None
        assert subscription.status == SubscriptionStatus.ACTIVE
        assert subscription.subscription_token is not None
        # 30 days from now, and the panel agrees with our row.
        assert (subscription.expires_at - datetime.now(UTC)).days == 29
        assert panel.users[str(USER_ID)].expire_at == subscription.expires_at

        stored = await uow.payments.get(payment.id)
        assert stored is not None
        assert stored.status == PaymentStatus.PROVISIONED
        # What actually arrived is recorded, not compared to the price.
        assert stored.paid_amount_kopeks == 19_800
        assert stored.amount_kopeks == 20_000

    async def test_double_tap_does_not_grant_twice(
        self, payments, tariff, provider, uow
    ) -> None:
        payment = await payments.create_invoice(USER_ID, tariff, 'yoomoney')
        provider.mark_paid(payment.id)

        first = await payments.check_and_finalize(payment.id)
        second = await payments.check_and_finalize(payment.id)

        assert first.outcome is FinalizeOutcome.PROVISIONED
        assert second.outcome is FinalizeOutcome.PROVISIONED
        subscription = await uow.subscriptions.get_by_user(USER_ID)
        assert subscription is not None
        assert subscription.expires_at == first.expires_at

    async def test_concurrent_taps_bounce_instead_of_queueing(
        self,
        session_factory,
        panel,
        registry,
        app_settings,
        seeded_tariffs,
        provider,
        uow,
    ) -> None:
        """A second worker must get BUSY, not a second provisioning."""
        await uow.users.upsert(USER_ID)
        await uow.commit()
        service = build_service(uow, panel, registry, app_settings)
        payment = await service.create_invoice(
            USER_ID, seeded_tariffs[0], 'yoomoney'
        )
        provider.mark_paid(payment.id)

        async with UnitOfWork(session_factory) as holder_uow:
            holder = build_service(holder_uow, panel, registry, app_settings)
            # Hold the row inside an open transaction.
            locked = await holder_uow.payments.lock_for_finalize(payment.id)
            assert locked is not None

            async with UnitOfWork(session_factory) as other_uow:
                other = build_service(other_uow, panel, registry, app_settings)
                result = await other.check_and_finalize(payment.id)

            assert result.outcome is FinalizeOutcome.BUSY
            assert await holder.check_and_finalize(payment.id) is not None

    async def test_crash_between_paid_and_provisioned_is_recovered(
        self, payments, tariff, provider, panel, uow
    ) -> None:
        """Money taken, panel down: the watcher finishes the job."""
        payment = await payments.create_invoice(USER_ID, tariff, 'yoomoney')
        provider.mark_paid(payment.id)
        panel.offline = True

        interrupted = await payments.check_and_finalize(payment.id)

        assert interrupted.outcome is FinalizeOutcome.PAID_PENDING_PROVISIONING
        stored = await uow.payments.get(payment.id)
        assert stored is not None
        assert stored.status == PaymentStatus.PAID
        # The days are already in the subscription and latched to this
        # payment, so the retry knows not to add them a second time.
        assert stored.days_applied_at is not None
        subscription = await uow.subscriptions.get_by_user(USER_ID)
        assert subscription is not None
        granted = subscription.expires_at

        panel.offline = False
        finished = await payments.finish_provisioning()

        assert len(finished) == 1
        # Same days delivered: no extra month for the delay, and none
        # taken away either.
        assert subscription.expires_at == granted
        assert panel.users[str(USER_ID)].expire_at == granted

    async def test_renewal_adds_to_the_remaining_days(
        self, payments, seeded_tariffs, provider, uow
    ) -> None:
        first = await payments.create_invoice(
            USER_ID, seeded_tariffs[0], 'yoomoney'
        )
        provider.mark_paid(first.id)
        initial = await payments.check_and_finalize(first.id)

        second = await payments.create_invoice(
            USER_ID, seeded_tariffs[0], 'yoomoney'
        )
        provider.mark_paid(second.id)
        renewed = await payments.check_and_finalize(second.id)

        assert renewed.expires_at is not None
        assert initial.expires_at is not None
        # Renewing early keeps what was left: +30 days on top.
        assert (renewed.expires_at - initial.expires_at).days == 30

    async def test_provider_outage_is_not_a_failed_payment(
        self, payments, tariff, provider, uow
    ) -> None:
        payment = await payments.create_invoice(USER_ID, tariff, 'yoomoney')
        provider.offline = True

        result = await payments.check_and_finalize(payment.id)

        assert result.outcome is FinalizeOutcome.PROVIDER_UNAVAILABLE
        stored = await uow.payments.get(payment.id)
        assert stored is not None
        assert stored.status == PaymentStatus.PENDING

    async def test_unknown_payment(self, payments) -> None:
        import uuid

        result = await payments.check_and_finalize(uuid.uuid4())

        assert result.outcome is FinalizeOutcome.UNKNOWN


class TestExpiryAndLateMoney:
    async def test_stale_invoices_expire(self, payments, tariff, uow) -> None:
        payment = await payments.create_invoice(USER_ID, tariff, 'yoomoney')
        payment.invoice_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await uow.commit()

        expired = await payments.expire_stale()

        assert [p.id for p in expired] == [payment.id]
        stored = await uow.payments.get(payment.id)
        assert stored is not None
        assert stored.status == PaymentStatus.EXPIRED

    async def test_paid_money_is_never_swept_into_expired(
        self, payments, tariff, provider, uow
    ) -> None:
        payment = await payments.create_invoice(USER_ID, tariff, 'yoomoney')
        provider.mark_paid(payment.id)
        await payments.check_and_finalize(payment.id)
        payment.invoice_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await uow.commit()

        assert await payments.expire_stale() == []
        stored = await uow.payments.get(payment.id)
        assert stored is not None
        assert stored.status == PaymentStatus.PROVISIONED

    async def test_late_money_is_found_and_delivered(
        self, payments, tariff, provider, uow
    ) -> None:
        """Money that arrives after the TTL must not vanish."""
        payment = await payments.create_invoice(USER_ID, tariff, 'yoomoney')
        payment.invoice_expires_at = datetime.now(UTC) - timedelta(hours=2)
        await uow.commit()
        await payments.expire_stale()
        # The user paid anyway, after the invoice was closed.
        provider.mark_paid(payment.id)

        late = await payments.sweep_late_payments()

        assert [p.id for p in late] == [payment.id]
        stored = await uow.payments.get(payment.id)
        assert stored is not None
        assert stored.status == PaymentStatus.PROVISIONED
        subscription = await uow.subscriptions.get_by_user(USER_ID)
        assert subscription is not None
        assert subscription.status == SubscriptionStatus.ACTIVE

    async def test_sweep_ignores_still_unpaid_invoices(
        self, payments, tariff, uow
    ) -> None:
        payment = await payments.create_invoice(USER_ID, tariff, 'yoomoney')
        payment.invoice_expires_at = datetime.now(UTC) - timedelta(hours=2)
        await uow.commit()
        await payments.expire_stale()

        assert await payments.sweep_late_payments() == []
        stored = await uow.payments.get(payment.id)
        assert stored is not None
        assert stored.status == PaymentStatus.EXPIRED

    async def test_money_older_than_a_day_is_still_found(
        self, payments, tariff, provider, uow
    ) -> None:
        """The sweep runs daily, so a day-wide window leaves gaps.

        One restart, one misfire, one run that took a minute too long,
        and an invoice falls out of the window between two runs — with
        nothing else in the system that ever looks at it again.
        """
        payment = await payments.create_invoice(USER_ID, tariff, 'yoomoney')
        payment.invoice_expires_at = datetime.now(UTC) - timedelta(days=3)
        await uow.commit()
        await payments.expire_stale()
        provider.mark_paid(payment.id)

        late = await payments.sweep_late_payments()

        assert [p.id for p in late] == [payment.id]
        stored = await uow.payments.get(payment.id)
        assert stored is not None
        assert stored.status == PaymentStatus.PROVISIONED

    async def test_a_provider_outage_does_not_lose_the_payment(
        self, payments, tariff, provider, uow
    ) -> None:
        """A skipped check must survive until the next daily run.

        The provider being down is exactly when the invoice is closest
        to ageing out: the sweep only comes back in twenty-four hours,
        by which point a day-wide window no longer covers it.
        """
        payment = await payments.create_invoice(USER_ID, tariff, 'yoomoney')
        payment.invoice_expires_at = datetime.now(UTC) - timedelta(hours=2)
        await uow.commit()
        await payments.expire_stale()
        provider.mark_paid(payment.id)
        provider.offline = True

        assert await payments.sweep_late_payments() == []

        # A day later, the next run.
        provider.offline = False
        payment.invoice_expires_at = datetime.now(UTC) - timedelta(hours=26)
        await uow.commit()
        late = await payments.sweep_late_payments()

        assert [p.id for p in late] == [payment.id]


class TestPoller:
    async def test_poller_finalises_paid_invoices_only(
        self, payments, seeded_tariffs, provider, uow
    ) -> None:
        paid = await payments.create_invoice(
            USER_ID, seeded_tariffs[0], 'yoomoney'
        )
        provider.mark_paid(paid.id)

        finalised = await payments.poll_pending()

        assert len(finalised) == 1
        stored = await uow.payments.get(paid.id)
        assert stored is not None
        assert stored.status == PaymentStatus.PROVISIONED


class TestProvisioningIsMonotonic:
    """Regressions from the pre-launch review.

    Provisioning used to compare dates to decide what to do, which let a
    retried older payment move a subscription backwards and let an admin
    grant swallow a payment. The payment now carries its own
    days-applied latch.
    """

    async def test_stuck_older_payment_never_shrinks_the_subscription(
        self, payments, seeded_tariffs, provider, panel, uow
    ) -> None:
        tariff = seeded_tariffs[0]
        first = await payments.create_invoice(USER_ID, tariff, 'yoomoney')
        provider.mark_paid(first.id)
        panel.offline = True
        # First payment: days land in the database, panel push fails.
        await payments.check_and_finalize(first.id)
        panel.offline = False

        second = await payments.create_invoice(USER_ID, tariff, 'yoomoney')
        provider.mark_paid(second.id)
        await payments.check_and_finalize(second.id)

        subscription = await uow.subscriptions.get_by_user(USER_ID)
        assert subscription is not None
        after_both = subscription.expires_at

        # The watcher now retries the older, still-PAID payment.
        await payments.finish_provisioning()

        assert subscription.expires_at == after_both
        assert panel.users[str(USER_ID)].expire_at == after_both
        # Both payments delivered a full month each.
        assert (after_both - datetime.now(UTC)).days == 59

    async def test_admin_grant_is_not_erased_by_a_retry(
        self, payments, seeded_tariffs, provider, panel, uow, app_settings
    ) -> None:
        from app.services.subscription_service import SubscriptionService

        payment = await payments.create_invoice(
            USER_ID, seeded_tariffs[0], 'yoomoney'
        )
        provider.mark_paid(payment.id)
        panel.offline = True
        await payments.check_and_finalize(payment.id)
        panel.offline = False

        subscriptions = SubscriptionService(uow, panel, app_settings)
        subscription = await uow.subscriptions.get_by_user(USER_ID)
        assert subscription is not None
        granted_until = subscription.expires_at + timedelta(days=60)
        await subscriptions.extend(subscription, granted_until)

        await payments.finish_provisioning()

        assert subscription.expires_at == granted_until

    async def test_retry_repairs_a_panel_that_missed_the_renewal(
        self, payments, seeded_tariffs, provider, panel, uow
    ) -> None:
        """The panel must end up holding the new date, not the old one."""
        tariff = seeded_tariffs[0]
        first = await payments.create_invoice(USER_ID, tariff, 'yoomoney')
        provider.mark_paid(first.id)
        await payments.check_and_finalize(first.id)
        after_first = panel.users[str(USER_ID)].expire_at

        renewal = await payments.create_invoice(USER_ID, tariff, 'yoomoney')
        provider.mark_paid(renewal.id)
        panel.offline = True
        await payments.check_and_finalize(renewal.id)
        # Database moved on, panel is still on the old date.
        assert panel.users[str(USER_ID)].expire_at == after_first

        panel.offline = False
        await payments.finish_provisioning()

        subscription = await uow.subscriptions.get_by_user(USER_ID)
        assert subscription is not None
        assert panel.users[str(USER_ID)].expire_at == subscription.expires_at
        stored = await uow.payments.get(renewal.id)
        assert stored is not None
        assert stored.status == PaymentStatus.PROVISIONED

    async def test_days_are_applied_exactly_once(
        self, payments, seeded_tariffs, provider, uow
    ) -> None:
        payment = await payments.create_invoice(
            USER_ID, seeded_tariffs[0], 'yoomoney'
        )
        provider.mark_paid(payment.id)

        await payments.check_and_finalize(payment.id)
        subscription = await uow.subscriptions.get_by_user(USER_ID)
        assert subscription is not None
        once = subscription.expires_at

        for _ in range(3):
            await payments.check_and_finalize(payment.id)

        assert subscription.expires_at == once


class TestStaleWorkerCannotDoubleApply:
    """The provisioning watcher loads its queue once, then spends a panel
    round trip per payment. Whatever happens meanwhile — a "проверить
    оплату" tap, another worker — lands in the database while the watcher
    still holds objects loaded before it.

    Deciding idempotency from those objects granted one payment's days
    twice. These pin both halves of the fix.
    """

    async def _paid_payment(self, session_factory, tariff) -> object:
        async with UnitOfWork(session_factory) as setup:
            await setup.users.upsert(USER_ID)
            payment = Payment(
                id=uuid.uuid4(),
                user_id=USER_ID,
                tariff_id=tariff.id,
                provider='yoomoney',
                status=PaymentStatus.PAID,
                amount_kopeks=tariff.price_kopeks,
                invoice_expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
            await setup.payments.add(payment)
            await setup.commit()
            return payment.id

    async def test_locking_a_payment_refreshes_the_copy_it_returns(
        self, session_factory, tariff
    ) -> None:
        """SELECT ... FOR UPDATE on a row the session already holds a
        copy of takes the lock and, without populate_existing, hands back
        the pre-lock attributes. The lock then guards nothing the caller
        can see."""
        payment_id = await self._paid_payment(session_factory, tariff)
        latched_at = datetime.now(UTC)

        async with UnitOfWork(session_factory) as worker:
            snapshot = await worker.payments.get(payment_id)
            assert snapshot is not None
            assert snapshot.days_applied_at is None

            async with UnitOfWork(session_factory) as other:
                assert (
                    await other.payments.mark_days_applied(
                        payment_id, latched_at
                    )
                    is not None
                )
                await other.commit()

            locked = await worker.payments.lock_for_finalize(payment_id)

        assert locked is not None
        assert locked.days_applied_at is not None

    async def test_days_claimed_elsewhere_are_not_granted_again(
        self, session_factory, panel, registry, app_settings, tariff
    ) -> None:
        """Even handed a payment whose in-memory copy says "not applied",
        provisioning must not add the duration a second time: the
        conditional UPDATE is the authority, not the attribute."""
        payment_id = await self._paid_payment(session_factory, tariff)
        start = datetime.now(UTC) + timedelta(days=10)
        async with UnitOfWork(session_factory) as setup:
            await SubscriptionService(
                setup, panel, app_settings
            ).create_pending(
                USER_ID, expires_at=start, origin=SubscriptionOrigin.TRIAL
            )

        async with UnitOfWork(session_factory) as worker:
            snapshot = await worker.payments.get(payment_id)
            assert snapshot is not None and snapshot.days_applied_at is None

            # Another session finishes the same payment end to end.
            async with UnitOfWork(session_factory) as other:
                await build_service(
                    other, panel, registry, app_settings
                ).check_and_finalize(payment_id)

            # The watcher now works through its stale queue entry.
            await build_service(
                worker, panel, registry, app_settings
            )._provision(snapshot)

        async with UnitOfWork(session_factory) as check:
            subscription = await check.subscriptions.get_by_user(USER_ID)
            assert subscription is not None
            assert subscription.expires_at == start + timedelta(
                days=tariff.duration_days
            )


class TestTheRowExistsBeforeTheProviderDoes:
    """The docstring said "record the invoice, then ask the provider" —
    the code did the opposite. Dying between the provider answering and
    the commit leaves a payable link at the provider whose label matches
    no row: the poller iterates rows, the late sweep iterates rows, so
    money paid on it is invisible to everything.

    Reproduced for real: the bot was restarted mid-purchase and the
    payment simply did not exist afterwards.
    """

    async def test_a_failing_provider_still_leaves_a_record(
        self, payments, tariff, provider, uow, session_factory
    ) -> None:
        provider.offline = True

        with pytest.raises(PaymentError):
            await payments.create_invoice(USER_ID, tariff, 'yoomoney')

        async with UnitOfWork(session_factory) as fresh:
            rows = await fresh.payments.list_by_user(USER_ID)
            assert len(rows) == 1
            # Closed, so the poller does not chase an invoice that was
            # never issued.
            assert rows[0].status == PaymentStatus.EXPIRED
            assert rows[0].invoice_url is None

    async def test_a_good_invoice_still_arrives_complete(
        self, payments, tariff, provider, uow
    ) -> None:
        payment = await payments.create_invoice(USER_ID, tariff, 'yoomoney')

        assert payment.status == PaymentStatus.PENDING
        assert payment.invoice_url
        assert payment.provider_invoice_id == f'inv-{payment.id}'
