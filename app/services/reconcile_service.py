"""Bringing the panel back in line with the database.

The database is the source of truth, so reconciliation is one-directional:
whatever the panel says, it is corrected to match our rows. The panel has
no reconciliation of its own — its Xray pushes are fire-and-forget with no
retry — so a push that failed at purchase time would otherwise stay broken
until the customer complained.

Accounts the panel has and we do not are **reported, never touched**. An
unknown account is at least as likely to be one the operator created by
hand as it is to be junk, and disabling it would turn a reporting job into
an outage.
"""

from dataclasses import dataclass, field

from loguru import logger

from app.core.enums import SubscriptionStatus
from app.core.settings import Settings
from app.integrations.celerity import CelerityClient, PanelError, PanelUser
from app.services.subscription_service import SubscriptionService, utcnow
from app.services.uow import UnitOfWork

#: Expiry differences below this are clock noise, not drift.
TOLERANCE_SECONDS = 60
PAGE_SIZE = 200


@dataclass(slots=True)
class ReconcileReport:
    checked: int = 0
    created: int = 0
    expiry_fixed: int = 0
    re_disabled: int = 0
    failed: int = 0
    orphans: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return self.created + self.expiry_fixed + self.re_disabled


class ReconcileService:
    def __init__(
        self,
        uow: UnitOfWork,
        panel: CelerityClient,
        settings: Settings,
        subscriptions: SubscriptionService,
    ) -> None:
        self._uow = uow
        self._panel = panel
        self._settings = settings
        self._subscriptions = subscriptions

    async def run(self) -> ReconcileReport:
        report = ReconcileReport()
        panel_users = await self._load_panel_users()
        known: set[str] = set()

        for subscription in await self._uow.subscriptions.list_all():
            report.checked += 1
            known.add(subscription.panel_user_id)
            panel_user = panel_users.get(subscription.panel_user_id)
            try:
                # The list above is a snapshot, and this loop makes an
                # HTTP call per row, so by the time we reach a row it
                # can be minutes old. expire_on_commit=False means the
                # in-loop commits never refresh it either. Deciding
                # from that read switched off users who renewed while
                # the sweep was running — a customer who had just paid,
                # cut off until the next run four hours later.
                await self._uow.session.refresh(subscription)
                await self._reconcile_one(subscription, panel_user, report)
            except PanelError as error:
                report.failed += 1
                logger.warning(
                    'Reconcile failed for user {}: {}',
                    subscription.user_id,
                    error,
                )

        report.orphans = sorted(set(panel_users) - known)
        if report.orphans:
            logger.info(
                'Panel has {} account(s) unknown to the bot: {}',
                len(report.orphans),
                ', '.join(report.orphans[:20]),
            )
        if report.changed:
            logger.warning(
                'Reconcile repaired {} subscription(s)', report.changed
            )
        return report

    async def _load_panel_users(self) -> dict[str, PanelUser]:
        users: dict[str, PanelUser] = {}
        page = 1
        while True:
            batch, total = await self._panel.iter_users(page, PAGE_SIZE)
            for user in batch:
                users[user.user_id] = user
            if not batch or len(users) >= total:
                break
            page += 1
        return users

    async def _reconcile_one(
        self,
        subscription,
        panel_user: PanelUser | None,
        report: ReconcileReport,
    ) -> None:
        revoked = subscription.status in (
            SubscriptionStatus.REVOKED,
            # An expired subscription is equally 'must not be active':
            # recreating or extending it would hand access back for free.
            SubscriptionStatus.EXPIRED,
        )

        if panel_user is None:
            if revoked:
                # Nothing to disable, and creating it would restore access.
                return
            await self._subscriptions.provision(subscription)
            report.created += 1
            return

        if revoked:
            if panel_user.enabled:
                await self._panel.revoke(subscription.panel_user_id)
                report.re_disabled += 1
            return

        if self._expiry_differs(panel_user, subscription.expires_at):
            # Sending our absolute date also re-enables a user the panel
            # switched off, so one call repairs both kinds of drift.
            await self._subscriptions.extend(
                subscription, subscription.expires_at
            )
            report.expiry_fixed += 1
            return

        if not panel_user.enabled and subscription.status == (
            SubscriptionStatus.ACTIVE
        ):
            await self._subscriptions.extend(
                subscription, subscription.expires_at
            )
            report.expiry_fixed += 1
            return

        if (
            subscription.status == SubscriptionStatus.PENDING
            and panel_user.enabled
        ):
            # The panel is healthy and holds the right date, so the row
            # simply never heard that provisioning finished. Left alone
            # it stays PENDING for ever: the screen says «Выдаём
            # доступ» over a working link, and expiry_sync and the
            # reminders both skip it because they filter on ACTIVE.
            subscription.status = SubscriptionStatus.ACTIVE
            subscription.provisioned_at = (
                subscription.provisioned_at or utcnow()
            )
            report.expiry_fixed += 1

        subscription.last_synced_at = utcnow()
        if subscription.subscription_token != panel_user.subscription_token:
            subscription.subscription_token = panel_user.subscription_token
        await self._uow.commit()

    @staticmethod
    def _expiry_differs(panel_user: PanelUser, expected) -> bool:
        if panel_user.expire_at is None:
            return True
        drift = abs((panel_user.expire_at - expected).total_seconds())
        return drift > TOLERANCE_SECONDS
