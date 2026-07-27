"""Per-user rate limiting for anything a stranger can trigger."""

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
        full_key = f'{self._prefix}:{key}'
        count = await self._redis.incr(full_key)
        if count == 1:
            await self._redis.expire(full_key, window)
        return count <= limit


class AllowAllRateLimiter:
    """Used when no Redis is wired up (tests, local runs)."""

    async def allow(self, key: str, limit: int, window: int) -> bool:
        return True
