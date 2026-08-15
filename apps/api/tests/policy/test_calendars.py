"""Public holidays, and the send window learning about them.

The send window already knew the working week. It did not know that the 25th of
December is not a working day, so a campaign sending on weekdays sent on
Christmas -- and a cold approach arriving on a public holiday is the message
that announces it was sent by something that did not know what day it was.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo

import pytest
from titan.db.enums import Region
from titan.policy.calendars import (
    REGION_COUNTRIES,
    clear_cache,
    holiday_on,
    is_working_day,
    resolve_country,
)
from titan.policy.engine import DenyCode, evaluate_send
from titan.policy.schedule import SendWindow

from .test_send_authorization import sendable_context

LONDON = zoneinfo.ZoneInfo("Europe/London")

#: Christmas Day 2026 falls on a Friday -- a working day by every other rule.
CHRISTMAS = dt.date(2026, 12, 25)


@pytest.fixture(autouse=True)
def _fresh_calendars():
    clear_cache()
    yield
    clear_cache()


# ==========================================================================
# The calendar itself
# ==========================================================================
def test_christmas_is_a_holiday_and_a_weekday() -> None:
    """The case a working-week rule alone gets wrong."""
    assert CHRISTMAS.weekday() == 4  # Friday
    assert holiday_on(CHRISTMAS, country="GB") == "Christmas Day"
    assert is_working_day(CHRISTMAS, country="GB") is False


def test_an_ordinary_weekday_is_not_a_holiday() -> None:
    assert holiday_on(dt.date(2026, 12, 22), country="GB") is None
    assert is_working_day(dt.date(2026, 12, 22), country="GB") is True


def test_a_substitute_day_is_recognised() -> None:
    """Boxing Day 2026 falls on a Saturday, so the UK observes it on the
    Monday. A hand-written table of fixed dates gets this wrong every few
    years without failing."""
    assert dt.date(2026, 12, 26).weekday() == 5  # Saturday
    assert "Boxing Day" in (holiday_on(dt.date(2026, 12, 28), country="GB") or "")


def test_markets_do_not_share_a_calendar() -> None:
    """US Thanksgiving is an ordinary Thursday in Britain."""
    thanksgiving = dt.date(2026, 11, 26)

    assert holiday_on(thanksgiving, country="US") == "Thanksgiving Day"
    assert holiday_on(thanksgiving, country="GB") is None


def test_the_holiday_is_named_not_just_flagged() -> None:
    """ "Waiting out Christmas Day" is a better answer than "outside the send
    window" for somebody reading a deferred queue in late December."""
    assert isinstance(holiday_on(CHRISTMAS, country="GB"), str)


# ==========================================================================
# Whose calendar applies
# ==========================================================================
def test_the_recipients_own_country_wins() -> None:
    assert resolve_country("de", Region.UK) == "DE"


def test_the_market_answers_where_it_names_one_country() -> None:
    assert resolve_country(None, Region.UK) == "GB"
    assert resolve_country(None, Region.USA) == "US"
    assert resolve_country("  ", Region.AUSTRALIA) == "AU"


def test_a_multi_country_market_resolves_to_nothing() -> None:
    """Europe and the Middle East each span a dozen calendars. Picking one
    would put a German business on French holidays."""
    assert resolve_country(None, Region.EUROPE) is None
    assert resolve_country(None, Region.MIDDLE_EAST) is None
    assert REGION_COUNTRIES[Region.EUROPE] is None


def test_no_country_means_no_holiday_blocking() -> None:
    """The behaviour before calendars existed, which is safe: it sends."""
    assert holiday_on(CHRISTMAS, country=None) is None
    assert is_working_day(CHRISTMAS, country=None) is True


def test_an_unrecognised_subdivision_falls_back_to_the_country() -> None:
    """Places returns administrative areas in whatever form a country uses, and
    most are not the ISO codes the library wants. Losing the state calendar is
    a small loss; losing the national one over a string mismatch is a large
    one."""
    assert holiday_on(CHRISTMAS, country="GB", subdiv="Greater London") == "Christmas Day"
    assert holiday_on(CHRISTMAS, country="AU", subdiv="not-a-state") is not None


