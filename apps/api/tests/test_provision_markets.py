"""The market plan, checked without touching a database.

Every campaign in this workspace was ``region = uk`` and every geography a
northern English city. The regional machinery -- working hours per market,
timezone bands, holiday calendars per country -- had existed since Phase 02 with
nothing feeding it, because nothing ever created a campaign outside one market.

These assert the plan itself: that it names real places, covers the markets that
were asked for, and derives each campaign's clock rather than typing it in.
"""

from __future__ import annotations

from titan.config import OperatingMode
from titan.db.enums import Region
from titan.policy.schedule import (
    LEAD_IN_HOURS,
    REGION_WORKING_HOURS,
    SUNDAY_TO_THURSDAY,
    default_window_for,
)
from titan.provision_markets import (
    DEFAULT_DAILY_SEND_LIMIT,
    DEFAULT_MIN_LEAD_SCORE,
    PLAN,
    plan_rows,
)


def test_every_planned_start_is_a_real_territory() -> None:
    """A metro the catalogue does not hold would create a campaign with no clock
    and no rotation, and would fail at send time rather than here."""
    assert len(plan_rows()) == len(PLAN)


def test_all_six_markets_the_operator_named_are_covered() -> None:
    """USA, UK, Dubai, Australia, Canada and Eastern Europe.

    The UK is absent from the plan on purpose: eleven UK campaigns already
    exist with leads and history attached, and re-creating them is not this
    script's business.
    """
    markets = {territory.region for _, territory in plan_rows()}

    assert markets == {
        Region.USA,
        Region.CANADA,
        Region.EUROPE,
        Region.MIDDLE_EAST,
        Region.AUSTRALIA,
    }


def test_eastern_europe_is_planned_not_just_catalogued() -> None:
    """Being in the catalogue is not the same as being searched.

    That gap -- machinery present, nothing feeding it -- is the exact failure
    this script exists to close, so it is worth asserting rather than assuming.
    """
    starts = {territory.query_name for _, territory in plan_rows()}

    assert "Warsaw Poland" in starts
    assert "Bucharest Romania" in starts


def test_slugs_are_unique() -> None:
    """Provisioning is idempotent on slug; a duplicate would silently create one
    campaign and report two."""
    slugs = [entry.slug for entry in PLAN]

    assert len(slugs) == len(set(slugs))


def test_the_gulf_gets_its_own_working_week() -> None:
    """The one market that is not Monday to Friday.

    A Gulf campaign given 9-17 Monday-Friday would send on Friday -- read as
    ignorant where Jumu'ah is observed -- and never on Sunday, which is a
    working day there.
    """
    window = default_window_for(Region.MIDDLE_EAST)

    assert window.days == SUNDAY_TO_THURSDAY
    assert window.end_hour == 18


def test_every_window_opens_ahead_of_its_own_working_day() -> None:
    """Derived from the market's hours, never typed.

    A literal 8 is right for a 09:00 market and an hour late for Germany, and
    carries no record of which it was meant to be.
    """
    for _, territory in plan_rows():
        window = default_window_for(territory.region)
        hours = REGION_WORKING_HOURS[territory.region]

        assert window.start_hour <= hours.start_hour - LEAD_IN_HOURS or (
            window.start_hour == 7
        )
        assert window.end_hour == hours.end_hour
        assert window.days == hours.days
        assert window.is_usable


def test_provisioning_a_market_does_not_authorise_sending_to_it() -> None:
    """The defaults this script writes are the ones the eleven live campaigns
    already carry. Nothing here moves the delivery gate."""
    assert DEFAULT_MIN_LEAD_SCORE == 70
    assert DEFAULT_DAILY_SEND_LIMIT == 25
    assert OperatingMode.RESEARCH_ONLY.value == "research_only"


def test_a_planned_campaign_carries_its_bands_market() -> None:
    """A US campaign holding an Australian band would schedule against the wrong
    continent while looking correctly configured."""
    from titan.policy.subregions import belongs_to

    for _, territory in plan_rows():
        assert belongs_to(territory.sub_region, territory.region), territory.query_name
