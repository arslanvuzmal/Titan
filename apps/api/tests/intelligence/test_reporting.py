"""Judging the week.

Pure. The thresholds here decide whether an operator is told to pause a domain,
so they are tested against a table of numbers rather than inferred from a
database fixture.
"""

from __future__ import annotations

import pytest
from titan.intelligence.reporting import (
    MIN_VOLUME_FOR_RATES,
    DeliverabilityHealth,
    Health,
    WeeklyReport,
    assess_deliverability,
    headline,
    render,
)


def report(**overrides) -> WeeklyReport:
    base = {
        "workspace_name": "Titan",
        "period_start": "2026-08-03",
        "period_end": "2026-08-10",
    }
    base.update(overrides)
    return WeeklyReport(**base)


# ==========================================================================
# Deliverability thresholds
# ==========================================================================


def test_a_low_volume_week_reports_no_rate_at_all():
    """One bounce in six is not a 17% bounce rate in any useful sense.

    Reporting it as critical is how a reader learns to ignore this line in the
    weeks before it is true.
    """
    health = assess_deliverability(sent=6, bounced=1, complained=0)

    assert health.status is Health.INSUFFICIENT_DATA
    assert not health.needs_action
    assert "too few" in health.detail


def test_healthy_sending_is_reported_as_healthy():
    health = assess_deliverability(sent=500, bounced=2, complained=0)

    assert health.status is Health.GOOD
    assert not health.needs_action


@pytest.mark.parametrize(
    ("sent", "bounced", "complained", "expected"),
    [
        # Gmail's bulk-sender ceiling is 0.30%. At or above it is a breach.
        (1000, 0, 3, Health.CRITICAL),
        # Gmail asks senders to stay under 0.10%. Above it is the quiet moment
        # before a breach, not a breach.
        (1000, 0, 1, Health.WARNING),
        (1000, 0, 0, Health.GOOD),
        # 5% bounce is where the damage takes months to undo.
        (100, 5, 0, Health.CRITICAL),
        # 2% is where providers begin throttling.
        (100, 2, 0, Health.WARNING),
        (100, 1, 0, Health.GOOD),
    ],
)
def test_thresholds_match_what_providers_actually_enforce(
    sent: int, bounced: int, complained: int, expected: Health
):
    assert (
        assess_deliverability(sent=sent, bounced=bounced, complained=complained).status
        is expected
    )


def test_complaints_outrank_bounces():
    """A complaint rate breach is more serious than a bounce rate one.

    Bounces cost reputation gradually; complaints are what gets a domain
    blocked outright, so the message must name the complaint problem even when
    both are bad.
    """
    health = assess_deliverability(sent=1000, bounced=100, complained=5)

    assert health.status is Health.CRITICAL
    assert "complaint" in health.detail.lower()


def test_the_boundary_is_inclusive():
    """Exactly 0.30% is a breach, not a near miss.

    An exclusive comparison here would let a sender sit permanently on the
    limit and report itself healthy.
    """
    assert (
        assess_deliverability(sent=1000, bounced=0, complained=3).status
        is Health.CRITICAL
    )
    assert MIN_VOLUME_FOR_RATES == 20


# ==========================================================================
# The rendered report
# ==========================================================================


def test_the_report_opens_with_what_needs_a_person():
    """A report that opens with "42 messages sent" trains the reader to skim.

    The number is the same most weeks and carries no decision.
    """
    text = render(
        report(
            awaiting_reply=3,
            messages_sent=42,
            replies_received=4,
            health=assess_deliverability(sent=42, bounced=0, complained=0),
        )
    )

    needs_you = text.index("NEEDS YOU")
    this_week = text.index("THIS WEEK")
    assert needs_you < this_week
    assert "3 prospect(s) said yes" in text


def test_a_quiet_week_says_so_plainly():
    text = render(report(messages_sent=10, replies_received=0))

    assert "Nothing needs you this week." in text


def test_hot_leads_are_named_with_how_long_they_have_waited():
    """An operator should be able to act without opening the CRM first."""
    text = render(
        report(
            awaiting_reply=2,
            hot_leads=("Bellrose Dental -- waiting since 2026-08-04",),
        )
    )

    assert "Bellrose Dental" in text
    assert "2026-08-04" in text


def test_a_critical_health_problem_appears_in_the_attention_block():
    text = render(
        report(
            messages_sent=1000,
            complained=5,
            health=assess_deliverability(sent=1000, bounced=0, complained=5),
        )
    )

    assert "NEEDS YOU" in text
    assert "Pause sending" in text


def test_the_reply_rate_is_the_closing_line_not_the_volume():
    """Volume only says the machine ran. The reply rate says whether the
    messages were any good, and it is the only number here worth moving."""
    text = render(
        report(
            messages_sent=200,
            replies_received=12,
            health=assess_deliverability(sent=200, bounced=1, complained=0),
        )
    )

    assert "Reply rate 6.0%" in text
    assert text.rstrip().endswith("volume only says the machine ran.")


def test_a_low_volume_week_gets_no_misleading_reply_rate_line():
    text = render(report(messages_sent=5, replies_received=1))

    # 20% off five sends is not a reply rate worth acting on.
    assert "Reply rate" not in text


# ==========================================================================
# The headline
# ==========================================================================


def test_the_headline_leads_with_deliverability_when_it_is_critical():
    text = headline(
        report(
            awaiting_reply=5,
            health=DeliverabilityHealth(Health.CRITICAL, 0.0, 0.01, "bad"),
        )
    )

    assert "deliverability" in text.lower()


def test_the_headline_otherwise_leads_with_waiting_prospects():
    text = headline(report(awaiting_reply=2, messages_sent=100))

    assert "2 prospect(s) waiting" in text


def test_the_headline_falls_back_to_volume_when_nothing_needs_doing():
    text = headline(report(messages_sent=40, replies_received=3))

    assert "40 sent" in text


def test_needs_attention_counts_a_health_problem_as_one_item():
    quiet = report(messages_sent=10)
    unhealthy = report(
        messages_sent=10,
        health=DeliverabilityHealth(Health.WARNING, 0.03, 0.0, "bounces up"),
    )

    assert quiet.needs_attention == 0
    assert unhealthy.needs_attention == 1


# ---------------------------------------------------------------------------
# Meetings and pipeline
# ---------------------------------------------------------------------------
def test_an_unscheduled_call_is_something_that_needs_a_person():
    """Every meeting starts without a time, so this is a standing item."""
    assert report(meetings_unscheduled=2).needs_attention == 2


def test_the_unscheduled_call_line_says_why_there_is_no_time():
    """Otherwise it reads like a bug rather than a deliberate refusal to guess."""
    body = render(report(meetings_unscheduled=1))

    assert "2 call" not in body
    assert "1 call(s) requested with no time set" in body
    assert "does not guess a time" in body


def test_calls_requested_are_reported_as_an_outcome():
    body = render(report(meetings_proposed=3, messages_sent=100))

    assert "Calls requested: 3" in body


def test_pipeline_value_is_stated_as_a_ceiling_not_a_forecast():
    """It is a sum of catalogue prices for work nobody has agreed to buy."""
    body = render(report(opportunities_identified=4, pipeline_value_usd=9400.0))

    assert "4 identified" in body
    assert "$9,400 if every one closed" in body


def test_no_opportunities_means_no_pipeline_line():
    """A zero-value line trains the reader to skip the section."""
    body = render(report(opportunities_identified=0, pipeline_value_usd=0.0))

    assert "Opportunities" not in body
