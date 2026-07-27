"""Panel payloads we care about.

The panel returns full Mongoose documents; these models keep the fields
the bot uses and ignore the rest, so a panel update that adds a field
cannot break parsing.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PanelModel(BaseModel):
    model_config = ConfigDict(extra='ignore', populate_by_name=True)


class Traffic(PanelModel):
    """Bytes. The user object carries tx/rx; /api/info adds used/limit."""

    tx: int = 0
    rx: int = 0
    used: int = 0
    # 0 means unlimited, which is what we sell.
    limit: int = 0

    @property
    def total_bytes(self) -> int:
        return self.used or (self.tx + self.rx)

    @property
    def unlimited(self) -> bool:
        return self.limit == 0


class PanelUser(PanelModel):
    """A panel account — one per Telegram user."""

    user_id: str = Field(alias='userId')
    username: str = ''
    enabled: bool = False
    # None means "never expires" in the panel.
    expire_at: datetime | None = Field(default=None, alias='expireAt')
    # Bytes; 0 means unlimited.
    traffic_limit: int = Field(default=0, alias='trafficLimit')
    # 0 = inherit the group's limit, -1 = unlimited.
    max_devices: int = Field(default=0, alias='maxDevices')
    # The credential in the public subscription link.
    subscription_token: str | None = Field(
        default=None, alias='subscriptionToken'
    )
    traffic: Traffic = Field(default_factory=Traffic)

    @field_validator('expire_at', mode='after')
    @classmethod
    def _as_utc(cls, value: datetime | None) -> datetime | None:
        """Panel timestamps are UTC; make naive values explicit."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @property
    def never_expires(self) -> bool:
        return self.expire_at is None


class ServerGroup(PanelModel):
    """A node group; tariffs map to one of these."""

    id: str = Field(alias='_id')
    name: str


class SubscriptionInfo(PanelModel):
    """Public per-token status, cheap enough for the "моя подписка" screen."""

    enabled: bool = False
    expire: datetime | None = None
    servers: int = 0
    traffic: Traffic = Field(default_factory=Traffic)

    @field_validator('expire', mode='after')
    @classmethod
    def _as_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class PanelHealth(PanelModel):
    status: str = ''
    uptime: float | None = None
    is_syncing: bool = Field(default=False, alias='isSyncing')
