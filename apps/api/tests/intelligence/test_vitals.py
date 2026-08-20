"""Alarms on the pipeline's own pulse.

Every fault this system has had was found by a person going looking. None
announced itself, because each component was behaving exactly as written while
the whole had stopped: research failed 1,819 times against 2,005 starts and the
crawler, the workflow and the sweeper were all individually correct about it.

These tests are mostly about what must *not* fire. An alarm that cries wolf is
worse than no alarm, because it trains the person it exists to reach.
"""

from __future__ import annotations

import datetime as dt

from titan.intelligence.vitals import (
    DECLINE_DAYS,
    IDLE_ALARM_HOUR_UTC,
    MIN_RESEARCH_SAMPLE,
    RESEARCH_FAILURE_ALARM,
    RUNWAY_ALARM_DAYS,
    Vitals,
    check,
    render,
)

AFTERNOON = dt.datetime(2026, 8, 20, IDLE_ALARM_HOUR_UTC + 1, tzinfo=dt.UTC)
MORNING = dt.datetime(2026, 8, 20, 7, tzinfo=dt.UTC)


def vitals(**overrides) -> Vitals:
    """A healthy workspace. Each test breaks exactly one thing."""
    base = {
        "leads_in_hand": 400,
        "daily_send_capacity": 25,
        "mailboxes_sending": 2,
        "mailboxes_active": 2,
        "crawls_today": 300,
        "addresses_found_today": 90,
        "sends_today": 25,
        "human_replies_this_week": 1,
        "research_finished": 200,
        "research_failed": 10,
        "sends_by_day": (20, 22, 25),
        "now": AFTERNOON,
    }
    base.update(overrides)
    return Vitals(**base)


def codes(v: Vitals) -> set[str]:
    return {a.code for a in check(v)}


# ------------------------------------------------------------ silence is right


def test_a_healthy_pipeline_raises_nothing() -> None:
    """The baseline. If this ever fires, every test below is meaningless."""
    assert check(vitals()) == []


# --------------------------------------------------------- research is failing


def test_research_failing_is_caught() -> None:
    """The fault that ran for days without a word: 1,819 failures against
    2,005 starts, every component individually correct."""
    assert "research_failing" in codes(
        vitals(research_finished=2005, research_failed=1819)
    )


def test_ordinary_failure_is_not_an_alarm() -> None:
    """Sites time out, refuse robots, and resolve to nothing. An alarm on any
    failure at all is an alarm every hour of every day."""
    assert "research_failing" not in codes(
        vitals(research_finished=200, research_failed=30)
    )


def test_a_tiny_sample_cannot_raise_a_rate_alarm() -> None:
    """Planted violation: drop the sample floor and this fails.

    Two failures out of three is not a 67% failure rate. Paging on it is how an
    operator learns to ignore the pager.
    """
    assert "research_failing" not in codes(vitals(research_finished=3, research_failed=3))


def test_the_sample_floor_is_where_the_rest_of_the_system_puts_it() -> None:
    assert MIN_RESEARCH_SAMPLE >= 50


def test_an_unmeasured_rate_is_none_not_zero() -> None:
    """ "Not measured" and "measured and healthy" are different facts, and a
    dashboard that renders the first as the second is lying quietly."""
    assert vitals(research_finished=3, research_failed=0).research_failure_rate is None
    assert vitals(research_finished=200, research_failed=0).research_failure_rate == 0.0


# ---------------------------------------------------------------- running dry


def test_a_short_runway_is_caught_before_it_runs_out() -> None:
    """Three days, not one. An alarm on the last day is a report."""
    assert "runway_short" in codes(vitals(leads_in_hand=50, daily_send_capacity=25))


def test_a_full_tank_is_quiet() -> None:
    assert "runway_short" not in codes(vitals(leads_in_hand=400, daily_send_capacity=25))


def test_no_capacity_is_not_an_empty_tank() -> None:
    """Planted violation: return 0.0 instead of None from ``days_of_fuel`` and
    this fails.

    With nothing able to send, the reserve is not being consumed. Reporting
    zero days of fuel would read as a fire at the exact moment there is none --
    and the mailbox alarm below is the one that should fire instead.
    """
    v = vitals(daily_send_capacity=0, mailboxes_sending=0, leads_in_hand=0)

    assert v.days_of_fuel is None
    assert "runway_short" not in codes(v)
    assert "no_sending_capacity" in codes(v)


# ------------------------------------------------------------ the slow fade


def test_three_falling_days_are_caught() -> None:
    """The shape every stoppage in this system has taken. Sends went 36, 18, 5
    over three days and nothing said a word."""
    assert "sends_declining" in codes(vitals(sends_by_day=(36, 18, 5)))


def test_a_weekend_is_not_a_decline() -> None:
    """Planted violation: drop the zero-day requirement and this fails.

    Thursday 30, Friday 25, Saturday 0 is strictly falling and is not a fault.
    Campaigns run Monday to Friday, and a rule that cannot tell a Saturday from
    a collapse pages somebody every weekend -- by the third one nobody reads it.
    """
    assert "sends_declining" not in codes(vitals(sends_by_day=(30, 25, 0)))


