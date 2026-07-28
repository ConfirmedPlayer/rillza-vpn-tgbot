"""Subscription queries."""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import SubscriptionStatus
from app.db.models import Subscription


class SubscriptionsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_user(self, telegram_id: int) -> Subscription | None:
        result = await self._session.execute(
            select(Subscription).where(Subscription.user_id == telegram_id)
        )
        return result.scalar_one_or_none()

    async def lock_by_user(self, telegram_id: int) -> Subscription | None:
        """Take the user's subscription row FOR UPDATE.

        Two payments of the same user finalised at once would otherwise
        both read the same expiry and one duration would be lost.
        """
        result = await self._session.execute(
            select(Subscription)
            .where(Subscription.user_id == telegram_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def add(self, subscription: Subscription) -> Subscription:
        self._session.add(subscription)
        await self._session.flush()
        return subscription

    async def list_expiring_between(
        self, start: datetime, end: datetime
    ) -> Sequence[Subscription]:
        """Active subscriptions expiring inside a window (reminders)."""
        result = await self._session.execute(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at >= start,
                Subscription.expires_at < end,
            )
        )
        return result.scalars().all()

    async def list_due_for_expiry(
        self, moment: datetime
    ) -> Sequence[Subscription]:
        result = await self._session.execute(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at <= moment,
            )
        )
        return result.scalars().all()

    async def mark_expired(
        self, subscription_id: uuid.UUID, moment: datetime
    ) -> Subscription | None:
        """ACTIVE -> EXPIRED once; a concurrent run gets None.

        RETURNING keeps the in-session object authoritative (see the
        module docstring in repositories/payments.py).
        """
        result = await self._session.execute(
            update(Subscription)
            .where(
                Subscription.id == subscription_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at <= moment,
            )
            .values(status=SubscriptionStatus.EXPIRED, notified_stage=None)
            .returning(Subscription)
            .execution_options(
                synchronize_session=False, populate_existing=True
            )
        )
        return result.scalar_one_or_none()

    async def mark_notified(
        self, subscription_id: uuid.UUID, stage: str
    ) -> Subscription | None:
        """Record which expiry reminder went out; repeats get None."""
        result = await self._session.execute(
            update(Subscription)
            .where(
                Subscription.id == subscription_id,
                Subscription.notified_stage.is_distinct_from(stage),
            )
            .values(notified_stage=stage)
            .returning(Subscription)
            .execution_options(
                synchronize_session=False, populate_existing=True
            )
        )
        return result.scalar_one_or_none()

    async def list_all(self) -> Sequence[Subscription]:
        """Every subscription, for reconciliation against the panel."""
        result = await self._session.execute(
            select(Subscription).order_by(Subscription.user_id)
        )
        return result.scalars().all()

    async def count_by_status(self) -> dict[str, int]:
        result = await self._session.execute(
            select(Subscription.status, func.count()).group_by(
                Subscription.status
            )
        )
        return {status: total for status, total in result.all()}
