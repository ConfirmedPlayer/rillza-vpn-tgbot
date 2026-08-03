"""In-memory stand-in for the CELERITY panel.

Mirrors the behaviours the bot depends on: create-or-fetch semantics,
absolute expiry, and a subscription token minted on creation. Failures
can be armed to exercise the degraded paths.
"""

import secrets
from datetime import datetime

from app.integrations.celerity.errors import (
    PanelNotFoundError,
    PanelUnavailableError,
)
from app.integrations.celerity.schemas import (
    NodeStatus,
    PanelHealth,
    PanelStats,
    PanelUser,
    ServerGroup,
    SubscriptionInfo,
    Traffic,
)

GROUP_ID = '65f0aa0000000000000000aa'


class FakePanel:
    """Implements the slice of CelerityClient the services call."""

    def __init__(self, group_name: str = 'Rillza') -> None:
        self.users: dict[str, PanelUser] = {}
        self.group_name = group_name
        #: Raise PanelUnavailableError from every call while set.
        self.offline = False
        self.calls: list[str] = []
        self.info_traffic = Traffic(used=0, limit=0)
        #: Flip to model a node dropping out of every subscription.
        self.all_nodes_online = True

    def _guard(self, call: str) -> None:
        self.calls.append(call)
        if self.offline:
            raise PanelUnavailableError('fake panel is offline')

    def _require(self, panel_user_id: str) -> PanelUser:
        """The real panel answers 404, not a KeyError."""
        user = self.users.get(panel_user_id)
        if user is None:
            raise PanelNotFoundError('Пользователь не найден', 404)
        return user

    async def resolve_group_id(self, refresh: bool = False) -> str:
        self._guard('resolve_group_id')
        return GROUP_ID

    async def list_groups(self) -> list[ServerGroup]:
        self._guard('list_groups')
        return [ServerGroup(_id=GROUP_ID, name=self.group_name)]

    async def get_user(self, panel_user_id: str) -> PanelUser | None:
        self._guard(f'get_user:{panel_user_id}')
        return self.users.get(panel_user_id)

    async def create_or_get_user(
        self,
        panel_user_id: str,
        expire_at: datetime | None,
        *,
        max_devices: int,
        username: str = '',
        group_id: str | None = None,
    ) -> tuple[PanelUser, bool]:
        self._guard(f'create_or_get_user:{panel_user_id}')
        existing = self.users.get(panel_user_id)
        if existing is not None:
            return existing, False
        user = PanelUser(
            userId=panel_user_id,
            username=username,
            enabled=True,
            expireAt=expire_at,
            trafficLimit=0,
            maxDevices=max_devices,
            subscriptionToken=secrets.token_hex(8),
        )
        self.users[panel_user_id] = user
        return user, True

    async def iter_users(
        self, page: int = 1, limit: int = 100
    ) -> tuple[list[PanelUser], int]:
        self._guard('iter_users')
        ordered = list(self.users.values())
        start = (page - 1) * limit
        return ordered[start : start + limit], len(ordered)

    async def set_state(
        self, panel_user_id: str, expire_at: datetime | None, max_devices: int
    ) -> PanelUser:
        self._guard(f'set_state:{panel_user_id}')
        user = self._require(panel_user_id)
        updated = user.model_copy(
            update={
                'expire_at': expire_at,
                'max_devices': max_devices,
                'enabled': True,
            }
        )
        self.users[panel_user_id] = updated
        return updated

    async def revoke(self, panel_user_id: str) -> PanelUser:
        self._guard(f'revoke:{panel_user_id}')
        user = self._require(panel_user_id)
        updated = user.model_copy(update={'enabled': False})
        self.users[panel_user_id] = updated
        return updated

    async def get_subscription_info(
        self, token: str
    ) -> SubscriptionInfo | None:
        self._guard(f'get_subscription_info:{token}')
        for user in self.users.values():
            if user.subscription_token == token:
                return SubscriptionInfo(
                    enabled=user.enabled,
                    expire=user.expire_at,
                    servers=3,
                    traffic=self.info_traffic,
                )
        return None

    async def stats(self) -> PanelStats:
        self._guard('stats')
        return PanelStats(
            onlineUsers=len(self.users),
            nodesList=[
                NodeStatus(name='nl-1', online=2),
                NodeStatus(name='nl-2', online=0),
            ],
            nodes={'total': 2, 'online': 2 if self.all_nodes_online else 1},
            users={'total': len(self.users), 'enabled': len(self.users)},
        )

    async def health(self) -> PanelHealth:
        self._guard('health')
        return PanelHealth(status='ok', isSyncing=False)

    async def sync(self) -> None:
        self._guard('sync')

    async def close(self) -> None:
        return None
