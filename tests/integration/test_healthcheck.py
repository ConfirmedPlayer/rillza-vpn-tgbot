"""Container liveness: is the scheduler still making progress?

`restart: unless-stopped` reacts only to a process that exited. A
coroutine that hangs without crashing leaves docker calling the
container healthy indefinitely while the bot answers nobody — and on a
VPS there is no one watching to notice.
"""

from datetime import UTC, datetime, timedelta

from app.core.jobs import HEALTHCHECK_QUIET, PAYMENT_POLLER, RECONCILER
from app.db.models import JobHeartbeat
from app.services.uow import UnitOfWork
from scripts.healthcheck import is_alive, latest_success

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def test_a_fresh_heartbeat_is_alive() -> None:
    assert is_alive(NOW - timedelta(seconds=30), NOW)


def test_silence_past_the_threshold_is_not() -> None:
    assert not is_alive(NOW - HEALTHCHECK_QUIET - timedelta(seconds=1), NOW)


def test_no_heartbeat_at_all_is_not_alive() -> None:
    """A fresh database before the first run. compose covers this with
    start_period, so it must not be reported as healthy by accident."""
    assert not is_alive(None, NOW)


async def test_it_reads_the_freshest_job_not_the_first(
    uow: UnitOfWork,
) -> None:
    """The slowest job runs every four hours, so asking about one named
    job would either flap or never fire. Any job proves the loop turns.
    """
    uow.session.add_all(
        [
            JobHeartbeat(
                job_name=RECONCILER, last_success_at=NOW - timedelta(hours=3)
            ),
            JobHeartbeat(
                job_name=PAYMENT_POLLER,
                last_success_at=NOW - timedelta(seconds=20),
            ),
        ]
    )
    await uow.commit()

    assert await latest_success(uow.session) == NOW - timedelta(seconds=20)
