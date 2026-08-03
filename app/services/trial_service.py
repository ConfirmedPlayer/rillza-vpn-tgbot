"""Free trial issuance.

The trial is granted once per Telegram account, forever. The latch is a
conditional UPDATE, so two taps racing each other cannot both win — and
it is consumed *before* the panel is called: if provisioning fails, the
subscription stays pending and is finished on the next attempt, rather
than handing out a second trial.
"""

from dataclasses import dataclass
from enum import Enum, auto

from app.core.enums import SubscriptionOrigin, SubscriptionStatus
from app.core.settings import Settings
from app.db.models import Subscription
from app.integrations.celerity import PanelError
from app.services.subscription_service import (
    DEFAULT_MAX_DEVICES,
    SubscriptionService,
    trial_expiry,
    utcnow,
)
from app.services.uow import UnitOfWork


class TrialOutcome(Enum):
    GRANTED = auto()
    ALREADY_USED = auto()
    HAS_SUBSCRIPTION = auto()
    #: Recorded in the database, but the panel could not be reached.
    PENDING_PROVISIONING = auto()


@dataclass(frozen=True, slots=True)
class TrialResult:
    outcome: TrialOutcome
    subscription: Subscription | None = None

    @property
    def granted(self) -> bool:
        return self.outcome is TrialOutcome.GRANTED


class TrialService:
    def __init__(
        self,
        uow: UnitOfWork,
        subscriptions: SubscriptionService,
        settings: Settings,
    ) -> None:
        self._uow = uow
        self._subscriptions = subscriptions
        self._settings = settings

    async def is_available(self, telegram_id: int) -> bool:
        user = await self._uow.users.get(telegram_id)
        if user is None or user.trial_used:
            return False
        return await self._subscriptions.get(telegram_id) is None

    async def grant(self, telegram_id: int, username: str = '') -> TrialResult:
        # Serialise against every other writer of this user's
        # subscription, the payment poller finalising a purchase most of
        # all: the trial button is drawn once, from an is_available()
        # answered at menu-render time, and nothing takes it back off
        # the screen when a purchase lands a moment later. An unlocked
        # read here saw no subscription, spent the trial latch in a
        # transaction of its own, and only then collided with the
        # purchase on uq_subscriptions_user_id — an error screen right
        # after a successful payment, with the one trial per account
        # gone for good. lock_user rather than a row lock because there
        # may be no row yet; that is the whole case it exists for.
        await self._uow.subscriptions.lock_user(telegram_id)
        existing = await self._uow.subscriptions.lock_by_user(telegram_id)
        if existing is not None:
            # Nothing left to insert, so end the transaction before the
            # panel call below: holding a lock across an HTTP round trip
            # would park every other writer of this user behind it.
            await self._uow.commit()
            # A pending *trial* row means an earlier attempt died before
            # the panel answered; finish it instead of refusing. The
            # status has to be checked as well as the token: revoking a
            # row that never reached the panel leaves REVOKED *and* no
            # token, and finishing that would hand back the access an
            # admin just took away — the trial button outlives the
            # keyboard that drew it.
            #
            # The origin has to be checked too. A purchase is pending for
            # the moment between _apply_days committing the row and the
            # panel answering, and a trial tapped inside that window used
            # to adopt it: the trial button did a purchase's
            # provisioning and reported it as a trial. The provisioning
            # watcher owns that recovery and runs every sixty seconds.
            if (
                existing.subscription_token is None
                and existing.status == SubscriptionStatus.PENDING
                and existing.origin == SubscriptionOrigin.TRIAL
            ):
                return await self._finish(existing, username)
            return TrialResult(TrialOutcome.HAS_SUBSCRIPTION, existing)

        now = utcnow()
        if await self._uow.users.mark_trial_used(telegram_id, now) is None:
            await self._uow.commit()
            return TrialResult(TrialOutcome.ALREADY_USED)

        subscription = await self._subscriptions.create_pending(
            telegram_id,
            expires_at=trial_expiry(self._settings, now),
            origin=SubscriptionOrigin.TRIAL,
            max_devices=DEFAULT_MAX_DEVICES,
            # The latch and the row land together, under the lock taken
            # above. Committing the latch on its own left a window where
            # the trial was spent and nothing had been created yet.
            commit=False,
        )
        await self._uow.commit()
        return await self._finish(subscription, username)

    async def _finish(
        self, subscription: Subscription, username: str
    ) -> TrialResult:
        try:
            provisioned = await self._subscriptions.provision(
                subscription, username=username
            )
        except PanelError:
            return TrialResult(TrialOutcome.PENDING_PROVISIONING, subscription)
        return TrialResult(TrialOutcome.GRANTED, provisioned)
