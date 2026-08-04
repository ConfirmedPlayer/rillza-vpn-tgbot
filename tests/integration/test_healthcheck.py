"""Container liveness: is the scheduler still making progress?

`restart: unless-stopped` reacts only to a process that exited. A
coroutine that hangs without crashing leaves docker calling the
container healthy indefinitely while the bot answers nobody — and on a
VPS there is no one watching to notice.
"""

from datetime import UTC, datetime, timedelta

from app.core.jobs import (
    HEALTHCHECK_QUIET,
    LATE_PAYMENT_SWEEP,
    PAYMENT_POLLER,
    PROVISIONING_WATCHER,
    RECONCILER,
)
from app.db.models import JobHeartbeat
from app.services.uow import UnitOfWork
from scripts.healthcheck import (
    all_heartbeats,
    is_alive,
    latest_success,
    stalled,
)

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


class TestAJobThatStoppedWhileOthersKeptGoing:
    """The aggregate answer hides a single dead job.

    is_alive takes the freshest heartbeat of any job, and the two
    fastest tick every 30 and 60 seconds without ever touching the
    panel. So provisioning and reconciliation can fail on every pass
    while docker reports healthy — and unattended, nothing else says so.
    """

    async def test_a_dead_reconciler_is_reported(self, uow) -> None:
        uow.session.add_all(
            [
                JobHeartbeat(
                    job_name=PAYMENT_POLLER,
                    last_success_at=NOW - timedelta(seconds=20),
                ),
                JobHeartbeat(
                    job_name=RECONCILER,
                    last_success_at=NOW - timedelta(hours=9),
                ),
            ]
        )
        await uow.commit()
        beats = await all_heartbeats(uow.session)

        # The loop is turning, so the old question answers "fine".
        assert is_alive(await latest_success(uow.session), NOW)
        # The new one does not.
        assert stalled(beats, NOW) == [RECONCILER]

    async def test_a_slow_job_within_its_own_window_is_not_stale(
        self, uow
    ) -> None:
        """stale_after allows two missed runs, so a four-hourly job is
        not called dead at four hours and one second — the admin screen
        learned that lesson first, and this must not relearn it."""
        uow.session.add_all(
            [
                JobHeartbeat(
                    job_name=RECONCILER,
                    last_success_at=NOW - timedelta(hours=5),
                ),
                JobHeartbeat(
                    job_name=LATE_PAYMENT_SWEEP,
                    last_success_at=NOW - timedelta(hours=30),
                ),
            ]
        )
        await uow.commit()

        assert stalled(await all_heartbeats(uow.session), NOW) == []

    async def test_a_job_that_never_ran_is_not_stale(self, uow) -> None:
        """A fresh container between boot and the slower jobs' first
        runs. start_period covers it; calling it stale would make every
        start unhealthy for minutes."""
        uow.session.add_all(
            [
                JobHeartbeat(
                    job_name=PAYMENT_POLLER,
                    last_success_at=NOW - timedelta(seconds=10),
                ),
                JobHeartbeat(
                    job_name=PROVISIONING_WATCHER, last_success_at=None
                ),
            ]
        )
        await uow.commit()

        assert stalled(await all_heartbeats(uow.session), NOW) == []
