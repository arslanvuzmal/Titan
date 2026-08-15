"""Timezone bands inside a market.

Two derivations, and the interesting cases are where each one is *not* good
enough. A state name is exact for most states and meaningless for the dozen a
zone boundary runs through. Coordinates work everywhere and are approximate
everywhere. Neither alone is right, which is why both are here.
"""

from __future__ import annotations

import zoneinfo

import pytest
from titan.db.enums import Region, SubRegion
from titan.policy.schedule import resolve_timezone
from titan.policy.subregions import (
    SPLIT_ADMIN_AREAS,
    SUBREGION_REGIONS,
    SUBREGION_TIMEZONES,
    belongs_to,
    subregion_for_location,
    subregion_from_admin_area,
    subregion_from_longitude,
    timezone_for,
)


# ==========================================================================
# The problem being solved
# ==========================================================================
def test_two_us_states_that_share_everything_but_the_clock() -> None:
    """California and Georgia share a country, a market and a working week, and
    are three hours apart. This is the whole item."""
    california = subregion_for_location("US", "California")
    georgia = subregion_for_location("US", "Georgia")

    assert california is SubRegion.US_PACIFIC
    assert georgia is SubRegion.US_EASTERN
    assert timezone_for(california) == "America/Los_Angeles"
    assert timezone_for(georgia) == "America/New_York"


# ==========================================================================
# Names, where the name settles it
# ==========================================================================
@pytest.mark.parametrize(
    ("area", "expected"),
    [
        ("New York", SubRegion.US_EASTERN),
        ("NY", SubRegion.US_EASTERN),
        ("  california  ", SubRegion.US_PACIFIC),
        ("ILLINOIS", SubRegion.US_CENTRAL),
        ("Colorado", SubRegion.US_MOUNTAIN),
        ("Alaska", SubRegion.US_ALASKA),
        ("Hawaii", SubRegion.US_HAWAII),
    ],
)
def test_a_state_name_resolves_its_band(area: str, expected: SubRegion) -> None:
    assert subregion_from_admin_area("US", area) is expected


def test_arizona_is_its_own_band() -> None:
    """It does not observe daylight saving. Folding it into Mountain is correct
    for four months of the year and an hour wrong for the other eight."""
    assert subregion_for_location("US", "Arizona") is SubRegion.US_ARIZONA
    assert timezone_for(SubRegion.US_ARIZONA) == "America/Phoenix"
    assert timezone_for(SubRegion.US_MOUNTAIN) == "America/Denver"


def test_canadian_and_australian_names_resolve_too() -> None:
    assert subregion_from_admin_area("CA", "British Columbia") is SubRegion.CA_PACIFIC
    assert subregion_from_admin_area("AU", "Western Australia") is SubRegion.AU_WESTERN


def test_a_code_is_only_read_inside_its_own_country() -> None:
    """WA is Washington in the USA and Western Australia in Australia. Reading
    codes globally would put a Seattle business on Perth's clock."""
    assert subregion_from_admin_area("US", "WA") is SubRegion.US_PACIFIC
    assert subregion_from_admin_area("AU", "WA") is SubRegion.AU_WESTERN


def test_an_unknown_country_resolves_nothing() -> None:
    assert subregion_from_admin_area("FR", "Normandy") is SubRegion.UNSPECIFIED
    assert subregion_from_admin_area(None, "California") is SubRegion.UNSPECIFIED
    assert subregion_from_admin_area("US", None) is SubRegion.UNSPECIFIED


# ==========================================================================
# Split states, where the name does not
# ==========================================================================
def test_a_split_state_is_not_resolved_by_name() -> None:
    """ "Texas" is a real answer to the wrong question. Accepting it would put a
    Houston business on El Paso's clock or the reverse."""
    assert "TEXAS" in SPLIT_ADMIN_AREAS
    assert subregion_from_admin_area("US", "Texas") is SubRegion.UNSPECIFIED


def test_a_split_state_resolves_by_coordinates_instead() -> None:
    houston = subregion_for_location("US", "Texas", longitude=-95.37)
    el_paso = subregion_for_location("US", "Texas", longitude=-106.49)

    assert houston is SubRegion.US_CENTRAL
    assert el_paso is SubRegion.US_MOUNTAIN


def test_florida_east_and_west_land_in_different_bands() -> None:
    miami = subregion_for_location("US", "Florida", longitude=-80.19)
    pensacola = subregion_for_location("US", "Florida", longitude=-87.60)

    assert miami is SubRegion.US_EASTERN
    assert pensacola is SubRegion.US_CENTRAL


