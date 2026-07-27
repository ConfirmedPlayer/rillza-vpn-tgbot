"""Domain enumerations.

Stored as TEXT with CHECK constraints rather than native PostgreSQL
enums: adding a value stays a one-line migration instead of ALTER TYPE
ceremony, while the type safety lives in these classes.
"""

from enum import StrEnum


class SubscriptionStatus(StrEnum):
    PENDING = 'pending'
    ACTIVE = 'active'
    EXPIRED = 'expired'
    REVOKED = 'revoked'


class SubscriptionOrigin(StrEnum):
    TRIAL = 'trial'
    PURCHASE = 'purchase'
    ADMIN_GRANT = 'admin_grant'


class PaymentProvider(StrEnum):
    YOOMONEY = 'yoomoney'
    CRYPTOBOT = 'cryptobot'


class PaymentStatus(StrEnum):
    PENDING = 'pending'
    PAID = 'paid'
    PROVISIONED = 'provisioned'
    EXPIRED = 'expired'
    CANCELED = 'canceled'


class BroadcastStatus(StrEnum):
    DRAFT = 'draft'
    RUNNING = 'running'
    DONE = 'done'
    CANCELED = 'canceled'


class SupportDirection(StrEnum):
    IN = 'in'
    OUT = 'out'


class NotifiedStage(StrEnum):
    """Which expiry reminder was already sent for a subscription."""

    THREE_DAYS = '3d'
    ONE_DAY = '1d'


def values(enum_class: type[StrEnum]) -> list[str]:
    """Member values, for building CHECK constraints."""
    return [member.value for member in enum_class]
