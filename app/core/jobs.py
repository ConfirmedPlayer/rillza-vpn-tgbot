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
