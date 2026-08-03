"""Payment queries.

Two rules shape this module, both about not losing money:

* the manual "проверить оплату" button and the background poller share
  :meth:`lock_for_finalize`, so a double tap cannot provision twice;
* every state change is a compare-and-set that RETURNs the updated row.
  Reading the ORM object after a plain UPDATE is unreliable — attributes
  that were never loaded stay stale, and touching them in async code
  raises MissingGreenlet — so the authoritative row comes back from the
  same statement.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Update, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import PaymentStatus
from app.db.models import Payment


def _returning(statement: Update) -> Update:
    return statement.returning(Payment).execution_options(
        synchronize_session=False, populate_existing=True
    )


class PaymentsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, payment_id: uuid.UUID) -> Payment | None:
        return await self._session.get(Payment, payment_id)

    async def add(self, payment: Payment) -> Payment:
        self._session.add(payment)
        await self._session.flush()
        return payment

    async def lock_for_finalize(self, payment_id: uuid.UUID) -> Payment | None:
        """Take the row with FOR UPDATE SKIP LOCKED.

        Returns None when another worker already holds it, so the caller
        can answer "проверяем…" instead of blocking behind an HTTP call.

        ``populate_existing`` is not optional here. Without it a SELECT
        that finds the object already in the identity map takes the row
        lock and hands back the *pre-lock* attribute values — the session
        does not commit (``expire_on_commit=False``), so a background
        sweep that loaded this payment minutes ago would decide
        idempotency from a snapshot taken before another worker latched
        it, and grant the days a second time.
        """
        result = await self._session.execute(
            select(Payment)
            .where(Payment.id == payment_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def mark_paid(
        self,
        payment_id: uuid.UUID,
        paid_at: datetime,
        target_expires_at: datetime,
        paid_amount_kopeks: int | None = None,
        paid_currency: str | None = None,
    ) -> Payment | None:
        """pending -> paid, freezing the absolute target expiry.

        Returns the updated payment, or None when the payment was not
        pending any more — only the first caller wins, and
        ``target_expires_at`` is never recomputed, so provisioning
        retries cannot add the duration twice.
        """
        result = await self._session.execute(
            _returning(
                update(Payment)
                .where(
                    Payment.id == payment_id,
                    Payment.status == PaymentStatus.PENDING,
                )
                .values(
                    status=PaymentStatus.PAID,
                    paid_at=paid_at,
                    target_expires_at=target_expires_at,
                    paid_amount_kopeks=paid_amount_kopeks,
                    paid_currency=paid_currency,
                )
            )
        )
        return result.scalar_one_or_none()

    async def mark_days_applied(
        self, payment_id: uuid.UUID, moment: datetime
    ) -> Payment | None:
        """Latch this payment's days as applied; a repeat gets None.

        Callers set it in the same transaction that writes the new
        subscription expiry, which is what makes provisioning idempotent
        without having to infer it from dates.
        """
        result = await self._session.execute(
            _returning(
                update(Payment)
                .where(
                    Payment.id == payment_id, Payment.days_applied_at.is_(None)
                )
                .values(days_applied_at=moment)
            )
        )
        return result.scalar_one_or_none()

    async def mark_provisioned(
        self, payment_id: uuid.UUID, moment: datetime
    ) -> Payment | None:
        """paid -> provisioned. Only a paid payment can be delivered."""
        result = await self._session.execute(
            _returning(
                update(Payment)
                .where(
                    Payment.id == payment_id,
                    Payment.status == PaymentStatus.PAID,
                )
                .values(
                    status=PaymentStatus.PROVISIONED, provisioned_at=moment
                )
            )
        )
        return result.scalar_one_or_none()

    async def mark_expired(self, payment_id: uuid.UUID) -> Payment | None:
        """pending -> expired. Never touches money already received."""
        result = await self._session.execute(
            _returning(
                update(Payment)
                .where(
                    Payment.id == payment_id,
                    Payment.status == PaymentStatus.PENDING,
                )
                .values(status=PaymentStatus.EXPIRED)
            )
        )
        return result.scalar_one_or_none()

    async def reopen(self, payment_id: uuid.UUID) -> Payment | None:
        """expired -> pending, for money that arrived after the TTL.

        Reopening funnels late payments back through the normal
        finalisation path instead of adding a second way to grant access.
        """
        result = await self._session.execute(
            _returning(
                update(Payment)
                .where(
                    Payment.id == payment_id,
                    Payment.status == PaymentStatus.EXPIRED,
                )
                .values(status=PaymentStatus.PENDING)
            )
        )
        return result.scalar_one_or_none()

    async def list_pending(self, moment: datetime) -> Sequence[Payment]:
        """Live invoices for the poller: pending and not past their TTL."""
        result = await self._session.execute(
            select(Payment).where(
                Payment.status == PaymentStatus.PENDING,
                Payment.invoice_expires_at > moment,
            )
        )
        return result.scalars().all()

    async def list_stale_pending(self, moment: datetime) -> Sequence[Payment]:
        result = await self._session.execute(
            select(Payment).where(
                Payment.status == PaymentStatus.PENDING,
                Payment.invoice_expires_at <= moment,
            )
        )
        return result.scalars().all()

    async def list_awaiting_provisioning(self) -> Sequence[Payment]:
        """Money taken, access not delivered — the retry queue."""
        result = await self._session.execute(
            select(Payment)
            .where(Payment.status == PaymentStatus.PAID)
            .order_by(Payment.paid_at)
        )
        return result.scalars().all()

    async def list_recently_expired(
        self, since: datetime, until: datetime
    ) -> Sequence[Payment]:
        """Expired invoices to re-check once for late money."""
        result = await self._session.execute(
            select(Payment).where(
                Payment.status == PaymentStatus.EXPIRED,
                Payment.invoice_expires_at >= since,
                Payment.invoice_expires_at < until,
            )
        )
        return result.scalars().all()

    async def list_by_user(
        self, telegram_id: int, limit: int = 20
    ) -> Sequence[Payment]:
        result = await self._session.execute(
            select(Payment)
            .where(Payment.user_id == telegram_id)
            .order_by(Payment.created_at.desc())
            .limit(limit)
        )
        return result.scalars().all()

    async def newest_applied_created_at(
        self, user_id: int, exclude: uuid.UUID
    ) -> datetime | None:
        """When this user's newest *applied* payment was invoiced.

        The device count is an assignment, not an addition, so unlike
        days it cannot be made order-independent: a payment swept up as
        late as seven days after the fact would write its own tariff's
        count over a newer purchase's.

        Ordered by ``created_at``, not ``paid_at``. ``paid_at`` records
        when the money was *noticed*, so late money carries a later
        stamp than the purchase that superseded it — sorting by it
        would invert exactly the case this guards. ``created_at`` is
        when the invoice was raised, which is the order the buyer
        pressed the buttons in.

        Covered by ix_payments_user_id_created_at.
        """
        result = await self._session.execute(
            select(func.max(Payment.created_at)).where(
                Payment.user_id == user_id,
                Payment.days_applied_at.is_not(None),
                Payment.id != exclude,
            )
        )
        return result.scalar_one_or_none()
