"""Operational guards that are cheap to state and expensive to lose."""

from datetime import UTC, datetime, timedelta

from app.bot.texts.admin import render_stats
from app.core.jobs import JOB_INTERVALS
from app.db.models import JobHeartbeat
from app.scheduler.jobs import LATE_PAYMENT_SWEEP, PAYMENT_POLLER, RECONCILER
from app.services.broadcast_service import STALE_AFTER


class _Stats:
    """The shape render_stats reads; only heartbeats matter here."""

    users = 0
    active_subscriptions = 0
    trial_subscriptions = 0
    expired_subscriptions = 0
    conversion_percent = 0
    trial_converted = 0
    trials_issued = 0
    revenue_day_kopeks = 0
    revenue_week_kopeks = 0
    revenue_month_kopeks = 0
    payments_awaiting_provisioning = 0

    def __init__(self, heartbeats):
        self.heartbeats = heartbeats


def _rendered(job_name: str, minutes_ago: int) -> str:
    now = datetime.now(UTC)
    beat = JobHeartbeat(
        job_name=job_name, last_success_at=now - timedelta(minutes=minutes_ago)
    )
    return render_stats(_Stats([beat]), None, now)


class TestHeartbeatFreshnessFollowsTheJob:
    """A single 60-minute threshold made healthy jobs look sick.

    The reconciler runs every four hours and the late-payment sweep once
    a day, so both were permanently ⚠️ and the operator learned to
    ignore the warning that is supposed to mean "this job died".
    """

    def test_a_four_hourly_job_is_healthy_after_an_hour(self) -> None:
        assert '✅ reconciler' in _rendered(RECONCILER, 60)

    def test_a_daily_job_is_healthy_after_five_hours(self) -> None:
        assert '✅ late_payment_sweep' in _rendered(LATE_PAYMENT_SWEEP, 300)

    def test_a_four_hourly_job_that_missed_two_runs_is_flagged(self) -> None:
        assert '⚠️ reconciler' in _rendered(RECONCILER, 60 * 9)

    def test_a_half_minute_job_is_flagged_after_an_hour(self) -> None:
        assert '⚠️ payment_poller' in _rendered(PAYMENT_POLLER, 60)

    def test_every_registered_job_declares_its_interval(self) -> None:
        """A job without an interval would fall back to guessing."""
        from app.scheduler import jobs

        names = {
            value
            for name, value in vars(jobs).items()
            if name.isupper() and isinstance(value, str)
        }
        assert names <= set(JOB_INTERVALS)


def test_every_job_is_registered_with_the_declared_interval() -> None:
    """Registration is a loop over JOB_INTERVALS, so a job dropped from
    the table stops running entirely — silently, in production."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.scheduler.jobs import JobRunner, register_jobs

    scheduler = AsyncIOScheduler()
    register_jobs(scheduler, JobRunner(None, None, None, None, None))

    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == set(JOB_INTERVALS)
    for name, interval in JOB_INTERVALS.items():
        assert jobs[name].trigger.interval == interval
        assert jobs[name].max_instances == 1


def test_a_live_broadcast_cannot_be_stolen_by_the_resumer() -> None:
    """The resumer runs every five minutes and checkpoints per page.

    If a run is called stale as soon as the interval elapses, a slow
    page (one long TelegramRetryAfter is enough) is picked up while its
    sender is still going, and both write to the same counters.
    """
    assert STALE_AFTER > JOB_INTERVALS['broadcast_resumer']
