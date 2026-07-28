"""The payment provider port.

Providers answer with a closed status enum rather than a bool: without
EXPIRED and CANCELED the poller cannot stop chasing a dead invoice, and
every new provider would reinvent that logic.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID


class ProviderStatus(StrEnum):
    PENDING = 'pending'
    PAID = 'paid'
    EXPIRED = 'expired'
    CANCELED = 'canceled'


@dataclass(frozen=True, slots=True)
class Invoice:
    """What the user is sent to pay."""

    url: str
    #: The provider's own id, when it has one (CryptoBot). Stored for
    #: support and for the unique constraint on payments.
    provider_invoice_id: str | None = None


@dataclass(frozen=True, slots=True)
class PaymentCheck:
    """The outcome of asking a provider about one payment."""

    status: ProviderStatus
    #: What actually arrived. Recorded, never compared against the price:
    #: p2p transfers arrive net of the sender's fee.
    paid_amount_kopeks: int | None = None
    paid_currency: str | None = None

    @property
    def is_paid(self) -> bool:
        return self.status is ProviderStatus.PAID

    @property
    def is_final(self) -> bool:
        """True when no later check can change the answer."""
        return self.status in (
            ProviderStatus.PAID,
            ProviderStatus.EXPIRED,
            ProviderStatus.CANCELED,
        )


class PaymentError(Exception):
    """The provider could not be reached or answered nonsense."""


@runtime_checkable
class PaymentProvider(Protocol):
    """Every provider is addressed by our own payment id.

    That id is the label (YooMoney) or payload (CryptoBot), which makes
    lookups idempotent and keeps reconciliation possible without storing
    provider-specific state.
    """

    name: str

    async def create_invoice(
        self,
        payment_id: UUID,
        amount_kopeks: int,
        description: str,
        ttl_minutes: int,
    ) -> Invoice: ...

    async def check_payment(
        self, payment_id: UUID, provider_invoice_id: str | None = None
    ) -> PaymentCheck: ...

    async def describe_account(self) -> str:
        """Who the token belongs to, for a credentials check.

        Read-only by contract: it must never create or move money, so a
        misconfigured deployment can be diagnosed without issuing a real
        invoice to somebody.
        """
        ...

    async def close(self) -> None: ...
