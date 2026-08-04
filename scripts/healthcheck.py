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
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jobs import HEALTHCHECK_QUIET, stale_after
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


async def all_heartbeats(session: AsyncSession) -> Sequence[JobHeartbeat]:
    result = await session.execute(select(JobHeartbeat))
    return result.scalars().all()


def stalled(beats: Sequence[JobHeartbeat], now: datetime) -> list[str]:
    """Jobs that were running and then stopped, past their own threshold.

    :func:`is_alive` asks whether the event loop turns at all, and takes
    the freshest heartbeat of any job to answer it. That is the right
    question for a hang, and the wrong one for a single job that keeps
    failing: the two fastest jobs tick every 30 and 60 seconds and
    neither touches the panel, so they hold the container green while
    provisioning and reconciliation fail on every pass. Unattended,
    nothing else would say so.

    A job with no heartbeat at all is deliberately not stale — it has
    not had its first run yet, which compose covers with start_period.
    Counting it would make every container unhealthy for the minutes
    between boot and the slower jobs' first runs.

    The threshold is each job's own: :func:`stale_after` allows two
    missed runs, so a four-hourly reconciler is not called dead at four
    hours and one second.
    """
    return sorted(
        beat.job_name
        for beat in beats
        if beat.last_success_at is not None
        and now - beat.last_success_at > stale_after(beat.job_name)
    )


async def _check() -> int:
    engine = build_engine(get_settings())
    try:
        async with AsyncSession(engine) as session:
            beats = await all_heartbeats(session)
    finally:
        await engine.dispose()

    now = datetime.now(UTC)
    latest = max(
        (b.last_success_at for b in beats if b.last_success_at is not None),
        default=None,
    )
    if not is_alive(latest, now):
        print(f'scheduler quiet since {latest}', file=sys.stderr)
        return 1

    stale = stalled(beats, now)
    if stale:
        print(f'jobs stopped running: {", ".join(stale)}', file=sys.stderr)
        return 1
    return 0


def main() -> int:
    try:
        return asyncio.run(_check())
    except Exception as error:
        print(f'healthcheck failed: {error!r}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
