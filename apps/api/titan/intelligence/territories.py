"""Where to look for businesses, in what order, and on whose clock.

``build_query`` turns a campaign's targeting into one search: business type in
geography. One campaign, one geography, forever. Google Places returns roughly
twenty-five usable results for a text query, so a campaign exhausts its only
search in a single run and then re-asks the identical question every cycle for
the rest of its life -- paying for a request each time and receiving the same
twenty-five businesses it already has.

Observed, in the system's own words, eleven times an hour for fifteen hours:

    returned: 40, admitted: 0, refused: {already_known: 25}, cost_usd: 0.064

This is the list of places worth asking instead, the rule for moving through
them, and the clock each one keeps.

**Wide, but only where the work is.** Not every city -- the metros where the
kind of business Titan sells to actually clusters, and where a broken booking
page costs the owner enough money to be worth an email. A long tail of market
towns would multiply the search bill to find businesses with no budget.

**Every territory carries its own timezone.** Not a region default: the actual
IANA zone of the actual metro. Discovery knows exactly which city it searched,
which means it knows the timezone of every business it finds there -- and until
now it threw that away. Three hundred and eighty-two organisation locations were
stored and twenty had a timezone, so ninety-five percent of leads were scheduled
against their market's representative clock. That is survivable for the UK, one
hour wrong for Bucharest against a Berlin default, and three hours wrong for Los
Angeles against Eastern.

**Order is deliberate, not alphabetical.** The first entry for each region is
the densest market, so a campaign that only ever runs once still runs against
the best available ground. After that, roughly by size, because a campaign
rotates through them in order and should spend its early searches where the
answers are thickest.

**Runway is the point.** A market with eight metros gives a campaign eight
hours of useful searching and then stops. The catalogue is sized so that each
market carries enough ground for a campaign to work it for days rather than
hours.
"""

from __future__ import annotations

from dataclasses import dataclass

from titan.db.enums import Region, SubRegion

#: Zones used by more than one territory, named once. A typo in an IANA string
#: is not a syntax error -- ``zoneinfo`` raises at the moment of use, deep in
#: send scheduling, long after the entry was added.
_LONDON = "Europe/London"
_DUBLIN = "Europe/Dublin"
_BERLIN = "Europe/Berlin"
_GULF = "Asia/Dubai"


@dataclass(frozen=True, slots=True)
class Territory:
    """One metro worth searching, and the clock it keeps."""

    #: What goes into the Places query, e.g. "London UK". Includes the country
    #: because "Birmingham" alone returns Alabama as readily as England.
    query_name: str
    country_code: str
    region: Region
    #: The metro's real IANA zone. Required, not defaulted: a territory whose
    #: clock nobody stated is a territory whose leads get sent at the wrong
    #: time of day, and the default would hide that rather than surface it.
    timezone: str
    #: Only where a market spans bands that the campaign-level clock must know
    #: about. Europe is deliberately UNSPECIFIED throughout -- its zones follow
    #: national borders, ``timezone`` states them exactly, and a second
    #: vocabulary saying the same thing would be one more place for the two to
    #: disagree.
    sub_region: SubRegion = SubRegion.UNSPECIFIED

    @property
    def city(self) -> str:
        return (
            self.query_name.rsplit(" ", 1)[0]
            if " " in self.query_name
            else self.query_name
        )


