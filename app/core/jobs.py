"""Background job names and how often each one runs.

This lives in core because two places need the same numbers: the
scheduler that registers the jobs, and the admin screen that decides
whether a job looks alive. The screen used to assume one hour for all of
them, so the reconciler (every four hours) and the late-payment sweep
(daily) were permanently marked ⚠️ — which taught the operator to ignore
the mark that is supposed to mean "this job died".
"""

from datetime import timedelta

PAYMENT_POLLER = 'payment_poller'
PROVISIONING_WATCHER = 'provisioning_watcher'
INVOICE_EXPIRER = 'invoice_expirer'
EXPIRY_SYNC = 'expiry_sync'
LATE_PAYMENT_SWEEP = 'late_payment_sweep'
EXPIRY_NOTIFIER = 'expiry_notifier'
RECONCILER = 'reconciler'
BROADCAST_RESUMER = 'broadcast_resumer'

#: How often the scheduler runs each job. register_jobs reads these, so
#: the interval and the health threshold cannot drift apart.
JOB_INTERVALS: dict[str, timedelta] = {
    PAYMENT_POLLER: timedelta(seconds=30),
    PROVISIONING_WATCHER: timedelta(seconds=60),
    INVOICE_EXPIRER: timedelta(minutes=5),
    EXPIRY_SYNC: timedelta(minutes=10),
    EXPIRY_NOTIFIER: timedelta(hours=1),
    BROADCAST_RESUMER: timedelta(minutes=5),
    RECONCILER: timedelta(hours=4),
    LATE_PAYMENT_SWEEP: timedelta(hours=24),
}

#: A job is only called late once it has missed this many runs. One
#: missed run is ordinary — a restart, a misfire, a pass that ran long.
#: Two in a row is a symptom, and waiting for a third would mean twelve
#: hours of silence from the reconciler before anyone was told.
MISSED_RUNS_BEFORE_STALE = 2

#: Below this, a job that is simply fast would flap between ✅ and ⚠️
#: on the round-trip to the admin screen.
MIN_STALE_AFTER = timedelta(hours=1)


def stale_after(job_name: str) -> timedelta:
    """How quiet a job may be before the admin screen flags it."""
    interval = JOB_INTERVALS.get(job_name, MIN_STALE_AFTER)
    return max(interval * MISSED_RUNS_BEFORE_STALE, MIN_STALE_AFTER)


#: The latest any job may have its first run after a start. Two things
#: depend on this number together, and they must not drift apart.
#:
#: The healthcheck reports a job that ran and then went quiet past its
#: own :func:`stale_after`. After downtime longer than that — a night
#: with the laptop shut, a pause between deployments — every heartbeat
#: in the database is already stale at boot, through no fault of the
#: scheduler. So compose's ``start_period`` has to outlast the slowest
#: first run: until then the container is "starting", not "unhealthy",
#: and by the time it is judged, every job has run once in this process.
#:
#: Get this wrong and a host watchdog restarts the container at the end
#: of start_period, before the slow jobs have run — and it never gets
#: further. tests/test_job_health.py pins both halves.
FIRST_RUN_LATEST = timedelta(minutes=5)

#: How long every job may be silent before the container calls itself
#: unhealthy. Deliberately tighter than :func:`stale_after`, which keeps
#: an admin screen from flapping between ✅ and ⚠️; this one answers a
#: different question — is the event loop turning at all. The two
#: fastest jobs tick every 30 and 60 seconds, so five minutes of total
#: silence is not a slow pass, it is a stopped scheduler.
HEALTHCHECK_QUIET = timedelta(minutes=5)
