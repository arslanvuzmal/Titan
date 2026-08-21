"""Which carrier a message rides, and therefore what hour it lands at.

The carrier campaign holds the clock. Getting this wrong does not produce a
slightly worse message -- it produces one that arrives at midnight, or on a
Friday in the Gulf, from a mailbox whose reputation took three weeks to build.

These prove the ordering: the recipient's own market wins whenever it is known
and served, and every other path falls back to what the campaign already said
rather than substituting a market that happened to be reachable.
"""

from __future__ import annotations

import pytest
from titan.db.enums import Region
from titan.delivery.carrier_routing import UNROUTABLE, CarrierMap, route_for_market

#: The six markets as they are actually provisioned on the live account.
CARRIERS = CarrierMap(
    by_clock={},
    by_market={
        Region.UK: "3809755",
        Region.USA: "3809757",
        Region.CANADA: "3809758",
        Region.EUROPE: "3809759",
        Region.MIDDLE_EAST: "3809760",
        Region.AUSTRALIA: "3809761",
    },
)


def route(
    country: str | None,
    *,
    carriers: CarrierMap = CARRIERS,
    timezone: str | None = None,
    campaign: int | None = 3770052,
):
    return route_for_market(
        carriers=carriers,
        recipient_country_code=country,
        recipient_timezone=timezone,
        campaign_carrier_id=campaign,
    )


# ------------------------------------------------- the recipient's own market


@pytest.mark.parametrize(
    ("country", "expected_region", "expected_id"),
    [
        ("GB", Region.UK, "3809755"),
        ("US", Region.USA, "3809757"),
        ("AE", Region.MIDDLE_EAST, "3809760"),
        ("AU", Region.AUSTRALIA, "3809761"),
        ("NL", Region.EUROPE, "3809759"),
        ("CA", Region.CANADA, "3809758"),
    ],
)
def test_the_recipient_market_chooses_the_carrier(
    country, expected_region, expected_id
) -> None:
    decision = route(country)

    assert decision.campaign_id == expected_id
    assert decision.region is expected_region
    assert decision.routed_by_market


def test_a_dubai_recipient_does_not_ride_the_london_clock() -> None:
    """Planted violation: read the id off the campaign instead and this fails.

    The campaign here is the UK one -- which is exactly what a business-type
    campaign looks like once its leads span six markets. Routing by the
    campaign would send a Gulf dentist on London hours, Monday to Friday, in a
    market that works Sunday to Thursday.
    """
    decision = route("AE", campaign=3809755)

    assert decision.campaign_id == "3809760", (
        "a Gulf recipient was routed to the campaign's own carrier"
    )


def test_two_recipients_of_one_campaign_can_ride_different_clocks() -> None:
    """The property the whole change exists for: one Titan campaign, many
    markets, each message on its recipient's own working day."""
    london = route("GB", campaign=3770052)
    sydney = route("AU", campaign=3770052)

    assert {london.campaign_id, sydney.campaign_id} == {"3809755", "3809761"}


# -------------------------------------------------------------- the fallbacks


def test_an_unknown_location_uses_the_campaigns_own_carrier() -> None:
    """Not a failure. It is precisely what every message did before markets had
    carriers of their own, so a lead Places never resolved is routed today
    exactly as it was yesterday."""
    decision = route(None, campaign=3770052)

    assert decision.campaign_id == "3770052"
    assert decision.region is None
    assert not decision.routed_by_market
    assert "unspecified" in decision.reason


def test_a_country_outside_every_market_falls_back_rather_than_guessing() -> None:
    """Japan has no working week in the schedule. Picking the nearest carrier
    would be inventing one."""
    decision = route("JP", campaign=3770052)

    assert decision.campaign_id == "3770052"
    assert not decision.routed_by_market
    assert "other" in decision.reason


