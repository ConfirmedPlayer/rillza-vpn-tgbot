"""Container healthcheck: is the scheduler still making progress?

``restart: unless-stopped`` reacts only to a process that *exited*. A
coroutine that hangs without crashing leaves docker calling the
container healthy for as long as it likes, while the bot answers nobody
— and on a VPS there is no one at the keyboard to notice.

The scheduler already writes the cheapest possible proof of life: a
heartbeat per job, the fastest of them every 30 seconds. Reading the
freshest one from outside the hung process is the one check that a hang
cannot fake.

Note what this does and does not buy. Docker does **not** restart an
unhealthy container by itself — the state shows up in ``docker ps`` and
in the API, and something on the host has to act on it. See
docs/SETUP.md.
"""

import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jobs import HEALTHCHECK_QUIET
from app.core.settings import get_settings
from app.db.engine import build_engine
from app.db.models import JobHeartbeat


async def latest_success(session: AsyncSession) -> datetime | None:
    """When any job last finished. None if none ever has."""
    result = await session.execute(
        select(func.max(JobHeartbeat.last_success_at))
    )
    return result.scalar()


def is_alive(latest: datetime | None, now: datetime) -> bool:
    """Whether the scheduler has moved recently enough to be trusted.

    ``None`` is not alive: a database with no heartbeat at all has
    either never started a job or lost the table. compose covers the
    honest case of the first boot with ``start_period``.
    """
    if latest is None:
        return False
    return now - latest < HEALTHCHECK_QUIET


async def _check() -> int:
    engine = build_engine(get_settings())
    try:
        async with AsyncSession(engine) as session:
            latest = await latest_success(session)
    finally:
        await engine.dispose()

    if is_alive(latest, datetime.now(UTC)):
        return 0
    print(f'scheduler quiet since {latest}', file=sys.stderr)
    return 1


def main() -> int:
    try:
        return asyncio.run(_check())
    except Exception as error:
        print(f'healthcheck failed: {error!r}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
