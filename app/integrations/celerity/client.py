"""Client for the CELERITY panel.

Three rules from PLAN.md §8 are enforced here rather than left to
callers, because getting them wrong fails silently:

* a created user always carries ``enabled: true`` **and** its groups in
  the same POST — the panel pushes to Xray nodes only for an enabled
  user, and only from that request's group list, so anything else leaves
  working Hysteria entries next to dead VLESS ones;
* renewals send an **absolute** ``expireAt``, never "current + N days",
  so a retry cannot extend twice;
* revoking is ``enabled: false``. Deleting is not offered: the Hysteria
  password is derived from the user id, so a deleted-and-recreated
  account revives any leaked ``hysteria2://`` link.

``POST /api/users/{id}/enable`` is deliberately absent too — it
validates nothing and would hand an expired user a working VLESS.
"""

import asyncio
from datetime import datetime
from typing import Any

import aiohttp
from loguru import logger

from app.core.settings import Settings
from app.integrations.celerity.errors import (
    PanelAuthError,
    PanelConflictError,
    PanelError,
    PanelForbiddenError,
    PanelNotFoundError,
    PanelRateLimitedError,
    PanelUnavailableError,
)
from app.integrations.celerity.schemas import (
    PanelHealth,
    PanelStats,
    PanelUser,
    ServerGroup,
    SubscriptionInfo,
)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15)
#: Attempts per call, including the first one.
DEFAULT_ATTEMPTS = 3
#: Base of the exponential backoff, in seconds.
DEFAULT_BACKOFF = 1.0
#: Warn when the key's per-minute budget gets this low.
RATE_LIMIT_WARN_THRESHOLD = 10
#: Paths whose last segment is a secret and must not be logged.
SECRET_PATH_PREFIXES = ('/api/info/', '/api/files/')


