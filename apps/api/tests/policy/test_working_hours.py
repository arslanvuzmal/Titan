"""The window opens an hour before the market's working day, not at eight.

``8`` was one number for every market. It is an hour ahead of a nine-o'clock
working day, exactly on time for Germany's eight -- so no lead-in at all -- and
it closed at 17:00 in a Gulf market that works to 18:00. The number was right
for the market it was written for and silently wrong for the others, and nothing
recorded which market that was.

The tests that matter here are not "does the arithmetic work". They are: does
every market actually get its lead-in, does the floor stop the lead-in reaching
into the night, and is the derived window the one a campaign is *created* with
rather than a constant consulted at send time and overriding a human's edit.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo

import pytest
from titan.db.enums import Region
from titan.policy.schedule import (
    EARLIEST_SEND_HOUR,
    LEAD_IN_HOURS,
    REGION_SEND_DAYS,
    REGION_WORKING_HOURS,
    SUNDAY_TO_THURSDAY,
    default_window_for,
    describe_derivation,
    lead_in_start_hour,
    working_hours_for,
)


# ==========================================================================
# The lead-in
# ==========================================================================
@pytest.mark.parametrize("region", list(Region))
def test_every_market_opens_before_its_own_working_day(region: Region) -> None:
    """The point of the whole exercise. A cold approach gets one reliable pass
    at an inbox -- the first one -- and arriving after the working day has begun
    forfeits it."""
    hours = working_hours_for(region)
    window = default_window_for(region)

    assert window.start_hour < hours.start_hour, (
        f"{region.value} opens at {window.start_hour:02d}:00, which is not before "
        f"its working day at {hours.start_hour:02d}:00"
    )


@pytest.mark.parametrize("region", list(Region))
def test_the_lead_in_is_an_hour_wherever_the_floor_allows(region: Region) -> None:
    hours = working_hours_for(region)
    window = default_window_for(region)

    expected = max(hours.start_hour - LEAD_IN_HOURS, EARLIEST_SEND_HOUR)
    assert window.start_hour == expected


@pytest.mark.parametrize("region", list(Region))
def test_the_window_closes_when_the_market_stops_working(region: Region) -> None:
    """The lead-in is added at the front only. Extending the far end would mean
    sending after people have gone home, which is where a reply stops being
    likely and a complaint starts."""
    assert default_window_for(region).end_hour == working_hours_for(region).end_hour


def test_a_nine_to_five_market_opens_at_eight() -> None:
    assert default_window_for(Region.USA).start_hour == 8


def test_germany_opens_at_seven_because_it_starts_at_eight() -> None:
    """The case the single ``8`` got wrong. A European campaign sending at 08:00
    was not arriving early, it was arriving exactly as the working day began --
    the lead-in was zero and nothing said so."""
    assert working_hours_for(Region.EUROPE).start_hour == 8
    assert default_window_for(Region.EUROPE).start_hour == 7


def test_the_gulf_window_runs_to_six_because_the_gulf_does() -> None:
    """The other half of what one pair of numbers cost: an hour of the Middle
    East working day was unreachable."""
    assert default_window_for(Region.MIDDLE_EAST).end_hour == 18


# ==========================================================================
# The floor
# ==========================================================================
def test_the_lead_in_never_reaches_into_the_night() -> None:
    """A market entered with an early start must not open the window at 05:00.
    A timestamp that far outside working hours is itself a mark of automation."""
    for working_start in range(0, 24):
        assert lead_in_start_hour(working_start) >= EARLIEST_SEND_HOUR


def test_a_midnight_working_day_cannot_produce_a_negative_hour() -> None:
    assert lead_in_start_hour(0) >= 0


def test_the_floor_is_calibrated_to_the_earliest_market_not_picked_at_random() -> None:
    """No market today is actually held back by the floor -- Europe lands exactly
    on it. That is the point: it is set at the earliest hour any real market
    needs, so it constrains a future table edit without silently altering any
    market now."""
    earliest = min(
        hours.start_hour - LEAD_IN_HOURS for hours in REGION_WORKING_HOURS.values()
    )

    assert earliest == EARLIEST_SEND_HOUR, (
        "the floor no longer matches the earliest market; it is either clamping "
        "a real market silently or has drifted away from constraining anything"
    )


def test_a_market_starting_earlier_would_be_held_at_the_floor() -> None:
    """The clamp itself, exercised directly. No entry in the table reaches it
    today, so this is the only place its behaviour is pinned down."""
    assert lead_in_start_hour(EARLIEST_SEND_HOUR) == EARLIEST_SEND_HOUR
    assert lead_in_start_hour(EARLIEST_SEND_HOUR - 2) == EARLIEST_SEND_HOUR


# ==========================================================================
# The working week
# ==========================================================================
def test_the_middle_east_works_sunday_to_thursday() -> None:
    assert default_window_for(Region.MIDDLE_EAST).days == SUNDAY_TO_THURSDAY


def test_a_middle_east_window_is_shut_on_friday_and_open_on_sunday() -> None:
    """The failure a Monday-to-Friday default produces: sending on the two days
    the recipients are not working and skipping the two they are."""
    window = default_window_for(Region.MIDDLE_EAST)
    dubai = zoneinfo.ZoneInfo("Asia/Dubai")

    friday = dt.datetime(2026, 8, 7, 10, 0, tzinfo=dubai)
    sunday = dt.datetime(2026, 8, 9, 10, 0, tzinfo=dubai)

    assert friday.weekday() == 4 and sunday.weekday() == 6
    assert not window.is_open_at(friday)
    assert window.is_open_at(sunday)


def test_the_send_days_table_is_derived_not_restated() -> None:
    """Two tables that have to agree eventually will not."""
    for region, hours in REGION_WORKING_HOURS.items():
        assert REGION_SEND_DAYS[region] == hours.days


@pytest.mark.parametrize("region", list(Region))
def test_every_derived_window_is_usable(region: Region) -> None:
    """A window whose end is not after its start closes the campaign for good.
    Deriving one is exactly how a table edit could produce that silently."""
    assert default_window_for(region).is_usable


@pytest.mark.parametrize("region", list(Region))
def test_every_derived_window_satisfies_the_database_constraint(
    region: Region,
) -> None:
    """``ck_campaign_policies_send_window_ordered``. A derived value that cannot
    be stored fails at campaign creation, which is the worst place to find out."""
    window = default_window_for(region)
    assert 0 <= window.start_hour < window.end_hour <= 24


# ==========================================================================
# Showing the work
# ==========================================================================
def test_the_derivation_is_recoverable_only_because_it_is_recorded() -> None:
    """08:00 could be a 09:00 market with an hour's lead-in or a market that
    simply starts at eight. The stored hours cannot tell you which."""
    assert default_window_for(Region.USA).start_hour == 8
    assert working_hours_for(Region.EUROPE).start_hour == 8

    usa = describe_derivation(Region.USA)
    assert "09:00-17:00" in usa
    assert "opens 08:00" in usa


def test_no_market_is_currently_described_as_held_back() -> None:
    """The clamp note is truthful: it appears only when the floor actually moved
    the window, and today nothing does."""
    for region in Region:
        assert "earliest permitted hour" not in describe_derivation(region)
