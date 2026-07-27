"""ORM models — the source of truth for users, money and access.

The CELERITY panel is an executor: everything it holds can be rebuilt
from these tables (see PLAN.md §2).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    BroadcastStatus,
    PaymentProvider,
    PaymentStatus,
    SubscriptionOrigin,
    SubscriptionStatus,
    SupportDirection,
)
from app.db.base import Base, TimestampMixin, enum_check, enum_text


class User(Base, TimestampMixin):
    """A Telegram user. The Telegram id is the primary key everywhere."""

    __tablename__ = 'users'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    # Set when Telegram reports the user blocked the bot (broadcasts).
    is_bot_blocked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default='false'
    )
    # Latch: the free trial is granted once per user, forever.
    trial_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Set when an admin bans the user from writing to support.
    support_blocked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # Derived state: let the database's ON DELETE CASCADE remove it.
    subscription: Mapped['Subscription | None'] = relationship(
        back_populates='user',
        uselist=False,
        cascade='all, delete-orphan',
        passive_deletes=True,
    )
    # Money records have no ON DELETE, so the FK refuses the delete
    # instead of the ORM quietly detaching them.
    payments: Mapped[list['Payment']] = relationship(
        back_populates='user', passive_deletes=True
    )

    @property
    def trial_used(self) -> bool:
        return self.trial_used_at is not None

    @property
    def support_blocked(self) -> bool:
        return self.support_blocked_at is not None


class Tariff(Base, TimestampMixin):
    """A purchasable duration. Prices are editable from the admin panel."""

    __tablename__ = 'tariffs'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    title_ru: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    # Kopeks: integer money only, never floats.
    price_kopeks: Mapped[int] = mapped_column(Integer, nullable=False)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default='0'
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default='true'
    )
    # Tariffs are archived, never deleted: payments reference them.
    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default='false'
    )

    __table_args__ = (
        Index('ix_tariffs_active_order', 'is_active', 'sort_order'),
    )

    @property
    def price_rubles(self) -> int:
        return self.price_kopeks // 100

    @property
    def monthly_price_kopeks(self) -> int:
        """Price per 30 days, for the "выгоднее на N%" label."""
        return round(self.price_kopeks / (self.duration_days / 30))


class Subscription(Base, TimestampMixin):
    """One subscription per user; the panel account it drives."""

    __tablename__ = 'subscriptions'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey('users.id', ondelete='CASCADE'),
        unique=True,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        enum_text(SubscriptionStatus), nullable=False
    )
    origin: Mapped[str] = mapped_column(
        enum_text(SubscriptionOrigin), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # CELERITY user id — always str(telegram_id).
    panel_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # Cached from the panel response; used to build the Happ link.
    subscription_token: Mapped[str | None] = mapped_column(String(64))
    provisioned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # '3d' / '1d' — guards against repeating an expiry reminder.
    notified_stage: Mapped[str | None] = mapped_column(String(4))

    user: Mapped[User] = relationship(back_populates='subscription')

    __table_args__ = (
        enum_check('status', SubscriptionStatus),
        enum_check('origin', SubscriptionOrigin),
        Index('ix_subscriptions_status_expires_at', 'status', 'expires_at'),
    )

    def is_active_at(self, moment: datetime) -> bool:
        return (
            self.status == SubscriptionStatus.ACTIVE
            and self.expires_at > moment
        )


class Payment(Base, TimestampMixin):
    """An immutable money record. ``id`` is the provider's label/payload."""

    __tablename__ = 'payments'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('users.id'), nullable=False
    )
    tariff_id: Mapped[int] = mapped_column(
        Integer, ForeignKey('tariffs.id'), nullable=False
    )
    provider: Mapped[str] = mapped_column(
        enum_text(PaymentProvider), nullable=False
    )
    status: Mapped[str] = mapped_column(
        enum_text(PaymentStatus), nullable=False
    )
    amount_kopeks: Mapped[int] = mapped_column(Integer, nullable=False)
    # What actually arrived, recorded for the record — never compared
    # against the price: p2p transfers arrive net of the sender's fee.
    paid_amount_kopeks: Mapped[int | None] = mapped_column(Integer)
    paid_currency: Mapped[str | None] = mapped_column(String(8))
    provider_invoice_id: Mapped[str | None] = mapped_column(String(128))
    # Frozen once when the payment turns "paid" so provisioning retries
    # can never add the duration twice.
    target_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    invoice_url: Mapped[str | None] = mapped_column(Text)
    invoice_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provisioned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    user: Mapped[User] = relationship(back_populates='payments')
    tariff: Mapped[Tariff] = relationship()

    __table_args__ = (
        enum_check('provider', PaymentProvider),
        enum_check('status', PaymentStatus),
        UniqueConstraint(
            'provider',
            'provider_invoice_id',
            name='uq_payments_provider_invoice_id',
        ),
        Index('ix_payments_status', 'status'),
        Index('ix_payments_user_id_created_at', 'user_id', 'created_at'),
    )


class Broadcast(Base, TimestampMixin):
    """A resumable admin broadcast; ``last_user_id`` is the cursor."""

    __tablename__ = 'broadcasts'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        enum_text(BroadcastStatus), nullable=False
    )
    last_user_id: Mapped[int | None] = mapped_column(BigInteger)
    sent: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default='0'
    )
    failed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default='0'
    )
    blocked: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default='0'
    )

    __table_args__ = (enum_check('status', BroadcastStatus),)


class SupportMessage(Base):
    """Maps a message in an admin's chat back to the user who wrote in.

    An admin replies to the card; the reply's ``reply_to_message_id``
    finds this row, which names the recipient (PLAN.md §12).
    """

    __tablename__ = 'support_messages'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('users.id', ondelete='CASCADE'), nullable=False
    )
    admin_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    admin_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    direction: Mapped[str] = mapped_column(
        enum_text(SupportDirection), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        enum_check('direction', SupportDirection),
        UniqueConstraint(
            'admin_chat_id',
            'admin_message_id',
            name='uq_support_messages_admin_chat_id_admin_message_id',
        ),
        Index('ix_support_messages_user_id', 'user_id'),
    )


class JobHeartbeat(Base):
    """Last successful run of each scheduled job (the dead-man switch)."""

    __tablename__ = 'job_heartbeats'

    job_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
