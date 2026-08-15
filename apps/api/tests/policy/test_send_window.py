"""Per-campaign working hours, in the recipient's own time.

Two failures are being defended against, and they are opposite. Sending outside
somebody's working day -- which the old global quiet-hours window permitted at
14:00 on a Sunday, because it modelled night rather than work. And refusing every
lead whose location Places never resolved, which is what failing closed on an
unknown timezone quietly did.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo

import pytest
from titan.db.enums import Region
from titan.policy.engine import DenyCode, evaluate_send
from titan.policy.schedule import (
    MONDAY_TO_FRIDAY,
    REGION_SEND_DAYS,
    REGION_TIMEZONES,
    SUNDAY_TO_THURSDAY,
    SendWindow,
    default_window_for,
    local_time,
    resolve_timezone,
)

from .test_send_authorization import fully_authorized_settings, sendable_context

#: A Monday, so the default window is open unless a test closes it.
MONDAY_NOON_UTC = dt.datetime(2026, 8, 3, 12, 0, tzinfo=dt.UTC)
SATURDAY_NOON_UTC = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)


def windowed(**overrides):
    """A context with the schedule actually switched on.

    The delivery fixtures set respect_quiet_hours=False, which is why every
    existing test passes without noticing this check exists.
    """
    base = {
        "now": MONDAY_NOON_UTC,
        "respect_quiet_hours": True,
        "send_window": SendWindow(),
        "campaign_region": Region.UK,
        "recipient_timezone": "Europe/London",
    }
    base.update(overrides)
    # The builder's approval expiry is relative to its own NOW, and several
    # tests here move the clock days forward. Without this they would be
    # measuring an expired approval rather than the send window.
    base.setdefault("approval_expires_at", base["now"] + dt.timedelta(days=2))
    return sendable_context(**base)


def window_denials(ctx) -> list[DenyCode]:
    return [
        d.code
        for d in evaluate_send(ctx).denials
        if d.code in (DenyCode.OUTSIDE_SEND_WINDOW, DenyCode.QUIET_HOURS)
    ]


# ==========================================================================
# The window itself
# ==========================================================================
def test_a_working_hour_on_a_working_day_is_open() -> None:
    assert window_denials(windowed()) == []


def test_the_middle_of_saturday_afternoon_is_refused() -> None:
    """The case the old rule allowed. 14:00 on a weekend clears any quiet-hours
    check and is still a cold approach arriving in somebody's weekend."""
    assert window_denials(windowed(now=SATURDAY_NOON_UTC)) == [
        DenyCode.OUTSIDE_SEND_WINDOW
    ]


def test_the_end_hour_is_exclusive() -> None:
    """A window ending "at five" that sends at 17:45 is the kind of small wrong
    that reads as automation."""
    at_five = MONDAY_NOON_UTC.replace(hour=16)  # 17:00 London, BST
    assert window_denials(windowed(now=at_five)) == [DenyCode.OUTSIDE_SEND_WINDOW]


def test_a_refusal_here_is_temporary_not_fatal() -> None:
    """The message waits for Monday; it is not thrown away."""
    from titan.delivery.outbox_worker import OutboxWorker
    from titan.delivery.providers.mock import MockEmailProvider

    decision = evaluate_send(windowed(now=SATURDAY_NOON_UTC))
    worker = OutboxWorker(MockEmailProvider())

    assert worker._is_temporary(decision) is True


# ==========================================================================
# Two markets, one process
# ==========================================================================
def test_one_moment_is_inside_one_market_and_outside_another() -> None:
    """The whole reason a single global window could not work. 08:30 in London
    is 00:30 in Los Angeles, and no pair of integers is right for both."""
    moment = dt.datetime(2026, 8, 3, 7, 30, tzinfo=dt.UTC)  # 08:30 BST

    london = windowed(now=moment, recipient_timezone="Europe/London")
    pacific = windowed(
        now=moment,
        recipient_timezone="America/Los_Angeles",
        campaign_region=Region.USA,
    )

    assert window_denials(london) == []
    assert window_denials(pacific) == [DenyCode.OUTSIDE_SEND_WINDOW]


def test_the_recipients_own_timezone_beats_the_campaign_market() -> None:
    """The region is a fact about the campaign; the timezone is a fact about
    them. A business in Perth inside a UK campaign is still in Perth."""
    assert resolve_timezone("Australia/Perth", Region.UK) == "Australia/Perth"


def test_the_market_answers_when_the_recipient_cannot() -> None:
    """The improvement over failing closed: a campaign that declares its market
    can schedule for a lead whose location was never resolved."""
    assert resolve_timezone(None, Region.UK) == "Europe/London"
    assert resolve_timezone("  ", Region.AUSTRALIA) == "Australia/Sydney"