def test_a_known_market_with_no_carrier_is_not_given_a_neighbours() -> None:
    """Planted violation: fall through to any available carrier and this fails.

    This lead's working day is known, and known to differ from every carrier on
    offer. The fix is to provision the market -- which the reason says -- not to
    put the message on a clock that is wrong in a documented way.
    """
    decision = route(
        "AE",
        carriers=CarrierMap(by_clock={}, by_market={Region.UK: "3809755"}),
        campaign=3770052,
    )

    assert decision.campaign_id == "3770052"
    assert not decision.routed_by_market
    assert "no carrier campaign provisioned for middle_east" in decision.reason


def test_no_carrier_anywhere_leaves_the_provider_default() -> None:
    """None is not an error: the adapter then uses its configured campaign,
    which is what a single-market workspace has always relied on."""
    decision = route("GB", carriers=CarrierMap(by_clock={}, by_market={}), campaign=None)

    assert decision.campaign_id is None


def test_the_campaign_id_crosses_as_text() -> None:
    """Smartlead's ids are numeric and Instantly's are not. A value that changes
    type on the way through is one that eventually arrives as the wrong one."""
    assert route("GB", campaign=3770052).campaign_id == "3809755"
    assert isinstance(route(None, campaign=3770052).campaign_id, str)


def test_unroutable_markets_are_the_two_that_name_no_working_week() -> None:
    """UNSPECIFIED is "nobody said"; OTHER is "somewhere the schedule has no
    opinion about". Adding a real market here would silently stop routing it."""
    assert UNROUTABLE == frozenset({Region.UNSPECIFIED, Region.OTHER})


# ------------------------------------------------------- the recipient's clock

#: Toronto answers for Vancouver three hours away; New York for Los Angeles;
#: Dubai for Riyadh; Berlin for Dublin. On the live workspace that is 539 leads
#: of 2,733 on an hour that is not theirs, so a carrier may hold a clock.
WITH_CLOCKS = CarrierMap(
    by_clock={"America/Los_Angeles": "3900001", "Europe/Dublin": "3900002"},
    by_market=CARRIERS.by_market,
)


def test_the_recipients_own_clock_beats_their_market() -> None:
    """A Los Angeles dentist is three hours from the market's New York clock.
    Given a carrier for their zone, they ride it."""
    decision = route("US", carriers=WITH_CLOCKS, timezone="America/Los_Angeles")

    assert decision.campaign_id == "3900001"
    assert decision.timezone == "America/Los_Angeles"
    assert decision.region is Region.USA


def test_a_clock_with_no_carrier_falls_back_to_the_market() -> None:
    """Planted violation: refuse when the exact zone is unprovisioned and every
    Chicago lead stops sending rather than sending an hour early."""
    decision = route("US", carriers=WITH_CLOCKS, timezone="America/Chicago")

    assert decision.campaign_id == "3809757", "should fall back to the USA carrier"
    assert decision.timezone is None
    assert decision.routed_by_market


def test_dublin_is_reachable_though_no_subregion_band_describes_it() -> None:
    """The reason this is keyed by IANA zone and not by SubRegion. Bands exist
    only for the USA, Canada and Australia -- so Dublin against Berlin, and
    Riyadh against Dubai, would have been unfixable by a band."""
    decision = route("IE", carriers=WITH_CLOCKS, timezone="Europe/Dublin")

    assert decision.campaign_id == "3900002"


def test_a_clock_carrier_is_not_used_for_a_lead_in_another_zone() -> None:
    """Planted violation: match on anything but the exact zone and a Berlin
    lead rides the Dublin campaign, an hour before its own working day."""
    decision = route("DE", carriers=WITH_CLOCKS, timezone="Europe/Berlin")

    assert decision.campaign_id == "3809759"


def test_a_lead_with_no_stored_clock_still_routes_by_market() -> None:
    """Every location Titan holds has a zone today, but the column is nullable
    and a nullable column is eventually null."""
    decision = route("US", carriers=WITH_CLOCKS, timezone=None)

    assert decision.campaign_id == "3809757"
