"""Aggregates for the admin statistics screen."""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    PaymentStatus,
    SubscriptionOrigin,
    SubscriptionStatus,
)
from app.db.models import JobHeartbeat, Payment, Subscription, User


@dataclass(slots=True)
class Stats:
    users: int = 0
    active_subscriptions: int = 0
    trial_subscriptions: int = 0
    expired_subscriptions: int = 0
    #: Users whose trial turned into at least one payment.
    trial_converted: int = 0
    trials_issued: int = 0
    revenue_day_kopeks: int = 0
    revenue_week_kopeks: int = 0
    revenue_month_kopeks: int = 0
    payments_awaiting_provisioning: int = 0
    heartbeats: list[JobHeartbeat] = field(default_factory=list)

    @property
    def conversion_percent(self) -> int:
        if not self.trials_issued:
            return 0
        return round(self.trial_converted / self.trials_issued * 100)


class StatsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _scalar(self, statement) -> int:
        result = await self._session.execute(statement)
        return int(result.scalar_one() or 0)

    async def _revenue_since(self, moment: datetime) -> int:
        """Only money actually delivered counts as revenue."""
        return await self._scalar(
            select(func.coalesce(func.sum(Payment.amount_kopeks), 0)).where(
                Payment.status == PaymentStatus.PROVISIONED,
                Payment.paid_at >= moment,
            )
        )

    async def collect(self, now: datetime, day, week, month) -> Stats:
        stats = Stats()
        stats.users = await self._scalar(select(func.count(User.id)))
        stats.trials_issued = await self._scalar(
            select(func.count(User.id)).where(User.trial_used_at.is_not(None))
        )
        stats.active_subscriptions = await self._scalar(
            select(func.count(Subscription.id)).where(
                Subscription.status == SubscriptionStatus.ACTIVE
            )
        )
        stats.trial_subscriptions = await self._scalar(
            select(func.count(Subscription.id)).where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.origin == SubscriptionOrigin.TRIAL,
            )
        )
        stats.expired_subscriptions = await self._scalar(
            select(func.count(Subscription.id)).where(
                Subscription.status == SubscriptionStatus.EXPIRED
            )
        )
        stats.trial_converted = await self._scalar(
            select(func.count(func.distinct(Payment.user_id)))
            .join(User, User.id == Payment.user_id)
            .where(
                User.trial_used_at.is_not(None),
                Payment.status == PaymentStatus.PROVISIONED,
            )
        )
        stats.revenue_day_kopeks = await self._revenue_since(day)
        stats.revenue_week_kopeks = await self._revenue_since(week)
        stats.revenue_month_kopeks = await self._revenue_since(month)
        stats.payments_awaiting_provisioning = await self._scalar(
            select(func.count(Payment.id)).where(
                Payment.status == PaymentStatus.PAID
            )
        )
        result = await self._session.execute(
            select(JobHeartbeat).order_by(JobHeartbeat.job_name)
        )
        stats.heartbeats = list(result.scalars().all())
        return stats