def test_an_unknown_country_does_not_raise() -> None:
    assert holiday_on(CHRISTMAS, country="ZZ") is None


# ==========================================================================
# The window
# ==========================================================================
def gb_lookup(day: dt.date) -> str | None:
    return holiday_on(day, country="GB")


def test_a_window_without_a_calendar_opens_on_christmas() -> None:
    """The state this replaced, asserted so the fix is visibly a fix."""
    noon = dt.datetime(2026, 12, 25, 10, 0, tzinfo=LONDON)
    assert SendWindow().is_open_at(noon) is True


def test_a_window_with_a_calendar_is_closed_on_christmas() -> None:
    noon = dt.datetime(2026, 12, 25, 10, 0, tzinfo=LONDON)
    assert SendWindow().is_open_at(noon, gb_lookup) is False


def test_the_next_open_day_skips_the_holiday_the_weekend_and_the_substitute() -> None:
    """Christmas Friday, then Saturday and Sunday, then Boxing Day observed on
    the Monday. The first working morning is Tuesday."""
    christmas = dt.datetime(2026, 12, 25, 10, 0, tzinfo=LONDON)

    opens = SendWindow().next_open_from(christmas, gb_lookup)

    assert opens is not None
    assert opens.date() == dt.date(2026, 12, 29)
    assert opens.hour == 8


def test_a_holiday_does_not_extend_a_window_that_was_already_open() -> None:
    ordinary = dt.datetime(2026, 12, 22, 10, 0, tzinfo=LONDON)
    assert SendWindow().next_open_from(ordinary, gb_lookup) == ordinary


# ==========================================================================
# The send gate
# ==========================================================================
def windowed(**overrides):
    base = {
        "now": dt.datetime(2026, 12, 25, 10, 0, tzinfo=dt.UTC),
        "respect_quiet_hours": True,
        "send_window": SendWindow(),
        "campaign_region": Region.UK,
        "recipient_timezone": "Europe/London",
    }
    base.update(overrides)
    base.setdefault("approval_expires_at", base["now"] + dt.timedelta(days=2))
    return sendable_context(**base)


def test_a_message_is_refused_on_a_public_holiday() -> None:
    decision = evaluate_send(windowed())

    assert DenyCode.OUTSIDE_SEND_WINDOW in decision.codes
    assert "Christmas Day" in decision.reason_text()


def test_the_same_hour_a_week_earlier_sends() -> None:
    """The control. Without it the test above passes for a campaign that can
    never send at all."""
    ordinary = dt.datetime(2026, 12, 18, 10, 0, tzinfo=dt.UTC)
    assert DenyCode.OUTSIDE_SEND_WINDOW not in evaluate_send(windowed(now=ordinary)).codes


def test_a_market_with_no_calendar_still_sends_on_that_day() -> None:
    """A Europe campaign has no single calendar, so nothing blocks. Honest, and
    the same behaviour as before this existed."""
    decision = evaluate_send(
        windowed(campaign_region=Region.EUROPE, recipient_timezone="Europe/Berlin")
    )
    assert DenyCode.OUTSIDE_SEND_WINDOW not in decision.codes


def test_the_recipients_country_beats_the_campaigns_market() -> None:
    """A German business inside a UK campaign keeps German holidays. The 26th
    is Boxing Day in Britain and the second Christmas day in Germany, so this
    picks a date where the two genuinely differ."""
    us_only = dt.datetime(2026, 11, 26, 15, 0, tzinfo=dt.UTC)  # Thanksgiving

    british = evaluate_send(
        windowed(now=us_only, recipient_country="GB", recipient_timezone="Europe/London")
    )
    american = evaluate_send(
        windowed(
            now=us_only,
            recipient_country="US",
            recipient_timezone="America/New_York",
        )
    )

    assert DenyCode.OUTSIDE_SEND_WINDOW not in british.codes
    assert DenyCode.OUTSIDE_SEND_WINDOW in american.codes
    assert "Thanksgiving" in american.reason_text()
