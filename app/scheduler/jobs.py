"""Background jobs.

Every job writes a heartbeat, so a silently dying job is visible in the
admin statistics instead of degrading into log noise. All are registered
with ``max_instances=1`` and coalescing: a slow run must never stack.
"""

from collections.abc import Awaitable, Callable
from datetime import timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings
from app.db.models import JobHeartbeat
from app.integrations.celerity import CelerityClient
from app.integrations.payments import PaymentRegistry
from app.services.broadcast_service import BroadcastService
from app.services.notification_service import NotificationService
from app.services.payment_service import PaymentService
from app.services.reconcile_service import ReconcileService
from app.services.subscription_service import SubscriptionService, utcnow
from app.services.uow import UnitOfWork

PAYMENT_POLLER = 'payment_poller'
PROVISIONING_WATCHER = 'provisioning_watcher'
INVOICE_EXPIRER = 'invoice_expirer'
EXPIRY_SYNC = 'expiry_sync'
LATE_PAYMENT_SWEEP = 'late_payment_sweep'
EXPIRY_NOTIFIER = 'expiry_notifier'
RECONCILER = 'reconciler'
BROADCAST_RESUMER = 'broadcast_resumer'


class JobRunner:
    """Builds a unit of work per run and records the outcome."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        panel: CelerityClient,
        providers: PaymentRegistry,
        bot: Bot,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._panel = panel
        self._providers = providers
        self._bot = bot

    def _payments(self, uow: UnitOfWork) -> PaymentService:
        subscriptions = SubscriptionService(uow, self._panel, self._settings)
        return PaymentService(
            uow, self._providers, subscriptions, self._settings
        )

    async def _heartbeat(
        self, uow: UnitOfWork, name: str, error: str | None
    ) -> None:
        beat = await uow.session.get(JobHeartbeat, name) or JobHeartbeat(
            job_name=name
        )
        now = utcnow()
        if error is None:
            beat.last_success_at = now
        else:
            beat.last_error = error[:500]
            beat.last_error_at = now
        uow.session.add(beat)
        await uow.commit()

    async def run(
        self, name: str, action: Callable[[UnitOfWork], Awaitable[object]]
    ) -> None:
        """Run one job; failures are recorded, never raised into the loop."""
        error: str | None = None
        async with UnitOfWork(self._session_factory) as uow:
            try:
                await action(uow)
            except Exception as exc:  # a job must never kill the loop
                error = repr(exc)
                logger.exception('Job {} failed', name)
                # The session may be poisoned (or hold partial writes);
                # start clean so the heartbeat itself can be recorded.
                await uow.rollback()
            await self._heartbeat(uow, name, error)

    async def poll_payments(self) -> None:
        await self.run(
            PAYMENT_POLLER, lambda uow: self._payments(uow).poll_pending()
        )

    async def finish_provisioning(self) -> None:
        await self.run(
            PROVISIONING_WATCHER,
            lambda uow: self._payments(uow).finish_provisioning(),
        )

    async def expire_invoices(self) -> None:
        await self.run(
            INVOICE_EXPIRER, lambda uow: self._payments(uow).expire_stale()
        )

    async def sweep_late_payments(self) -> None:
        await self.run(
            LATE_PAYMENT_SWEEP,
            lambda uow: self._payments(uow).sweep_late_payments(),
        )

    async def send_expiry_reminders(self) -> None:
        await self.run(
            EXPIRY_NOTIFIER,
            lambda uow: NotificationService(
                uow, self._bot
            ).send_expiry_reminders(),
        )

    async def resume_broadcasts(self) -> None:
        await self.run(
            BROADCAST_RESUMER,
            lambda uow: BroadcastService(uow, self._bot).resume_stale(),
        )

    async def reconcile(self) -> None:
        def action(uow: UnitOfWork):
            subscriptions = SubscriptionService(
                uow, self._panel, self._settings
            )
            return ReconcileService(
                uow, self._panel, self._settings, subscriptions
            ).run()

        await self.run(RECONCILER, action)

    async def sync_expired(self) -> None:
        async def action(uow: UnitOfWork) -> None:
            now = utcnow()
            for subscription in await uow.subscriptions.list_due_for_expiry(
                now
            ):
                await uow.subscriptions.mark_expired(subscription.id, now)
            await uow.commit()

        await self.run(EXPIRY_SYNC, action)


def register_jobs(scheduler: AsyncIOScheduler, runner: JobRunner) -> None:
    common = {'max_instances': 1, 'coalesce': True, 'misfire_grace_time': 60}
    scheduler.add_job(
        runner.poll_payments,
        'interval',
        seconds=30,
        id=PAYMENT_POLLER,
        **common,
    )
    scheduler.add_job(
        runner.finish_provisioning,
        'interval',
        seconds=60,
        id=PROVISIONING_WATCHER,
        **common,
    )
    scheduler.add_job(
        runner.expire_invoices,
        'interval',
        minutes=5,
        id=INVOICE_EXPIRER,
        **common,
    )
    scheduler.add_job(
        runner.sync_expired, 'interval', minutes=10, id=EXPIRY_SYNC, **common
    )
    scheduler.add_job(
        runner.send_expiry_reminders,
        'interval',
        hours=1,
        id=EXPIRY_NOTIFIER,
        **common,
    )
    scheduler.add_job(
        runner.resume_broadcasts,
        'interval',
        minutes=5,
        id=BROADCAST_RESUMER,
        next_run_time=utcnow() + timedelta(minutes=1),
        **common,
    )
    scheduler.add_job(
        runner.reconcile,
        'interval',
        hours=4,
        id=RECONCILER,
        next_run_time=utcnow() + timedelta(minutes=2),
        **common,
    )
    scheduler.add_job(
        runner.sweep_late_payments,
        'interval',
        hours=24,
        id=LATE_PAYMENT_SWEEP,
        next_run_time=utcnow() + timedelta(minutes=5),
        **common,
    )
