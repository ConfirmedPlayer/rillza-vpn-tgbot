"""Subscription lifecycle: create, provision, extend, revoke.

The database is written first and committed, then the panel is driven to
match. A crash in between leaves a ``pending`` subscription that the next
attempt finishes — never a paid user with no record.

Provisioning is idempotent: the panel call is create-or-fetch keyed by
the Telegram id, and the expiry sent is always the absolute date stored
on the row, so repeating it cannot grant extra days.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger

from app.core.enums import SubscriptionOrigin, SubscriptionStatus
from app.core.settings import Settings
from app.db.models import Subscription
from app.integrations.celerity import (
    CelerityClient,
    PanelError,
    PanelNotFoundError,
)
from app.integrations.celerity.schemas import SubscriptionInfo
from app.services.uow import UnitOfWork


@dataclass(frozen=True, slots=True)
class SubscriptionView:
    """Everything the "моя подписка" screen needs, already resolved."""

    subscription: Subscription
    url: str | None
    info: SubscriptionInfo | None

    @property
    def is_provisioned(self) -> bool:
        return self.url is not None


def utcnow() -> datetime:
    return datetime.now(UTC)


class SubscriptionService:
    def __init__(
        self, uow: UnitOfWork, panel: CelerityClient, settings: Settings
    ) -> None:
        self._uow = uow
        self._panel = panel
        self._settings = settings

    async def get(self, telegram_id: int) -> Subscription | None:
        return await self._uow.subscriptions.get_by_user(telegram_id)

    async def create_pending(
        self,
        telegram_id: int,
        expires_at: datetime,
        origin: SubscriptionOrigin,
        commit: bool = True,
    ) -> Subscription:
        """Record the subscription before touching the panel.

        ``commit=False`` leaves the row flushed but uncommitted, for a
        caller that must land it together with something else. Payment
        provisioning needs that: committing the days here and the
        idempotency latch afterwards made them two transactions, and a
        crash in the gap left days granted with nothing to stop them
        being granted again.
        """
        subscription = Subscription(
            id=uuid.uuid4(),
            user_id=telegram_id,
            status=SubscriptionStatus.PENDING,
            origin=origin,
            expires_at=expires_at,
            panel_user_id=str(telegram_id),
        )
        await self._uow.subscriptions.add(subscription)
        if commit:
            await self._uow.commit()
        return subscription

    async def provision(
        self, subscription: Subscription, username: str = ''
    ) -> Subscription:
        """Make the panel match the row, then mark the row active.

        Safe to call repeatedly: an existing panel account is reused and
        its expiry is set to the stored absolute date.
        """
        panel_user, created = await self._panel.create_or_get_user(
            subscription.panel_user_id,
            expire_at=subscription.expires_at,
            username=username,
        )
        if not created and panel_user.expire_at != subscription.expires_at:
            # The account predates this subscription (a returning user, or
            # a half-finished attempt): move it onto our absolute date.
            panel_user = await self._panel.set_expiry(
                subscription.panel_user_id, subscription.expires_at
            )

        now = utcnow()
        subscription.subscription_token = panel_user.subscription_token
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.provisioned_at = subscription.provisioned_at or now
        subscription.last_synced_at = now
        await self._uow.commit()

        logger.info(
            'Provisioned subscription {} for user {} until {}',
            subscription.id,
            subscription.user_id,
            subscription.expires_at.isoformat(),
        )
        return subscription

    async def extend(
        self, subscription: Subscription, until: datetime
    ) -> Subscription:
        """Move the expiry forward, then push it to the panel.

        Never moves backwards: a stale caller (a retried older payment, a
        reconciler working from an old read) must not take away days the
        subscription already has.
        """
        if until > subscription.expires_at:
            subscription.expires_at = until
        subscription.status = SubscriptionStatus.ACTIVE
        # A renewal starts the reminder cycle over.
        subscription.notified_stage = None
        await self._uow.commit()

        await self.push_expiry(subscription)
        return subscription

    async def push_expiry(self, subscription: Subscription) -> Subscription:
        """Make the panel agree with the row as it stands right now.

        Sends the subscription's own date rather than any caller-held
        value, so a retry can never install an outdated expiry.
        """
        try:
            panel_user = await self._panel.set_expiry(
                subscription.panel_user_id, subscription.expires_at
            )
        except PanelNotFoundError:
            # No account yet (an earlier provisioning failed) or it was
            # removed in the panel: create it rather than lose the days.
            panel_user, _ = await self._panel.create_or_get_user(
                subscription.panel_user_id, expire_at=subscription.expires_at
            )
        subscription.subscription_token = (
            panel_user.subscription_token or subscription.subscription_token
        )
        subscription.last_synced_at = utcnow()
        await self._uow.commit()
        return subscription

    async def revoke(self, subscription: Subscription) -> Subscription:
        """Block new connections; the account and its history stay."""
        subscription.status = SubscriptionStatus.REVOKED
        await self._uow.commit()

        await self._panel.revoke(subscription.panel_user_id)
        subscription.last_synced_at = utcnow()
        await self._uow.commit()
        return subscription

    def subscription_url(self, subscription: Subscription) -> str | None:
        if not subscription.subscription_token:
            return None
        return self._settings.subscription_url(subscription.subscription_token)

    async def describe(self, telegram_id: int) -> SubscriptionView | None:
        """Subscription plus live panel status, for the user's screen.

        A panel hiccup degrades to "no live data" rather than an error:
        the dates in our own database are what the user came to see.
        """
        subscription = await self.get(telegram_id)
        if subscription is None:
            return None

        url = self.subscription_url(subscription)
        info: SubscriptionInfo | None = None
        if subscription.subscription_token:
            try:
                info = await self._panel.get_subscription_info(
                    subscription.subscription_token
                )
            except PanelError as error:
                logger.warning(
                    'Panel status unavailable for user {}: {}',
                    telegram_id,
                    error,
                )
        return SubscriptionView(subscription=subscription, url=url, info=info)


def trial_expiry(
    settings: Settings, moment: datetime | None = None
) -> datetime:
    return (moment or utcnow()) + timedelta(days=settings.trial_days)