def test_a_lead_with_no_timezone_in_a_declared_market_still_sends() -> None:
    ctx = windowed(recipient_timezone=None, campaign_region=Region.UK)
    assert window_denials(ctx) == []


def test_a_lead_with_no_timezone_and_no_market_is_refused() -> None:
    """Still fails closed where nothing can answer. Guessing at a local hour is
    how mail goes out at 3am."""
    ctx = windowed(recipient_timezone=None, campaign_region=Region.UNSPECIFIED)

    assert window_denials(ctx) == [DenyCode.OUTSIDE_SEND_WINDOW]
    assert resolve_timezone(None, Region.UNSPECIFIED) is None


# ==========================================================================
# The working week
# ==========================================================================
def test_the_gulf_working_week_is_the_one_that_differs() -> None:
    assert REGION_SEND_DAYS[Region.MIDDLE_EAST] == SUNDAY_TO_THURSDAY
    assert all(
        REGION_SEND_DAYS[r] == MONDAY_TO_FRIDAY
        for r in (Region.USA, Region.CANADA, Region.UK, Region.EUROPE, Region.AUSTRALIA)
    )


def test_a_gulf_campaign_sends_on_sunday_and_not_on_friday() -> None:
    dubai = SendWindow(days=SUNDAY_TO_THURSDAY)
    tz = zoneinfo.ZoneInfo("Asia/Dubai")

    sunday = dt.datetime(2026, 8, 9, 10, 0, tzinfo=tz)
    friday = dt.datetime(2026, 8, 14, 10, 0, tzinfo=tz)

    assert dubai.is_open_at(sunday) is True
    assert dubai.is_open_at(friday) is False
    # And the inverse for a Monday-to-Friday campaign, which is the mistake.
    assert SendWindow().is_open_at(sunday) is False
    assert SendWindow().is_open_at(friday) is True


def test_every_market_has_a_default_window() -> None:
    for region in Region:
        assert default_window_for(region).is_usable


# ==========================================================================
# Bad configuration is reported, not repaired
# ==========================================================================
@pytest.mark.parametrize(
    "window",
    [
        SendWindow(start_hour=17, end_hour=8),
        SendWindow(start_hour=9, end_hour=9),
        SendWindow(days=()),
        SendWindow(start_hour=-1, end_hour=17),
        SendWindow(start_hour=8, end_hour=25),
    ],
)
def test_an_unusable_window_refuses_rather_than_guessing(window: SendWindow) -> None:
    """Guessing at what somebody meant by 17:00 to 08:00 would be inventing a
    policy on their behalf."""
    assert window.is_usable is False
    assert window_denials(windowed(send_window=window)) == [DenyCode.OUTSIDE_SEND_WINDOW]


def test_a_bad_timezone_string_does_not_fall_back_to_utc() -> None:
    """Falling back to UTC looks like an answer and is wrong by up to eleven
    hours."""
    assert local_time(MONDAY_NOON_UTC, "Not/AZone") is None
    assert local_time(MONDAY_NOON_UTC, "") is None


# ==========================================================================
# The global quiet hours, underneath
# ==========================================================================
def test_no_window_configured_leaves_the_old_rule_in_charge() -> None:
    ctx = windowed(
        send_window=None,
        now=dt.datetime(2026, 8, 3, 2, 0, tzinfo=dt.UTC),
        settings=fully_authorized_settings(quiet_hours_enabled=True),
    )
    assert window_denials(ctx) == [DenyCode.QUIET_HOURS]


def test_only_one_reason_is_reported_when_both_apply() -> None:
    """3am on a Sunday is outside both. Saying so twice tells an operator
    nothing the first line did not."""
    three_am_sunday = dt.datetime(2026, 8, 9, 2, 0, tzinfo=dt.UTC)

    assert window_denials(windowed(now=three_am_sunday)) == [DenyCode.OUTSIDE_SEND_WINDOW]


def test_the_schedule_can_be_switched_off_entirely() -> None:
    ctx = windowed(now=SATURDAY_NOON_UTC, respect_quiet_hours=False)
    assert window_denials(ctx) == []


# ==========================================================================
# Region timezones
# ==========================================================================
def test_every_schedulable_market_names_a_real_zone() -> None:
    for region, name in REGION_TIMEZONES.items():
        if name is None:
            assert region in (Region.OTHER, Region.UNSPECIFIED)
            continue
        assert zoneinfo.ZoneInfo(name) is not None
