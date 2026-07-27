"""Scriptable payment provider for tests."""

from uuid import UUID

from app.integrations.payments.base import (
    Invoice,
    PaymentCheck,
    PaymentError,
    ProviderStatus,
)


class FakeProvider:
    def __init__(self, name: str = 'yoomoney') -> None:
        self.name = name
        self.invoices: dict[str, Invoice] = {}
        #: Status returned by the next check, per payment id.
        self.statuses: dict[str, ProviderStatus] = {}
        self.default_status = ProviderStatus.PENDING
        self.offline = False
        self.checks: list[str] = []

    def mark_paid(self, payment_id) -> None:
        self.statuses[str(payment_id)] = ProviderStatus.PAID

    async def create_invoice(
        self,
        payment_id: UUID,
        amount_kopeks: int,
        description: str,
        ttl_minutes: int,
    ) -> Invoice:
        if self.offline:
            raise PaymentError('provider offline')
        invoice = Invoice(
            url=f'https://pay.example.com/{payment_id}',
            provider_invoice_id=f'inv-{payment_id}',
        )
        self.invoices[str(payment_id)] = invoice
        return invoice

    async def check_payment(
        self, payment_id: UUID, provider_invoice_id: str | None = None
    ) -> PaymentCheck:
        if self.offline:
            raise PaymentError('provider offline')
        self.checks.append(str(payment_id))
        status = self.statuses.get(str(payment_id), self.default_status)
        if status is ProviderStatus.PAID:
            return PaymentCheck(
                status, paid_amount_kopeks=19_800, paid_currency='RUB'
            )
        return PaymentCheck(status)

    async def close(self) -> None:
        return None
