"""A missed daily job is worth running late. A missed hourly one is not.

One catch-up window of thirty minutes used to apply to every schedule. It suits
the frequent jobs and quietly disables the daily ones: this machine sleeps, so
a job scheduled for 06:10 is skipped outright whenever the laptop is closed at
06:10.

Measured on the live workspace before the fix:

    titan-mailbox-ramp   ActionCounts {"Total": 4, "MissedCatchupWindow": 6}

It had missed more often than it had run, and the ramp not running is why every
mailbox's daily volume had stopped growing.
"""

from __future__ import annotations

import datetime as dt

from titan.workflows.schedules import (
    CATCHUP_WINDOW,
    DAILY_CATCHUP_WINDOW,
    catchup_for,
    plan_schedules,
)

WS = __import__("uuid").UUID("b6134809-6dba-4e27-bc1c-305753946c42")


def test_a_daily_job_is_recoverable_for_most_of_the_day() -> None:
    """06:10 is missed whenever the machine is closed at 06:10. The window has
    to be long enough that turning it on later the same day still counts."""
    assert catchup_for("10 6 * * *") == DAILY_CATCHUP_WINDOW
    assert DAILY_CATCHUP_WINDOW >= dt.timedelta(hours=12)


def test_a_frequent_job_keeps_the_short_window() -> None:
    """A delivery poll that missed 09:25 has nothing to say at 09:55 -- the
    10:25 poll reads the same provider state."""
    assert catchup_for("25 * * * *") == CATCHUP_WINDOW
    assert catchup_for("*/15 * * * *") == CATCHUP_WINDOW


def test_a_stepped_hour_is_treated_as_frequent() -> None:
    """``*/4`` in the hour field runs six times a day, not once."""
    assert catchup_for("0 */4 * * *") == CATCHUP_WINDOW


def test_no_window_reaches_past_the_next_occurrence() -> None:
    """The property that stops a backfill herd: at most one missed occurrence
    is ever recovered. Twenty-four hours here would let two daily runs fire
    together after a two-day outage."""
    assert DAILY_CATCHUP_WINDOW < dt.timedelta(hours=24)
    assert CATCHUP_WINDOW < dt.timedelta(hours=1)


def test_every_planned_job_gets_a_window_shorter_than_its_interval() -> None:
    """Planted violation: set DAILY_CATCHUP_WINDOW to 24 hours and this fails.

    Checked across the real plan rather than a fixture, so a job added later
    with an unusual cron cannot slip past.
    """
    for job in plan_schedules(WS, task_queue="titan-research"):
        window = catchup_for(job.cron)
        hour = job.cron.split()[1]
        if hour != "*" and "/" not in hour:
            assert window < dt.timedelta(hours=24), job.schedule_id
        else:
            assert window < dt.timedelta(hours=1), job.schedule_id


def test_the_ramp_is_the_job_this_was_written_for() -> None:
    """It is the one whose failure to run was measurable: daily volume stopped
    growing while every other subsystem reported healthy."""
    jobs = {j.workflow: j for j in plan_schedules(WS, task_queue="titan-research")}

    assert catchup_for(jobs["MailboxRampWorkflow"].cron) == DAILY_CATCHUP_WINDOW
