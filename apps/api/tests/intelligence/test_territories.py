"""Where to look next, once the current ground is worked out.

Every campaign in this workspace searched one geography for its whole life.
Places returns about twenty-five businesses for a text query, so each campaign
exhausted its only search on the first run and then paid for the identical
question every cycle afterwards:

    Search: dentists in Liverpool UK
    Refused: 25 already_known

Seventeen of those alerts arrived in one day. The machinery for regions and
timezone bands had existed since Phase 02 with nothing feeding it -- all eleven
campaigns were `uk`, and every geography was a northern English city.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo

from titan.db.enums import Region, SubRegion
from titan.intelligence.territories import (
    TERRITORIES,
    Territory,
    find,
    for_region,
    next_territory,
    timezone_of,
)
from titan.policy.subregions import belongs_to

# ---------------------------------------------------------------- the catalogue


def test_the_markets_the_operator_named_are_all_present() -> None:
    """London, New York and Dubai were asked for by name."""
    names = {t.query_name for t in TERRITORIES}

    assert "London UK" in names
    assert "New York NY USA" in names
    assert "Dubai UAE" in names


def test_eastern_europe_is_in_the_catalogue() -> None:
    """Asked for by name alongside the USA, UK, Dubai, Australia and Canada.

    It shares ``Region.EUROPE`` with the west because it is one market -- one
    working week, one holiday shape -- and the zones are what differ, which is
    what ``timezone`` is for.
    """
    names = {t.query_name for t in TERRITORIES}

    assert {"Warsaw Poland", "Prague Czech Republic", "Budapest Hungary"} <= names
    assert "Bucharest Romania" in names


def test_the_two_european_offsets_are_not_collapsed() -> None:
    """Warsaw and Bucharest are an hour apart, all year.

    Scheduled on the Europe default of Berlin, every Bucharest lead would be
    written to an hour before its own working day opened -- which is the whole
    reason a territory stores a zone rather than inheriting one.
    """
    warsaw = find("Warsaw Poland")
    bucharest = find("Bucharest Romania")

    assert warsaw is not None and bucharest is not None
    instant = dt.datetime(2026, 6, 1, 12, tzinfo=dt.UTC)
    offset_of = lambda tz: instant.astimezone(zoneinfo.ZoneInfo(tz)).utcoffset()  # noqa: E731

    assert offset_of(bucharest.timezone) - offset_of(warsaw.timezone) == dt.timedelta(
        hours=1
    )


def test_the_gulf_is_two_offsets_as_well() -> None:
    """The UAE is +4 and Saudi Arabia is +3; neither observes daylight saving."""
    dubai = find("Dubai UAE")
    riyadh = find("Riyadh Saudi Arabia")

    assert dubai is not None and riyadh is not None
    instant = dt.datetime(2026, 6, 1, 12, tzinfo=dt.UTC)

    assert instant.astimezone(
        zoneinfo.ZoneInfo(dubai.timezone)
    ).utcoffset() - instant.astimezone(
        zoneinfo.ZoneInfo(riyadh.timezone)
    ).utcoffset() == dt.timedelta(hours=1)


def test_every_territory_names_a_real_zone() -> None:
    """A typo in an IANA string is not a syntax error.

    ``zoneinfo`` raises at the moment of use, deep inside send scheduling and
    long after the entry was added, so the whole catalogue is resolved here.
    """
    for territory in TERRITORIES:
        zoneinfo.ZoneInfo(territory.timezone)  # raises if the name is not real


def test_a_metros_band_and_its_zone_agree_on_the_market() -> None:
    """The zone may be finer than the band, but it may not contradict it.

    Brisbane is ``Australia/Brisbane`` while its band resolves to Sydney, and
    they differ by an hour for four months of the year. That is deliberate --
    the metro is the more precise of the two and the recipient's own timezone
    wins at send time. What would be a bug is a band from a different continent.
    """
    for territory in TERRITORIES:
        assert belongs_to(territory.sub_region, territory.region), territory.query_name


def test_a_market_carries_days_of_runway_not_hours() -> None:
    """Discovery searches once an hour per campaign.

    A market with eight metros is spent before lunch and the campaign goes
    quiet, which is the failure this catalogue exists to prevent -- so the size
    of each market is itself the fix and worth asserting.
    """
    for region in (Region.UK, Region.USA, Region.EUROPE):
        assert len(for_region(region)) >= 15, region.value


def test_the_timezone_of_a_stored_geography_is_recoverable() -> None:
    """Discovery stamps it on every business it writes."""
    assert timezone_of("Dubai UAE") == "Asia/Dubai"
    assert timezone_of("  new york ny usa  ") == "America/New_York"


def test_an_uncatalogued_geography_has_no_zone_to_offer() -> None:
    """None means "fall back to the market default", which is what the caller
    did before. Naming a plausible zone instead would be a guess presented as a
    fact."""
    assert timezone_of("Somewhere Nobody Listed") is None
    assert timezone_of(None) is None


def test_every_real_market_has_somewhere_to_search() -> None:
    """A region with no territories would move on to nothing.

    `next_territory` would return None on its first call and the campaign would
    sit still while reporting that its ground was exhausted.
    """
    for region in (
        Region.UK,
        Region.USA,
        Region.EUROPE,
        Region.MIDDLE_EAST,
        Region.AUSTRALIA,
        Region.CANADA,
    ):
        assert for_region(region), f"{region.value} has no territories"


def test_a_query_name_carries_its_country() -> None:
    """ "Birmingham" alone returns Alabama as readily as England.

    The geography goes into a text search, so the country is part of the
    question rather than a filter applied afterwards.
    """
    for territory in TERRITORIES:
        assert len(territory.query_name.split()) >= 2, territory.query_name
        assert territory.country_code.isupper()
        assert len(territory.country_code) == 2


def test_us_territories_carry_their_timezone_band() -> None:
    """The whole reason the catalogue stores more than a name.

    A lead found in Phoenix scheduled on the US market default would be sent on
    Eastern -- three hours wrong for them, and outside their working day.
    """
    for territory in for_region(Region.USA):
        assert territory.sub_region is not SubRegion.UNSPECIFIED, territory.query_name


def test_arizona_is_its_own_band() -> None:
    """It does not observe daylight saving.

    Folding it into Mountain is right for four months a year and an hour wrong
    for the other eight.
    """
    phoenix = find("Phoenix AZ USA")

    assert phoenix is not None
    assert phoenix.sub_region is SubRegion.US_ARIZONA


def test_the_densest_market_is_searched_first() -> None:
    """A campaign that runs once should run against the best ground available."""
    assert for_region(Region.UK)[0].query_name == "London UK"
    assert for_region(Region.USA)[0].query_name == "New York NY USA"
    assert for_region(Region.MIDDLE_EAST)[0].query_name == "Dubai UAE"


# ------------------------------------------------------------------- rotation


def test_an_exhausted_geography_is_skipped() -> None:
    """The behaviour the whole module exists for."""
    nxt = next_territory(Region.UK, exhausted={"London UK"})

    assert nxt is not None
    assert nxt.query_name == "Manchester UK"


def test_rotation_walks_the_whole_market() -> None:
    """Each exhausted place moves the campaign to the next, not back to the
    first."""
    spent: set[str] = set()
    seen: list[str] = []
    while (nxt := next_territory(Region.UK, exhausted=spent)) is not None:
        seen.append(nxt.query_name)
        spent.add(nxt.query_name)

    assert seen == [t.query_name for t in for_region(Region.UK)]
    assert len(seen) == len(set(seen)), "a territory was offered twice"


def test_a_spent_market_returns_none_rather_than_repeating() -> None:
    """None is a real answer: widen the business type or the market.

    Cycling back to the first territory would restore exactly the behaviour
    this replaces -- paying for a question whose answer is already held.
    """
    spent = {t.query_name for t in for_region(Region.MIDDLE_EAST)}

    assert next_territory(Region.MIDDLE_EAST, exhausted=spent) is None


def test_rotation_never_leaves_the_campaigns_market() -> None:
    """A UK campaign that quietly started searching Phoenix would send on the
    wrong clock, in the wrong working week, with a message written for
    somewhere else."""
    spent = {t.query_name for t in for_region(Region.UK)}

    assert next_territory(Region.UK, exhausted=spent) is None


def test_exhaustion_matching_ignores_case_and_padding() -> None:
    """The stored geography is operator-entered text, not a controlled value."""
    nxt = next_territory(Region.UK, exhausted={"  london uk  "})

    assert nxt is not None
    assert nxt.query_name != "London UK"


def test_an_unknown_geography_is_not_a_territory() -> None:
    """A hand-configured campaign keeps working; discovery falls back to its own
    target_geography, so None here is normal rather than an error."""
    assert find("Somewhere Nobody Listed") is None
    assert find("") is None


def test_a_region_with_no_catalogue_entry_yields_nothing() -> None:
    """UNSPECIFIED is not a market. A campaign that never declared one has
    nowhere to be moved to, and must keep its configured geography."""
    assert next_territory(Region.UNSPECIFIED, exhausted=set()) is None


def test_territories_are_unique() -> None:
    """A duplicate would be offered twice and searched twice."""
    names = [t.query_name for t in TERRITORIES]

    assert len(names) == len(set(names))


def test_a_territory_is_immutable() -> None:
    """The catalogue is shared across campaigns and cycles."""
    territory = TERRITORIES[0]

    assert isinstance(territory, Territory)
    try:
        territory.query_name = "somewhere else"  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("a territory could be rewritten in place")
