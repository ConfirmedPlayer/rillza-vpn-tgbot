"""Per-user rate limiting for anything a stranger can trigger."""

from datetime import timedelta
from time import monotonic
from typing import Protocol

from redis.asyncio import Redis


class RateLimiter(Protocol):
    async def allow(self, key: str, limit: int, window: int) -> bool:
        """True when this hit fits inside ``limit`` per ``window`` seconds."""
        ...


class RedisRateLimiter:
    """A fixed window per key: INCR, and expire the counter with it."""

    def __init__(self, redis: Redis, prefix: str = 'rl') -> None:
        self._redis = redis
        self._prefix = prefix

    async def allow(self, key: str, limit: int, window: int) -> bool:
        """Count this hit and make sure the counter can expire.

        The two commands go in one transaction, and the TTL is set with
        NX rather than only on the first hit: a process that died
        between INCR and EXPIRE used to leave a counter with no TTL at
        all, which only ever grows — locking that user out of support
        for good.
        """
        full_key = f'{self._prefix}:{key}'
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(full_key)
            pipe.expire(full_key, window, nx=True)
            count, _ = await pipe.execute()
        return count <= limit


class AllowAllRateLimiter:
    """Used when no Redis is wired up (tests, local runs)."""

    async def allow(self, key: str, limit: int, window: int) -> bool:
        return True


class Cooldown:
    """One gate for an action that is expensive for the whole fleet.

    Deliberately in-process rather than in Redis: compose runs a single
    bot, the guarded action is an admin button, and a cooldown that
    forgets on restart is the safe way to be wrong.
    """

    def __init__(self, period: timedelta) -> None:
        self._period = period.total_seconds()
        self._last: float | None = None

    def claim(self) -> bool:
        """True when the caller may go ahead; starts the cooldown."""
        now = monotonic()
        if self._last is not None and now - self._last < self._period:
            return False
        self._last = now
        return True

    def release(self) -> None:
        """Give the turn back when the guarded action did not happen."""
        self._last = None
