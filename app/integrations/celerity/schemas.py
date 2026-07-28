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


class ServerGroup(PanelModel):
    """A node group; tariffs map to one of these."""

    id: str = Field(alias='_id')
    name: str
    #: The cap a user with ``maxDevices == 0`` inherits. ``GET /api/groups``
    #: deliberately answers with ids and names only, so this is meaningful
    #: only on a group nested inside a user payload.
    max_devices: int = Field(default=0, alias='maxDevices')


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
    #: Only ``GET /api/users`` and ``GET /api/users/{id}`` expand these.
    #: A create response, or the user inside a 409, carries bare ObjectId
    #: strings — see the validator below.
    groups: list[ServerGroup] = Field(default_factory=list)

    @field_validator('expire_at', mode='after')
    @classmethod
    def _as_utc(cls, value: datetime | None) -> datetime | None:
        """Panel timestamps are UTC; make naive values explicit."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @field_validator('groups', mode='before')
    @classmethod
    def _drop_unexpanded_groups(cls, value: object) -> object:
        """Keep expanded groups, drop the bare ids some routes return.

        Both shapes are normal, so neither may raise; a caller reads an
        empty list as "this response did not say", never as "no groups".
        """
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    @property
    def never_expires(self) -> bool:
        return self.expire_at is None

    @property
    def effective_device_limit(self) -> int:
        """The cap the panel will actually enforce for this account.

        Mirrors the panel's own rule (``effectiveDeviceLimit`` and the
        ``/auth`` gate): the account's own value wins unless it is 0, and
        0 falls back to the smallest positive limit among its groups.
        Both 0 and -1 end up meaning "no check runs at all" — the panel
        only counts devices when the resolved number is above zero.

        Meaningful only on a payload that expanded ``groups``; elsewhere
        it reports the account's own value, which is what we always send.
        """
        if self.max_devices != 0:
            return self.max_devices
        limits = [
            group.max_devices for group in self.groups if group.max_devices > 0
        ]
        return min(limits) if limits else 0


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


class NodeStatus(PanelModel):
    name: str = ''
    online: bool = False


class PanelStats(PanelModel):
    """Fleet health for the admin screen (scope stats:read)."""

    online_users: int = Field(default=0, alias='onlineUsers')
    nodes_list: list[NodeStatus] = Field(
        default_factory=list, alias='nodesList'
    )
    users: dict[str, int] = Field(default_factory=dict)
    nodes: dict[str, int] = Field(default_factory=dict)

    @property
    def nodes_total(self) -> int:
        return self.nodes.get('total', len(self.nodes_list))

    @property
    def nodes_online(self) -> int:
        return self.nodes.get(
            'online', sum(1 for node in self.nodes_list if node.online)
        )

    @property
    def offline_nodes(self) -> list[str]:
        """A node that drops out silently disappears from subscriptions."""
        return [node.name for node in self.nodes_list if not node.online]
