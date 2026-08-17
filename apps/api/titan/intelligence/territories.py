"""Where to look for businesses, and in what order.

``build_query`` turns a campaign's targeting into one search: business type in
geography. One campaign, one geography, forever. Google Places returns roughly
twenty-five results for a text query, so a campaign exhausts its only search in
a single run and then re-asks the identical question every cycle for the rest of
its life -- paying for a request each time and receiving the same twenty-five
businesses it already has.

Observed, in the system's own words:

    Search: dentists in Liverpool UK
    Refused: 25 already_known

This is the list of places worth asking instead, and the rule for moving through
them.

**Wide, but only where the work is.** Not every city -- the metros where the
kind of business Titan sells to actually clusters, and where a broken booking
page costs the owner enough money to be worth an email. A long tail of small
towns would triple the search bill to find businesses with no budget.

**Every territory carries its own clock.** A metro is stored with the region and
the timezone band it sits in, so a lead discovered in Phoenix is scheduled on
Arizona time rather than on the market default -- which is Eastern, and three
hours wrong for them. The regional machinery has existed since Phase 02 and had
nothing feeding it: every campaign was ``uk`` and every geography was a northern
English city.

**Order is deliberate, not alphabetical.** The first entry for each region is
the densest market, so a campaign that only ever runs once still runs against
the best available ground.
"""

from __future__ import annotations

from dataclasses import dataclass

from titan.db.enums import Region, SubRegion


@dataclass(frozen=True, slots=True)
class Territory:
    """One metro worth searching, and the clock it keeps."""

    #: What goes into the Places query, e.g. "London UK". Includes the country
    #: because "Birmingham" alone returns Alabama as readily as England.
    query_name: str
    country_code: str
    region: Region
    sub_region: SubRegion = SubRegion.UNSPECIFIED

    @property
    def city(self) -> str:
        return (
            self.query_name.rsplit(" ", 1)[0]
            if " " in self.query_name
            else self.query_name
        )


#: Densest first within each region. Deliberately short: these are the markets
#: where professional services and clinics cluster thickly enough that a single
#: business type returns a full page, and where the customer has budget.
TERRITORIES: tuple[Territory, ...] = (
    # ---- United Kingdom -------------------------------------------------
    Territory("London UK", "GB", Region.UK),
    Territory("Manchester UK", "GB", Region.UK),
    Territory("Birmingham UK", "GB", Region.UK),
    Territory("Leeds UK", "GB", Region.UK),
    Territory("Glasgow UK", "GB", Region.UK),
    Territory("Edinburgh UK", "GB", Region.UK),
    Territory("Bristol UK", "GB", Region.UK),
    Territory("Liverpool UK", "GB", Region.UK),
    # ---- United States --------------------------------------------------
    Territory("New York NY USA", "US", Region.USA, SubRegion.US_EASTERN),
    Territory("Los Angeles CA USA", "US", Region.USA, SubRegion.US_PACIFIC),
    Territory("Chicago IL USA", "US", Region.USA, SubRegion.US_CENTRAL),
    Territory("Miami FL USA", "US", Region.USA, SubRegion.US_EASTERN),
    Territory("Dallas TX USA", "US", Region.USA, SubRegion.US_CENTRAL),
    Territory("Boston MA USA", "US", Region.USA, SubRegion.US_EASTERN),
    Territory("Atlanta GA USA", "US", Region.USA, SubRegion.US_EASTERN),
    Territory("Seattle WA USA", "US", Region.USA, SubRegion.US_PACIFIC),
    Territory("Denver CO USA", "US", Region.USA, SubRegion.US_MOUNTAIN),
    # Its own band: Arizona does not observe daylight saving, so folding it
    # into Mountain is right for four months a year and an hour wrong for the
    # other eight.
    Territory("Phoenix AZ USA", "US", Region.USA, SubRegion.US_ARIZONA),
    # ---- Middle East ----------------------------------------------------
    Territory("Dubai UAE", "AE", Region.MIDDLE_EAST),
    Territory("Abu Dhabi UAE", "AE", Region.MIDDLE_EAST),
    Territory("Doha Qatar", "QA", Region.MIDDLE_EAST),
    Territory("Riyadh Saudi Arabia", "SA", Region.MIDDLE_EAST),
    # ---- Europe ---------------------------------------------------------
    Territory("Dublin Ireland", "IE", Region.EUROPE),
    Territory("Amsterdam Netherlands", "NL", Region.EUROPE),
    Territory("Berlin Germany", "DE", Region.EUROPE),
    Territory("Munich Germany", "DE", Region.EUROPE),
    Territory("Madrid Spain", "ES", Region.EUROPE),
    Territory("Milan Italy", "IT", Region.EUROPE),
    Territory("Zurich Switzerland", "CH", Region.EUROPE),
    # ---- Australia ------------------------------------------------------
    Territory("Sydney Australia", "AU", Region.AUSTRALIA, SubRegion.AU_EASTERN),
    Territory("Melbourne Australia", "AU", Region.AUSTRALIA, SubRegion.AU_EASTERN),
    Territory("Brisbane Australia", "AU", Region.AUSTRALIA, SubRegion.AU_EASTERN),
    Territory("Perth Australia", "AU", Region.AUSTRALIA, SubRegion.AU_WESTERN),
    Territory("Adelaide Australia", "AU", Region.AUSTRALIA, SubRegion.AU_CENTRAL),
    # ---- Canada ---------------------------------------------------------
    Territory("Toronto Canada", "CA", Region.CANADA, SubRegion.CA_EASTERN),
    Territory("Vancouver Canada", "CA", Region.CANADA, SubRegion.CA_PACIFIC),
    Territory("Montreal Canada", "CA", Region.CANADA, SubRegion.CA_EASTERN),
    Territory("Calgary Canada", "CA", Region.CANADA, SubRegion.CA_MOUNTAIN),
)


