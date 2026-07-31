"""Background jobs.

Every job writes a heartbeat, so a silently dying job is visible in the
admin statistics instead of degrading into log noise. All are registered
with ``max_instances=1`` and coalescing: a slow run must never stack.
"""

from collections.abc import Awaitable, Callable
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.texts import ru
from app.core.jobs import (
    BROADCAST_RESUMER,
    EXPIRY_NOTIFIER,
    EXPIRY_SYNC,
    INVOICE_EXPIRER,
    JOB_INTERVALS,
    LATE_PAYMENT_SWEEP,
    PAYMENT_POLLER,
    PROVISIONING_WATCHER,
    RECONCILER,
)
from app.core.settings import Settings
from app.db.models import JobHeartbeat
from app.integrations.celerity import CelerityClient
from app.integrations.payments import PaymentRegistry
from app.services.broadcast_service import BroadcastService
from app.services.notification_service import NotificationService
from app.services.payment_service import FinalizeResult, PaymentService
from app.services.reconcile_service import ReconcileService
from app.services.subscription_service import SubscriptionService, utcnow
from app.services.uow import UnitOfWork

#: Re-exported so callers keep importing job names from the scheduler.
__all__ = [
    'BROADCAST_RESUMER',
    'EXPIRY_NOTIFIER',
    'EXPIRY_SYNC',
    'INVOICE_EXPIRER',
    'LATE_PAYMENT_SWEEP',
    'PAYMENT_POLLER',
    'PROVISIONING_WATCHER',
    'RECONCILER',
    'JobRunner',
    'register_jobs',
]


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

    async def _announce_delivery(
        self, uow: UnitOfWork, delivered: list[FinalizeResult]
    ) -> None:
        """Tell people whose access a job delivered on their behalf.

        The "я оплатил" button answers in the handler, but nobody answers
        for a payment the poller or the watcher finished — the user is
        left staring at an invoice while their subscription is live.
        """
        for result in delivered:
            if result.payment is None or result.expires_at is None:
                continue
            telegram_id = result.payment.user_id
            text = ru.PAYMENT_SUCCESS.format(
                until=ru.format_date(result.expires_at)
            )
            try:
                await self._bot.send_message(telegram_id, text)
            except TelegramForbiddenError:
                await uow.users.set_bot_blocked(telegram_id, True)
                await uow.commit()
            except Exception as error:  # telling must not fail the job
                logger.warning(
                    'Could not announce payment to {}: {}', telegram_id, error
                )

    async def poll_payments(self) -> None:
        await self.run(PAYMENT_POLLER, self._poll_pending)

    async def _poll_pending(self, uow: UnitOfWork) -> None:
        delivered = await self._payments(uow).poll_pending()
        await self._announce_delivery(uow, delivered)

    async def finish_provisioning(self) -> None:
        await self.run(PROVISIONING_WATCHER, self._finish_provisioning)

    async def _finish_provisioning(self, uow: UnitOfWork) -> None:
        delivered = await self._payments(uow).finish_provisioning()
        await self._announce_delivery(uow, delivered)

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
    #: Delay before the first run, for jobs that would otherwise all
    #: fire at once on a cold start.
    first_run = {
        BROADCAST_RESUMER: timedelta(minutes=1),
        RECONCILER: timedelta(minutes=2),
        LATE_PAYMENT_SWEEP: timedelta(minutes=5),
    }
    actions = {
        PAYMENT_POLLER: runner.poll_payments,
        PROVISIONING_WATCHER: runner.finish_provisioning,
        INVOICE_EXPIRER: runner.expire_invoices,
        EXPIRY_SYNC: runner.sync_expired,
        EXPIRY_NOTIFIER: runner.send_expiry_reminders,
        BROADCAST_RESUMER: runner.resume_broadcasts,
        RECONCILER: runner.reconcile,
        LATE_PAYMENT_SWEEP: runner.sweep_late_payments,
    }
    for name, action in actions.items():
        # The interval comes from core.jobs, which the admin screen also
        # reads to decide whether a job has gone quiet for too long.
        extra = {}
        if name in first_run:
            extra['next_run_time'] = utcnow() + first_run[name]
        scheduler.add_job(
            action,
            'interval',
            seconds=JOB_INTERVALS[name].total_seconds(),
            id=name,
            **extra,
            **common,
        )