def test_a_split_state_with_no_coordinates_stays_unresolved() -> None:
    """Better than a coin flip. The caller falls back to something it can
    defend."""
    assert subregion_for_location("US", "Texas") is SubRegion.UNSPECIFIED


# ==========================================================================
# Coordinates
# ==========================================================================
@pytest.mark.parametrize(
    ("longitude", "expected"),
    [
        (-73.99, SubRegion.US_EASTERN),  # New York
        (-87.63, SubRegion.US_CENTRAL),  # Chicago
        (-104.99, SubRegion.US_MOUNTAIN),  # Denver
        (-118.24, SubRegion.US_PACIFIC),  # Los Angeles
    ],
)
def test_longitude_bands_the_continental_us(
    longitude: float, expected: SubRegion
) -> None:
    assert subregion_from_longitude("US", longitude) is expected


def test_coordinates_outside_the_continental_span_resolve_nothing() -> None:
    """A coordinate this far out is more likely bad data than an address."""
    assert subregion_from_longitude("US", -157.86) is SubRegion.UNSPECIFIED  # Honolulu
    assert subregion_from_longitude("US", 12.5) is SubRegion.UNSPECIFIED


def test_longitude_is_only_used_for_the_usa() -> None:
    """Canadian and Australian provinces resolve by name, and their populations
    sit in a few widely separated places -- a meridian rule would add error
    without adding coverage."""
    assert subregion_from_longitude("CA", -123.12) is SubRegion.UNSPECIFIED
    assert subregion_from_longitude("AU", 115.86) is SubRegion.UNSPECIFIED


def test_a_name_wins_over_coordinates_where_the_name_is_unambiguous() -> None:
    """A California business whose coordinates were recorded badly is still in
    California."""
    assert (
        subregion_for_location("US", "California", longitude=-73.99)
        is SubRegion.US_PACIFIC
    )


# ==========================================================================
# The resolution chain
# ==========================================================================
def test_the_recipients_own_timezone_beats_every_derivation() -> None:
    assert (
        resolve_timezone(
            "America/Denver",
            Region.USA,
            recipient_subregion=SubRegion.US_PACIFIC,
            campaign_subregion=SubRegion.US_EASTERN,
        )
        == "America/Denver"
    )


def test_the_recipients_band_beats_the_campaigns() -> None:
    """Their address is a fact about them; the campaign's band is a default."""
    assert (
        resolve_timezone(
            None,
            Region.USA,
            recipient_subregion=SubRegion.US_PACIFIC,
            campaign_subregion=SubRegion.US_EASTERN,
        )
        == "America/Los_Angeles"
    )


def test_the_campaigns_band_beats_the_market_default() -> None:
    """The improvement for a US Pacific campaign: its unresolved leads used to
    schedule on Eastern, three hours before anybody had arrived."""
    assert resolve_timezone(None, Region.USA) == "America/New_York"
    assert (
        resolve_timezone(None, Region.USA, campaign_subregion=SubRegion.US_PACIFIC)
        == "America/Los_Angeles"
    )


def test_the_market_still_answers_when_no_band_is_known() -> None:
    assert resolve_timezone(None, Region.UK) == "Europe/London"


def test_nothing_answers_for_an_undeclared_market() -> None:
    assert resolve_timezone(None, Region.UNSPECIFIED) is None


# ==========================================================================
# Consistency
# ==========================================================================
def test_every_band_has_a_real_zone_and_a_market() -> None:
    for band in SubRegion:
        if band is SubRegion.UNSPECIFIED:
            assert timezone_for(band) is None
            continue
        zone = SUBREGION_TIMEZONES[band]
        assert zoneinfo.ZoneInfo(zone) is not None
        assert band in SUBREGION_REGIONS


def test_a_band_belongs_only_to_its_own_market() -> None:
    assert belongs_to(SubRegion.US_PACIFIC, Region.USA) is True
    assert belongs_to(SubRegion.US_PACIFIC, Region.AUSTRALIA) is False


def test_an_unspecified_band_belongs_everywhere() -> None:
    """It is the absence of a claim, not a claim about somewhere else."""
    assert all(belongs_to(SubRegion.UNSPECIFIED, r) for r in Region)


def test_only_the_markets_that_span_zones_are_segmented() -> None:
    """Europe's zones follow its national borders, which target_country_code
    already names. A second vocabulary would be one more place to disagree."""
    segmented = set(SUBREGION_REGIONS.values())

    assert segmented == {Region.USA, Region.CANADA, Region.AUSTRALIA}
    assert Region.EUROPE not in segmented
    assert Region.UK not in segmented