def for_region(region: Region) -> tuple[Territory, ...]:
    """Every territory in one market, densest first."""
    return tuple(t for t in TERRITORIES if t.region is region)


def find(query_name: str) -> Territory | None:
    """The territory a stored geography names, if it is one of ours.

    Matched case-insensitively on the whole string. A campaign configured by
    hand with something not in the catalogue keeps working -- discovery falls
    back to its own ``target_geography`` -- so this returning None is a normal
    state and not an error.
    """
    wanted = (query_name or "").strip().casefold()
    for territory in TERRITORIES:
        if territory.query_name.casefold() == wanted:
            return territory
    return None


def next_territory(
    region: Region,
    *,
    exhausted: set[str],
    current: str | None = None,
) -> Territory | None:
    """The next place worth searching for this market, or None when spent.

    ``exhausted`` holds query names whose last search returned results Titan
    already had. Skipping them is the entire point: re-asking a spent question
    costs a billable request and returns nothing, which is what the campaign
    was doing every cycle.

    The current geography is skipped too when it is already exhausted, so a
    campaign moves on rather than sitting on a search that has stopped paying.

    None means every territory in the market is spent. That is a real answer and
    a useful one -- it says "widen the business type or the market", which is a
    decision for a person, not something to solve by asking again.
    """
    spent = {name.strip().casefold() for name in exhausted}
    for territory in for_region(region):
        if territory.query_name.casefold() in spent:
            continue
        return territory

    # Nothing left in the declared market. Deliberately not falling through to
    # another region: a campaign aimed at the UK that silently started emailing
    # Phoenix would be sending on the wrong clock, in the wrong working week,
    # with a message written for a different market.
    _ = current
    return None


def describe(territory: Territory) -> str:
    return (
        f"{territory.query_name} ({territory.region.value}"
        + (
            f"/{territory.sub_region.value}"
            if territory.sub_region is not SubRegion.UNSPECIFIED
            else ""
        )
        + ")"
    )


__all__ = [
    "TERRITORIES",
    "Territory",
    "describe",
    "find",
    "for_region",
    "next_territory",
]