#: Densest first within each region. These are the markets where professional
#: services and clinics cluster thickly enough that a single business type
#: returns a full page, and where the customer has budget.
TERRITORIES: tuple[Territory, ...] = (
    # ---- United Kingdom -------------------------------------------------
    # One zone throughout, Belfast included.
    Territory("London UK", "GB", Region.UK, _LONDON),
    Territory("Manchester UK", "GB", Region.UK, _LONDON),
    Territory("Birmingham UK", "GB", Region.UK, _LONDON),
    Territory("Leeds UK", "GB", Region.UK, _LONDON),
    Territory("Glasgow UK", "GB", Region.UK, _LONDON),
    Territory("Edinburgh UK", "GB", Region.UK, _LONDON),
    Territory("Bristol UK", "GB", Region.UK, _LONDON),
    Territory("Liverpool UK", "GB", Region.UK, _LONDON),
    Territory("Sheffield UK", "GB", Region.UK, _LONDON),
    Territory("Nottingham UK", "GB", Region.UK, _LONDON),
    Territory("Newcastle upon Tyne UK", "GB", Region.UK, _LONDON),
    Territory("Cardiff UK", "GB", Region.UK, _LONDON),
    Territory("Belfast UK", "GB", Region.UK, _LONDON),
    Territory("Leicester UK", "GB", Region.UK, _LONDON),
    Territory("Southampton UK", "GB", Region.UK, _LONDON),
    Territory("Brighton UK", "GB", Region.UK, _LONDON),
    Territory("Reading UK", "GB", Region.UK, _LONDON),
    Territory("Cambridge UK", "GB", Region.UK, _LONDON),
    Territory("Oxford UK", "GB", Region.UK, _LONDON),
    Territory("Aberdeen UK", "GB", Region.UK, _LONDON),
    # ---- United States --------------------------------------------------
    # sub_region is stated per metro rather than per state: Texas, Florida and
    # Tennessee are split by the zone boundary, so the state name genuinely
    # does not settle it.
    Territory(
        "New York NY USA", "US", Region.USA, "America/New_York", SubRegion.US_EASTERN
    ),
    Territory(
        "Los Angeles CA USA",
        "US",
        Region.USA,
        "America/Los_Angeles",
        SubRegion.US_PACIFIC,
    ),
    Territory(
        "Chicago IL USA", "US", Region.USA, "America/Chicago", SubRegion.US_CENTRAL
    ),
    Territory("Miami FL USA", "US", Region.USA, "America/New_York", SubRegion.US_EASTERN),
    Territory("Dallas TX USA", "US", Region.USA, "America/Chicago", SubRegion.US_CENTRAL),
    Territory(
        "Houston TX USA", "US", Region.USA, "America/Chicago", SubRegion.US_CENTRAL
    ),
    Territory(
        "Boston MA USA", "US", Region.USA, "America/New_York", SubRegion.US_EASTERN
    ),
    Territory(
        "Atlanta GA USA", "US", Region.USA, "America/New_York", SubRegion.US_EASTERN
    ),
    Territory(
        "Seattle WA USA",
        "US",
        Region.USA,
        "America/Los_Angeles",
        SubRegion.US_PACIFIC,
    ),
    Territory("Denver CO USA", "US", Region.USA, "America/Denver", SubRegion.US_MOUNTAIN),
    # Its own band: Arizona does not observe daylight saving, so folding it
    # into Mountain is right for four months a year and an hour wrong for the
    # other eight.
    Territory(
        "Phoenix AZ USA", "US", Region.USA, "America/Phoenix", SubRegion.US_ARIZONA
    ),
    Territory(
        "San Francisco CA USA",
        "US",
        Region.USA,
        "America/Los_Angeles",
        SubRegion.US_PACIFIC,
    ),
    Territory(
        "San Diego CA USA",
        "US",
        Region.USA,
        "America/Los_Angeles",
        SubRegion.US_PACIFIC,
    ),
    Territory("Austin TX USA", "US", Region.USA, "America/Chicago", SubRegion.US_CENTRAL),
    Territory(
        "Philadelphia PA USA",
        "US",
        Region.USA,
        "America/New_York",
        SubRegion.US_EASTERN,
    ),
    Territory(
        "Washington DC USA", "US", Region.USA, "America/New_York", SubRegion.US_EASTERN
    ),
    Territory(
        "Minneapolis MN USA", "US", Region.USA, "America/Chicago", SubRegion.US_CENTRAL
    ),
    Territory(
        "Charlotte NC USA", "US", Region.USA, "America/New_York", SubRegion.US_EASTERN
    ),
    Territory(
        "Nashville TN USA", "US", Region.USA, "America/Chicago", SubRegion.US_CENTRAL
    ),
    Territory("Tampa FL USA", "US", Region.USA, "America/New_York", SubRegion.US_EASTERN),
    Territory(
        "Orlando FL USA", "US", Region.USA, "America/New_York", SubRegion.US_EASTERN
    ),
    Territory(
        "Las Vegas NV USA",
        "US",
        Region.USA,
        "America/Los_Angeles",
        SubRegion.US_PACIFIC,
    ),
    Territory(
        "Portland OR USA",
        "US",
        Region.USA,
        "America/Los_Angeles",
        SubRegion.US_PACIFIC,
    ),
    Territory(
        "Detroit MI USA", "US", Region.USA, "America/New_York", SubRegion.US_EASTERN
    ),
    Territory(
        "Salt Lake City UT USA",
        "US",
        Region.USA,
        "America/Denver",
        SubRegion.US_MOUNTAIN,
    ),
    # ---- Middle East ----------------------------------------------------
    # Two offsets, not one. The UAE and Oman are +4; Saudi Arabia, Qatar,
    # Kuwait and Bahrain are +3. None of them observe daylight saving, so the
    # gap is fixed all year.
    Territory("Dubai UAE", "AE", Region.MIDDLE_EAST, _GULF),
    Territory("Abu Dhabi UAE", "AE", Region.MIDDLE_EAST, _GULF),
    Territory("Sharjah UAE", "AE", Region.MIDDLE_EAST, _GULF),
    Territory("Riyadh Saudi Arabia", "SA", Region.MIDDLE_EAST, "Asia/Riyadh"),
    Territory("Jeddah Saudi Arabia", "SA", Region.MIDDLE_EAST, "Asia/Riyadh"),
    Territory("Doha Qatar", "QA", Region.MIDDLE_EAST, "Asia/Qatar"),
    Territory("Kuwait City Kuwait", "KW", Region.MIDDLE_EAST, "Asia/Kuwait"),
    Territory("Manama Bahrain", "BH", Region.MIDDLE_EAST, "Asia/Bahrain"),
    Territory("Muscat Oman", "OM", Region.MIDDLE_EAST, "Asia/Muscat"),
    # ---- Europe, west ---------------------------------------------------
    Territory("Dublin Ireland", "IE", Region.EUROPE, _DUBLIN),
    Territory("Amsterdam Netherlands", "NL", Region.EUROPE, "Europe/Amsterdam"),
    Territory("Berlin Germany", "DE", Region.EUROPE, _BERLIN),
    Territory("Munich Germany", "DE", Region.EUROPE, _BERLIN),
    Territory("Frankfurt Germany", "DE", Region.EUROPE, _BERLIN),
    Territory("Hamburg Germany", "DE", Region.EUROPE, _BERLIN),
    Territory("Paris France", "FR", Region.EUROPE, "Europe/Paris"),
    Territory("Madrid Spain", "ES", Region.EUROPE, "Europe/Madrid"),
    Territory("Barcelona Spain", "ES", Region.EUROPE, "Europe/Madrid"),
    Territory("Milan Italy", "IT", Region.EUROPE, "Europe/Rome"),
    Territory("Rome Italy", "IT", Region.EUROPE, "Europe/Rome"),
    Territory("Zurich Switzerland", "CH", Region.EUROPE, "Europe/Zurich"),
    Territory("Geneva Switzerland", "CH", Region.EUROPE, "Europe/Zurich"),
    Territory("Vienna Austria", "AT", Region.EUROPE, "Europe/Vienna"),
    Territory("Brussels Belgium", "BE", Region.EUROPE, "Europe/Brussels"),
    Territory("Copenhagen Denmark", "DK", Region.EUROPE, "Europe/Copenhagen"),
    Territory("Stockholm Sweden", "SE", Region.EUROPE, "Europe/Stockholm"),
    Territory("Oslo Norway", "NO", Region.EUROPE, "Europe/Oslo"),
    Territory("Helsinki Finland", "FI", Region.EUROPE, "Europe/Helsinki"),
    Territory("Lisbon Portugal", "PT", Region.EUROPE, "Europe/Lisbon"),
    # ---- Europe, east ---------------------------------------------------
    # Same market, and genuinely two offsets. Poland, Czechia, Hungary,
    # Slovakia, Slovenia and Croatia keep Central European time; Romania,
    # Bulgaria, Greece and the Baltics are an hour ahead of them. Scheduled on
    # the Europe default of Berlin, every Bucharest lead would be written to an
    # hour before its own working day opened.
    Territory("Warsaw Poland", "PL", Region.EUROPE, "Europe/Warsaw"),
    Territory("Krakow Poland", "PL", Region.EUROPE, "Europe/Warsaw"),
    Territory("Wroclaw Poland", "PL", Region.EUROPE, "Europe/Warsaw"),
    Territory("Prague Czech Republic", "CZ", Region.EUROPE, "Europe/Prague"),
    Territory("Budapest Hungary", "HU", Region.EUROPE, "Europe/Budapest"),
    Territory("Bucharest Romania", "RO", Region.EUROPE, "Europe/Bucharest"),
    Territory("Sofia Bulgaria", "BG", Region.EUROPE, "Europe/Sofia"),
    Territory("Bratislava Slovakia", "SK", Region.EUROPE, "Europe/Bratislava"),
    Territory("Ljubljana Slovenia", "SI", Region.EUROPE, "Europe/Ljubljana"),
    Territory("Zagreb Croatia", "HR", Region.EUROPE, "Europe/Zagreb"),
    Territory("Tallinn Estonia", "EE", Region.EUROPE, "Europe/Tallinn"),
    Territory("Riga Latvia", "LV", Region.EUROPE, "Europe/Riga"),
    Territory("Vilnius Lithuania", "LT", Region.EUROPE, "Europe/Vilnius"),
    Territory("Athens Greece", "GR", Region.EUROPE, "Europe/Athens"),
    # ---- Australia ------------------------------------------------------
    Territory(
        "Sydney Australia",
        "AU",
        Region.AUSTRALIA,
        "Australia/Sydney",
        SubRegion.AU_EASTERN,
    ),
    Territory(
        "Melbourne Australia",
        "AU",
        Region.AUSTRALIA,
        "Australia/Melbourne",
        SubRegion.AU_EASTERN,
    ),
    Territory(
        "Brisbane Australia",
        "AU",
        Region.AUSTRALIA,
        "Australia/Brisbane",
        SubRegion.AU_EASTERN,
    ),
    Territory(
        "Perth Australia",
        "AU",
        Region.AUSTRALIA,
        "Australia/Perth",
        SubRegion.AU_WESTERN,
    ),
    Territory(
        "Adelaide Australia",
        "AU",
        Region.AUSTRALIA,
        "Australia/Adelaide",
        SubRegion.AU_CENTRAL,
    ),
    Territory(
        "Gold Coast Australia",
        "AU",
        Region.AUSTRALIA,
        "Australia/Brisbane",
        SubRegion.AU_EASTERN,
    ),
    Territory(
        "Canberra Australia",
        "AU",
        Region.AUSTRALIA,
        "Australia/Sydney",
        SubRegion.AU_EASTERN,
    ),
    Territory(
        "Newcastle Australia",
        "AU",
        Region.AUSTRALIA,
        "Australia/Sydney",
        SubRegion.AU_EASTERN,
    ),
    Territory(
        "Hobart Australia",
        "AU",
        Region.AUSTRALIA,
        "Australia/Hobart",
        SubRegion.AU_EASTERN,
    ),
    # ---- Canada ---------------------------------------------------------
    Territory(
        "Toronto Canada", "CA", Region.CANADA, "America/Toronto", SubRegion.CA_EASTERN
    ),
    Territory(
        "Vancouver Canada",
        "CA",
        Region.CANADA,
        "America/Vancouver",
        SubRegion.CA_PACIFIC,
    ),
    Territory(
        "Montreal Canada", "CA", Region.CANADA, "America/Toronto", SubRegion.CA_EASTERN
    ),
    Territory(
        "Calgary Canada", "CA", Region.CANADA, "America/Edmonton", SubRegion.CA_MOUNTAIN
    ),
    Territory(
        "Ottawa Canada", "CA", Region.CANADA, "America/Toronto", SubRegion.CA_EASTERN
    ),
    Territory(
        "Edmonton Canada",
        "CA",
        Region.CANADA,
        "America/Edmonton",
        SubRegion.CA_MOUNTAIN,
    ),
    Territory(
        "Winnipeg Canada", "CA", Region.CANADA, "America/Winnipeg", SubRegion.CA_CENTRAL
    ),
    Territory(
        "Quebec City Canada",
        "CA",
        Region.CANADA,
        "America/Toronto",
        SubRegion.CA_EASTERN,
    ),
    Territory(
        "Halifax Canada", "CA", Region.CANADA, "America/Halifax", SubRegion.CA_ATLANTIC
    ),
    Territory(
        "Victoria Canada",
        "CA",
        Region.CANADA,
        "America/Vancouver",
        SubRegion.CA_PACIFIC,
    ),
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
    costs a billable request and returns nothing, which is what every campaign
    here was doing each cycle.

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


def timezone_of(query_name: str | None) -> str | None:
    """The IANA zone of a stored geography, when it is a known territory.

    Used at discovery time to stamp every business found in a metro with that
    metro's clock. Returns None for a hand-configured geography, which is the
    honest answer -- the caller then falls back to the market default, exactly
    as it did before, rather than to a zone nobody has grounds for.
    """
    territory = find(query_name or "")
    return territory.timezone if territory is not None else None


def describe(territory: Territory) -> str:
    return (
        f"{territory.query_name} ({territory.region.value}"
        + (
            f"/{territory.sub_region.value}"
            if territory.sub_region is not SubRegion.UNSPECIFIED
            else ""
        )
        + f", {territory.timezone})"
    )


__all__ = [
    "TERRITORIES",
    "Territory",
    "describe",
    "find",
    "for_region",
    "next_territory",
    "timezone_of",
]