class CelerityClient:
    """Async panel client with retries and typed errors."""

    def __init__(
        self,
        settings: Settings,
        session: aiohttp.ClientSession | None = None,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff: float = DEFAULT_BACKOFF,
    ) -> None:
        self._base_url = settings.panel_base_url
        self._api_key = settings.panel_api_key.get_secret_value()
        self._group_name = settings.panel_group_name
        self._session = session
        self._owns_session = session is None
        self._attempts = attempts
        self._backoff = backoff
        self._group_id: str | None = None

    # --- lifecycle ---------------------------------------------------

    async def __aenter__(self) -> 'CelerityClient':
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT)
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    # --- transport ---------------------------------------------------

    def _url(self, path: str) -> str:
        return f'{self._base_url}{path}'

    @staticmethod
    def _safe_path(path: str) -> str:
        """Hide the subscription token: it is the user's credential."""
        for prefix in SECRET_PATH_PREFIXES:
            if path.startswith(prefix):
                return f'{prefix}<token>'
        return path

    @staticmethod
    def _error_message(payload: Any, fallback: str) -> str:
        if isinstance(payload, dict):
            message = payload.get('error')
            if isinstance(message, str) and message:
                return message
        return fallback

    def _raise_for_status(
        self, status: int, payload: Any, headers: Any
    ) -> None:
        message = self._error_message(payload, f'HTTP {status}')
        if status == 401:
            raise PanelAuthError(message, status)
        if status == 403:
            raise PanelForbiddenError(message, status)
        if status == 404:
            raise PanelNotFoundError(message, status)
        if status == 409:
            raise PanelConflictError(message, status)
        if status == 429:
            raw = headers.get('Retry-After') if headers else None
            try:
                retry_after = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                retry_after = None
            raise PanelRateLimitedError(message, status, retry_after)
        if status >= 500:
            raise PanelUnavailableError(message, status)
        if status >= 400:
            raise PanelError(message, status)

    async def _request_once(
        self, method: str, path: str, json: Any | None, authorized: bool
    ) -> Any:
        session = self._ensure_session()
        headers = {'X-API-Key': self._api_key} if authorized else {}
        try:
            async with session.request(
                method, self._url(path), json=json, headers=headers
            ) as response:
                payload: Any
                try:
                    payload = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    payload = await response.text()

                self._check_budget(response.headers)
                self._raise_for_status(
                    response.status, payload, response.headers
                )
                return payload
        except TimeoutError as error:
            raise PanelUnavailableError(f'timeout: {error!r}') from error
        except aiohttp.ClientError as error:
            raise PanelUnavailableError(
                f'connection failed: {error!r}'
            ) from error

    @staticmethod
    def _check_budget(headers: Any) -> None:
        remaining = headers.get('X-RateLimit-Remaining') if headers else None
        if remaining is None:
            return
        try:
            left = int(remaining)
        except (TypeError, ValueError):
            return
        if left <= RATE_LIMIT_WARN_THRESHOLD:
            logger.warning(
                'CELERITY rate limit budget nearly spent: {} left', left
            )

    async def _request(
        self,
        method: str,
        path: str,
        json: Any | None = None,
        authorized: bool = True,
    ) -> Any:
        """Retry transient failures only; 4xx are answers, not glitches."""
        last_error: PanelError | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                return await self._request_once(method, path, json, authorized)
            except PanelRateLimitedError as error:
                last_error = error
                delay = error.retry_after or self._backoff * 2 ** (attempt - 1)
            except PanelUnavailableError as error:
                last_error = error
                delay = self._backoff * 2 ** (attempt - 1)

            if attempt == self._attempts:
                break
            logger.warning(
                'CELERITY {} {} failed ({}), retry {}/{} in {:.1f}s',
                method,
                self._safe_path(path),
                last_error,
                attempt,
                self._attempts - 1,
                delay,
            )
            await asyncio.sleep(delay)

        if last_error is None:  # pragma: no cover - unreachable
            raise PanelUnavailableError(f'{method} {path} made no attempts')
        raise last_error

    # --- groups ------------------------------------------------------

    async def list_groups(self) -> list[ServerGroup]:
        payload = await self._request('GET', '/api/groups')
        return [ServerGroup.model_validate(item) for item in payload]

    async def resolve_group_id(self, refresh: bool = False) -> str:
        """Group id for ``PANEL_GROUP_NAME``, resolved once and cached."""
        if self._group_id is not None and not refresh:
            return self._group_id
        for group in await self.list_groups():
            if group.name == self._group_name:
                self._group_id = group.id
                return group.id
        raise PanelNotFoundError(
            f'server group {self._group_name!r} not found on the panel'
        )

    # --- users -------------------------------------------------------

    async def get_user(self, panel_user_id: str) -> PanelUser | None:
        try:
            payload = await self._request('GET', f'/api/users/{panel_user_id}')
        except PanelNotFoundError:
            return None
        return PanelUser.model_validate(payload)

    async def create_or_get_user(
        self,
        panel_user_id: str,
        expire_at: datetime | None,
        *,
        max_devices: int,
        username: str = '',
        group_id: str | None = None,
    ) -> tuple[PanelUser, bool]:
        """Create the account, or return the existing one.

        Returns ``(user, created)``. A 409 carries the existing user in
        the body, which makes this a natural create-or-fetch.

        ``max_devices`` is keyword-only and has no default on purpose:
        a default would silently sell two devices to someone who paid
        for four.
        """
        body = {
            'userId': panel_user_id,
            'username': username,
            # Both of these must travel with the create request; see the
            # module docstring.
            'enabled': True,
            'groups': [group_id or await self.resolve_group_id()],
            'expireAt': _isoformat(expire_at),
            # We sell unlimited traffic; the device count is ours.
            'trafficLimit': 0,
            'maxDevices': max_devices,
        }
        try:
            payload = await self._request('POST', '/api/users', json=body)
        except PanelConflictError:
            existing = await self.get_user(panel_user_id)
            if existing is None:  # pragma: no cover - panel contradiction
                raise
            return existing, False
        return PanelUser.model_validate(payload), True

    async def set_state(
        self, panel_user_id: str, expire_at: datetime | None, max_devices: int
    ) -> PanelUser:
        """Push the two fields the database owns. Re-enables a lapsed user.

        ``expire_at`` is absolute — the caller computes
        ``max(now, current) + duration`` once and reuses it on retries.

        Both fields go in one request. Sending them separately would
        leave "expiry updated, limit not" between the two calls, and
        the panel is not transactional, so that state survives until
        the next reconcile.
        """
        payload = await self._request(
            'PUT',
            f'/api/users/{panel_user_id}',
            json={
                'expireAt': _isoformat(expire_at),
                'maxDevices': max_devices,
            },
        )
        return PanelUser.model_validate(payload)

    async def revoke(self, panel_user_id: str) -> PanelUser:
        """Block new connections without destroying the account."""
        payload = await self._request(
            'PUT', f'/api/users/{panel_user_id}', json={'enabled': False}
        )
        return PanelUser.model_validate(payload)

    async def iter_users(
        self, page: int = 1, limit: int = 100
    ) -> tuple[list[PanelUser], int]:
        """One page of accounts plus the total, for reconciliation."""
        payload = await self._request(
            'GET', f'/api/users?page={page}&limit={limit}'
        )
        users = [
            PanelUser.model_validate(item) for item in payload.get('users', [])
        ]
        total = int(payload.get('pagination', {}).get('total', len(users)))
        return users, total

    # --- public endpoints (no API key, no rate-limit budget) ---------

    async def get_subscription_info(
        self, token: str
    ) -> SubscriptionInfo | None:
        try:
            payload = await self._request(
                'GET', f'/api/info/{token}', authorized=False
            )
        except PanelNotFoundError:
            return None
        return SubscriptionInfo.model_validate(payload)

    async def stats(self) -> PanelStats:
        """Fleet counters, including which nodes are offline."""
        payload = await self._request('GET', '/api/stats')
        return PanelStats.model_validate(payload)

    async def health(self) -> PanelHealth:
        payload = await self._request('GET', '/health', authorized=False)
        return PanelHealth.model_validate(payload)

    # --- support -----------------------------------------------------

    async def sync(self) -> None:
        """Re-push configuration to the nodes.

        The panel's Xray pushes are fire-and-forget with no retry and no
        reconciliation cron, so this is the "не подключается?" lever.
        Expensive — call it behind a cooldown.
        """
        await self._request('POST', '/api/sync')


def _isoformat(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None
