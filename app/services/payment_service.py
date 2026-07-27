"""Taking money and turning it into access.

The invariant: once a payment is recorded as paid, the user gets their
days — whatever crashes next. That rests on three things.

* ``check_and_finalize`` is the *only* way a payment advances, shared by
  the "проверить оплату" button and the poller. It takes the row with
  ``FOR UPDATE SKIP LOCKED``, so a second tap bounces with BUSY instead
  of queueing behind an HTTP call and provisioning twice.
* ``target_expires_at`` is computed once, when the payment turns paid,
  and never recomputed. A retry that recalculated "now + 30 days" would
  hand out a second month for one payment.
* ``paid`` is a real status, so "money taken, access not delivered" is a
  queryable state and the watcher can finish it after any crash.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto

from loguru import logger

from app.core.enums import PaymentStatus, SubscriptionOrigin
from app.core.settings import Settings
from app.db.models import Payment, Tariff
from app.integrations.celerity import PanelError
from app.integrations.payments import PaymentError, PaymentRegistry
from app.services.subscription_service import SubscriptionService, utcnow
from app.services.uow import UnitOfWork


class FinalizeOutcome(Enum):
    #: Paid and access delivered.
    PROVISIONED = auto()
    #: Paid, but the panel did not answer — the watcher will finish it.
    PAID_PENDING_PROVISIONING = auto()
    #: The provider has not seen the money yet.
    NOT_PAID = auto()
    #: The invoice's time ran out.
    EXPIRED = auto()
    #: Someone else is already finalising this payment.
    BUSY = auto()
    #: Unknown payment id.
    UNKNOWN = auto()
    #: The provider could not be reached.
    PROVIDER_UNAVAILABLE = auto()


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    outcome: FinalizeOutcome
    payment: Payment | None = None
    expires_at: datetime | None = None


class PaymentService:
    def __init__(
        self,
        uow: UnitOfWork,
        providers: PaymentRegistry,
        subscriptions: SubscriptionService,
        settings: Settings,
    ) -> None:
        self._uow = uow
        self._providers = providers
        self._subscriptions = subscriptions
        self._settings = settings

    # --- creating -----------------------------------------------------

    async def create_invoice(
        self, telegram_id: int, tariff: Tariff, provider_name: str
    ) -> Payment:
        """Record the invoice, then ask the provider for a payment page."""
        provider = self._providers.get(provider_name)
        if provider is None:
            raise PaymentError(f'provider {provider_name!r} is not configured')

        payment_id = uuid.uuid4()
        ttl = self._settings.invoice_ttl_minutes
        invoice = await provider.create_invoice(
            payment_id,
            amount_kopeks=tariff.price_kopeks,
            description=f'Rillza VPN — {tariff.title_ru}',
            ttl_minutes=ttl,
        )

        payment = Payment(
            id=payment_id,
            user_id=telegram_id,
            tariff_id=tariff.id,
            provider=provider_name,
            status=PaymentStatus.PENDING,
            amount_kopeks=tariff.price_kopeks,
            provider_invoice_id=invoice.provider_invoice_id,
            invoice_url=invoice.url,
            invoice_expires_at=utcnow() + timedelta(minutes=ttl),
        )
        await self._uow.payments.add(payment)
        await self._uow.commit()
        logger.info(
            'Invoice {} created for user {} ({}, {} kopeks)',
            payment_id,
            telegram_id,
            provider_name,
            tariff.price_kopeks,
        )
        return payment

    # --- finalising ---------------------------------------------------

    async def check_and_finalize(
        self, payment_id: uuid.UUID
    ) -> FinalizeResult:
        """The single funnel from "invoice" to "access granted"."""
        payment = await self._uow.payments.lock_for_finalize(payment_id)
        if payment is None:
            # Either no such payment, or another worker holds the row.
            exists = await self._uow.payments.get(payment_id)
            outcome = (
                FinalizeOutcome.UNKNOWN
                if exists is None
                else FinalizeOutcome.BUSY
            )
            return FinalizeResult(outcome)

        if payment.status == PaymentStatus.PROVISIONED:
            return FinalizeResult(
                FinalizeOutcome.PROVISIONED, payment, payment.target_expires_at
            )
        if payment.status == PaymentStatus.PAID:
            return await self._provision(payment)
        if payment.status != PaymentStatus.PENDING:
            return FinalizeResult(FinalizeOutcome.EXPIRED, payment)

        provider = self._providers.get(payment.provider)
        if provider is None:
            return FinalizeResult(
                FinalizeOutcome.PROVIDER_UNAVAILABLE, payment
            )

        try:
            check = await provider.check_payment(
                payment.id, payment.provider_invoice_id
            )
        except PaymentError as error:
            logger.warning('Payment {} check failed: {}', payment.id, error)
            return FinalizeResult(
                FinalizeOutcome.PROVIDER_UNAVAILABLE, payment
            )

        if not check.is_paid:
            if check.status.value in ('expired', 'canceled'):
                await self._uow.payments.mark_expired(payment.id)
                await self._uow.commit()
                return FinalizeResult(FinalizeOutcome.EXPIRED, payment)
            return FinalizeResult(FinalizeOutcome.NOT_PAID, payment)

        paid = await self._mark_paid(payment, check)
        if paid is None:
            # Someone finalised it between our lock and now.
            return FinalizeResult(FinalizeOutcome.BUSY, payment)
        return await self._provision(paid)

    async def _mark_paid(self, payment: Payment, check) -> Payment | None:
        """Freeze the absolute target expiry together with the status."""
        now = utcnow()
        tariff = await self._uow.tariffs.get(payment.tariff_id)
        if tariff is None:  # pragma: no cover - FK guarantees it exists
            raise PaymentError(f'tariff {payment.tariff_id} vanished')

        subscription = await self._subscriptions.get(payment.user_id)
        base = now
        if subscription is not None and subscription.expires_at > now:
            # Renewing early must not throw away the remaining days.
            base = subscription.expires_at
        target = base + timedelta(days=tariff.duration_days)

        updated = await self._uow.payments.mark_paid(
            payment.id,
            paid_at=now,
            target_expires_at=target,
            paid_amount_kopeks=check.paid_amount_kopeks,
            paid_currency=check.paid_currency,
        )
        await self._uow.commit()
        if updated is not None:
            logger.info(
                'Payment {} confirmed for user {}, access until {}',
                payment.id,
                payment.user_id,
                target.isoformat(),
            )
        return updated

    async def _provision(self, payment: Payment) -> FinalizeResult:
        """Deliver the days recorded on the payment. Safe to repeat."""
        target = payment.target_expires_at
        if target is None:  # pragma: no cover - set together with 'paid'
            raise PaymentError(
                f'payment {payment.id} is paid without a target'
            )

        try:
            subscription = await self._subscriptions.get(payment.user_id)
            if subscription is None:
                subscription = await self._subscriptions.create_pending(
                    payment.user_id,
                    expires_at=target,
                    origin=SubscriptionOrigin.PURCHASE,
                )
                await self._subscriptions.provision(subscription)
            elif subscription.expires_at != target:
                await self._subscriptions.extend(subscription, target)
            elif subscription.subscription_token is None:
                await self._subscriptions.provision(subscription)
        except PanelError as error:
            logger.warning(
                'Payment {} is paid but the panel is unreachable: {}',
                payment.id,
                error,
            )
            return FinalizeResult(
                FinalizeOutcome.PAID_PENDING_PROVISIONING, payment, target
            )

        await self._uow.payments.mark_provisioned(payment.id, utcnow())
        await self._uow.commit()
        return FinalizeResult(FinalizeOutcome.PROVISIONED, payment, target)

    # --- background sweeps --------------------------------------------

    async def poll_pending(self) -> int:
        """Advance every live invoice. Returns how many were finalised."""
        finalised = 0
        for payment in await self._uow.payments.list_pending(utcnow()):
            result = await self.check_and_finalize(payment.id)
            if result.outcome is FinalizeOutcome.PROVISIONED:
                finalised += 1
        return finalised

    async def finish_provisioning(self) -> int:
        """Retry payments stuck at "money taken, access not delivered"."""
        finished = 0
        for payment in await self._uow.payments.list_awaiting_provisioning():
            result = await self.check_and_finalize(payment.id)
            if result.outcome is FinalizeOutcome.PROVISIONED:
                finished += 1
        return finished

    async def expire_stale(self) -> list[Payment]:
        """Close invoices whose time ran out, without touching paid money."""
        expired: list[Payment] = []
        for payment in await self._uow.payments.list_stale_pending(utcnow()):
            closed = await self._uow.payments.mark_expired(payment.id)
            if closed is not None:
                expired.append(closed)
        await self._uow.commit()
        return expired

    async def sweep_late_payments(self) -> list[Payment]:
        """Re-check yesterday's expired invoices once.

        Money does arrive after a 30-minute TTL. Without this it would
        sit unnoticed: the payment is closed, the user has no access, and
        nothing ever looks again.
        """
        now = utcnow()
        candidates = await self._uow.payments.list_recently_expired(
            now - timedelta(days=1), now
        )
        late: list[Payment] = []
        for payment in candidates:
            provider = self._providers.get(payment.provider)
            if provider is None:
                continue
            try:
                check = await provider.check_payment(
                    payment.id, payment.provider_invoice_id
                )
            except PaymentError:
                continue
            if not check.is_paid:
                continue

            logger.warning(
                'Late payment {} from user {} arrived after expiry',
                payment.id,
                payment.user_id,
            )
            # Reopen and run the normal path: one way to grant access.
            if await self._uow.payments.reopen(payment.id) is None:
                continue
            await self._uow.commit()
            await self.check_and_finalize(payment.id)
            late.append(payment)
        return late