def test_a_stop_to_zero_is_reported_by_the_alarm_that_can_explain_it() -> None:
    """The division of labour the rule above depends on. Falling to zero is not
    silently ignored -- it is ``idle_with_capacity``'s to report, and that alarm
    can say what to look at."""
    stopped = vitals(sends_by_day=(36, 18, 0), sends_today=0)

    assert "sends_declining" not in codes(stopped)
    assert "idle_with_capacity" in codes(stopped)


def test_a_single_bad_day_is_not_a_trend() -> None:
    assert "sends_declining" not in codes(vitals(sends_by_day=(30, 28, 5, 26)))


def test_a_flat_run_is_not_a_decline() -> None:
    """Strictly falling. Steady is the desired state, not a warning."""
    assert "sends_declining" not in codes(vitals(sends_by_day=(25, 25, 25)))


def test_too_little_history_says_nothing() -> None:
    assert "sends_declining" not in codes(vitals(sends_by_day=(30,)))
    assert DECLINE_DAYS >= 3


# ------------------------------------------------------- stopped without saying


def test_capacity_and_leads_and_nothing_sent_is_a_fault() -> None:
    """Allowance available, fuel available, nothing left. Something between an
    approved draft and the outbox is broken -- which is exactly the state 241
    approved-but-never-queued drafts were in."""
    assert "idle_with_capacity" in codes(vitals(sends_today=0))


def test_the_morning_is_not_a_fault() -> None:
    """Planted violation: drop the hour guard and this fails. Before the send
    windows have run, zero sends is a clock, not a fault."""
    assert "idle_with_capacity" not in codes(vitals(sends_today=0, now=MORNING))


def test_a_blocked_mailbox_does_not_also_raise_idleness() -> None:
    """One fault, one alarm. Reporting "nothing sent" alongside "every mailbox
    is blocked" is the same news twice, and the second is the diagnosis."""
    raised = codes(vitals(sends_today=0, mailboxes_sending=0, daily_send_capacity=0))

    assert "no_sending_capacity" in raised
    assert "idle_with_capacity" not in raised


def test_a_workspace_with_no_mailboxes_is_not_broken() -> None:
    """Nothing configured is a setup state, not a failure."""
    assert "no_sending_capacity" not in codes(
        vitals(mailboxes_active=0, mailboxes_sending=0, daily_send_capacity=0)
    )


# ------------------------------------------------------------ what it says


def test_an_alarm_carries_a_diagnosis_not_just_a_reading() -> None:
    """ "Research failure rate 91%" is a number. What gets somebody to the right
    place is being told where to look."""
    alarm = next(
        a
        for a in check(vitals(research_finished=2005, research_failed=1819))
        if a.code == "research_failing"
    )

    assert "browser worker" in alarm.detail
    assert len(alarm.detail) > 120, "a one-line alarm is a reading, not a diagnosis"


def test_the_reading_renders_unmeasured_as_unmeasured() -> None:
    text = render(vitals(research_finished=3, research_failed=0, daily_send_capacity=0))

    assert "too few to say" in text
    assert "nothing sending" in text


def test_the_reading_carries_all_six_numbers() -> None:
    text = render(vitals())

    for label in (
        "leads in hand",
        "days of fuel",
        "crawls today",
        "addresses today",
        "sends today",
        "replies this week",
    ):
        assert label in text


def test_thresholds_are_stated_not_scattered() -> None:
    """They are read by the alarms and by anyone deciding whether one was
    fair. A threshold inlined at its use site is one nobody can find."""
    assert 0 < RESEARCH_FAILURE_ALARM < 1
    assert RUNWAY_ALARM_DAYS > 1
    assert 0 <= IDLE_ALARM_HOUR_UTC <= 23


# ------------------------------------------- the provider refusing us entirely


def test_a_refusing_provider_is_the_first_thing_reported() -> None:
    """Planted violation: drop the provider alarm and this fails.

    Smartlead returned ``401 {"message": "Plan expired!"}`` for hours while
    every other number on the dashboard looked healthy -- the day's allowance
    had already been spent before the plan lapsed, so nothing tried to send and
    nothing failed. It would have surfaced at the first attempt the next
    morning, as a wall of send errors rather than as the one fact that
    explains them.
    """
    alarms = check(vitals(sending_provider_ok=False))

    assert alarms, "a provider refusing every send raised nothing"
    assert alarms[0].code == "sending_provider_rejected", (
        "it outranks every other alarm: the others describe a pipeline working "
        "badly, this one means no mail leaves at all"
    )


def test_a_working_provider_is_silent() -> None:
    assert check(vitals(sending_provider_ok=True)) == []


def test_an_unchecked_provider_does_not_alarm() -> None:
    """None is not False. A probe that could not run is not evidence of
    failure, and alarming on it pages somebody every time the network
    hiccups."""
    assert check(vitals(sending_provider_ok=None)) == []


def test_the_reading_distinguishes_unchecked_from_refusing() -> None:
    assert "not checked" in render(vitals(sending_provider_ok=None))
    assert "REFUSING" in render(vitals(sending_provider_ok=False))
    assert "ok" in render(vitals(sending_provider_ok=True))
