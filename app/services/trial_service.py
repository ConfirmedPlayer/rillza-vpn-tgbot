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
        existing = await self._subscriptions.get(telegram_id)
        if existing is not None:
            # A pending row means an earlier attempt died before the
            # panel answered; finish it instead of refusing. The status
            # has to be checked as well as the token: revoking a row
            # that never reached the panel leaves REVOKED *and* no
            # token, and finishing that would hand back the access an
            # admin just took away — the trial button outlives the
            # keyboard that drew it.
            if (
                existing.subscription_token is None
                and existing.status == SubscriptionStatus.PENDING
            ):
                return await self._finish(existing, username)
            return TrialResult(TrialOutcome.HAS_SUBSCRIPTION, existing)

        now = utcnow()
        if await self._uow.users.mark_trial_used(telegram_id, now) is None:
            return TrialResult(TrialOutcome.ALREADY_USED)
        await self._uow.commit()

        subscription = await self._subscriptions.create_pending(
            telegram_id,
            expires_at=trial_expiry(self._settings, now),
            origin=SubscriptionOrigin.TRIAL,
            max_devices=DEFAULT_MAX_DEVICES,
        )
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
