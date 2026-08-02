"""Reconciliation: the panel is corrected to match the database."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.core.enums import SubscriptionOrigin, SubscriptionStatus
from app.core.settings import Settings
from app.services.reconcile_service import ReconcileService
from app.services.subscription_service import SubscriptionService
from tests.conftest import BASE_ENV
from tests.fake_panel import FakePanel

USER_ID = 42


@pytest.fixture
def panel() -> FakePanel:
    return FakePanel()


@pytest.fixture
def app_settings() -> Settings:
    return Settings(_env_file=None, **BASE_ENV)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def subscriptions(uow, panel, app_settings) -> SubscriptionService:
    return SubscriptionService(uow, panel, app_settings)


@pytest_asyncio.fixture
async def reconciler(uow, panel, app_settings, subscriptions):
    return ReconcileService(uow, panel, app_settings, subscriptions)


async def make_subscription(uow, subscriptions, days=30, provision=True):
    await uow.users.upsert(USER_ID)
    await uow.commit()
    subscription = await subscriptions.create_pending(
        USER_ID,
        expires_at=datetime.now(UTC) + timedelta(days=days),
        origin=SubscriptionOrigin.PURCHASE,
    )
    if provision:
        await subscriptions.provision(subscription)
    return subscription


async def test_healthy_state_changes_nothing(
    uow, subscriptions, reconciler, panel
) -> None:
    await make_subscription(uow, subscriptions)

    report = await reconciler.run()

    assert report.checked == 1
    assert report.changed == 0
    assert report.failed == 0


async def test_missing_panel_account_is_recreated(
    uow, subscriptions, reconciler, panel
) -> None:
    """The panel's Xray push is fire-and-forget with no retry of its own."""
    subscription = await make_subscription(uow, subscriptions)
    panel.users.clear()

    report = await reconciler.run()

    assert report.created == 1
    assert str(USER_ID) in panel.users
    assert panel.users[str(USER_ID)].expire_at == subscription.expires_at


async def test_expiry_drift_is_corrected_to_our_date(
    uow, subscriptions, reconciler, panel
) -> None:
    subscription = await make_subscription(uow, subscriptions)
    stale = subscription.expires_at - timedelta(days=10)
    panel.users[str(USER_ID)] = panel.users[str(USER_ID)].model_copy(
        update={'expire_at': stale}
    )

    report = await reconciler.run()

    assert report.expiry_fixed == 1
    assert panel.users[str(USER_ID)].expire_at == subscription.expires_at


async def test_small_clock_drift_is_left_alone(
    uow, subscriptions, reconciler, panel
) -> None:
    subscription = await make_subscription(uow, subscriptions)
    panel.users[str(USER_ID)] = panel.users[str(USER_ID)].model_copy(
        update={'expire_at': subscription.expires_at + timedelta(seconds=5)}
    )

    report = await reconciler.run()

    assert report.changed == 0


async def test_user_disabled_behind_our_back_is_restored(
    uow, subscriptions, reconciler, panel
) -> None:
    await make_subscription(uow, subscriptions)
    panel.users[str(USER_ID)] = panel.users[str(USER_ID)].model_copy(
        update={'enabled': False}
    )

    report = await reconciler.run()

    assert report.expiry_fixed == 1
    assert panel.users[str(USER_ID)].enabled is True


async def test_revoked_subscription_is_re_disabled(
    uow, subscriptions, reconciler, panel
) -> None:
    subscription = await make_subscription(uow, subscriptions)
    await subscriptions.revoke(subscription)
    # Someone re-enabled the account in the panel by hand.
    panel.users[str(USER_ID)] = panel.users[str(USER_ID)].model_copy(
        update={'enabled': True}
    )

    report = await reconciler.run()

    assert report.re_disabled == 1
    assert panel.users[str(USER_ID)].enabled is False


async def test_revoked_user_is_never_recreated(
    uow, subscriptions, reconciler, panel
) -> None:
    """Recreating a revoked account would hand access back."""
    subscription = await make_subscription(uow, subscriptions)
    await subscriptions.revoke(subscription)
    panel.users.clear()

    report = await reconciler.run()

    assert report.created == 0
    assert panel.users == {}


async def test_unknown_panel_accounts_are_reported_not_touched(
    uow, subscriptions, reconciler, panel
) -> None:
    """A hand-made account must survive: disabling it would be an outage."""
    await make_subscription(uow, subscriptions)
    await panel.create_or_get_user(
        '999', expire_at=datetime.now(UTC) + timedelta(days=5)
    )

    report = await reconciler.run()

    assert report.orphans == ['999']
    assert panel.users['999'].enabled is True


async def test_panel_outage_is_counted_not_fatal(
    uow, subscriptions, reconciler, panel
) -> None:
    await make_subscription(uow, subscriptions)
    panel.users.clear()

    async def failing_create(*args, **kwargs):
        from app.integrations.celerity import PanelUnavailableError

        raise PanelUnavailableError('down')

    panel.create_or_get_user = failing_create  # type: ignore[assignment]

    report = await reconciler.run()

    assert report.failed == 1
    assert report.created == 0


async def test_pending_subscription_is_finished(
    uow, subscriptions, reconciler, panel
) -> None:
    """A trial that never reached the panel is completed here."""
    subscription = await make_subscription(uow, subscriptions, provision=False)
    assert subscription.status == SubscriptionStatus.PENDING

    report = await reconciler.run()

    assert report.created == 1
    assert subscription.status == SubscriptionStatus.ACTIVE
    assert subscription.subscription_token is not None


async def test_a_renewal_during_the_run_is_not_undone(
    uow, subscriptions, reconciler, panel, session_factory
) -> None:
    """The reconciler snapshots every subscription up front and, with
    expire_on_commit=False, never sees a status that changed since.

    An expired user whose payment lands mid-run was still classified
    from the old read and had their panel account switched off — a
    customer who has just paid, with no VPN until the next run.
    """
    from app.services.uow import UnitOfWork

    subscription = await make_subscription(uow, subscriptions, days=-1)
    subscription.status = SubscriptionStatus.EXPIRED
    await uow.commit()
    assert panel.users[str(USER_ID)].enabled is True

    original = uow.subscriptions.list_all

    async def snapshot_then_someone_renews():
        rows = await original()
        # The payment lands right after the snapshot, on its own session.
        async with UnitOfWork(session_factory) as other:
            renewed = await other.subscriptions.get_by_user(USER_ID)
            renewed.status = SubscriptionStatus.ACTIVE
            renewed.expires_at = datetime.now(UTC) + timedelta(days=30)
            await other.commit()
        return rows

    uow.subscriptions.list_all = snapshot_then_someone_renews
    await reconciler.run()

    # The customer paid; their access must survive the sweep.
    assert panel.users[str(USER_ID)].enabled is True
