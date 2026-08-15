"""Working out which clock a business in a large market keeps.

A market is one working week and one set of holidays; it is not one clock. The
USA is the case that makes this obvious -- a business in California and one in
Georgia share a country, a market and a business day, and are three hours apart
-- and until now every part of Titan treated them identically, scheduling both
against a single Eastern clock.

**Only where the country says nothing.** Europe is absent from this module on
purpose: its zones follow national borders closely enough that
``target_country_code`` already distinguishes them, and a second vocabulary
saying the same thing would be one more place for the two to disagree.

**Two derivations, because neither is sufficient alone.**

The state name is exact where a state lies wholly in one zone, which is most of
them. It is not enough for the dozen that straddle a boundary -- Florida,
Texas, Tennessee and the rest -- where "Texas" genuinely does not determine the
hour.

Longitude is available for every business Places returns, works in any language,
and is approximate everywhere: zone boundaries follow county lines and state
politics, not meridians. It is used for the split states, and as a fallback when
a state name is missing or unrecognised.

So: name first where the name settles it, coordinates otherwise, and the market
default where neither answers. Each step is narrower than the last, and none of
them guesses -- an unresolvable location returns UNSPECIFIED and lets the caller
fall back to something it can defend.
"""

from __future__ import annotations

from titan.db.enums import Region, SubRegion

#: Sub-region to IANA zone. The zone is the authority on offsets and daylight
#: saving; this table only says which zone a band belongs to.
SUBREGION_TIMEZONES: dict[SubRegion, str] = {
    SubRegion.US_EASTERN: "America/New_York",
    SubRegion.US_CENTRAL: "America/Chicago",
    SubRegion.US_MOUNTAIN: "America/Denver",
    SubRegion.US_ARIZONA: "America/Phoenix",
    SubRegion.US_PACIFIC: "America/Los_Angeles",
    SubRegion.US_ALASKA: "America/Anchorage",
    SubRegion.US_HAWAII: "Pacific/Honolulu",
    SubRegion.CA_ATLANTIC: "America/Halifax",
    SubRegion.CA_EASTERN: "America/Toronto",
    SubRegion.CA_CENTRAL: "America/Winnipeg",
    SubRegion.CA_MOUNTAIN: "America/Edmonton",
    SubRegion.CA_PACIFIC: "America/Vancouver",
    SubRegion.AU_EASTERN: "Australia/Sydney",
    SubRegion.AU_CENTRAL: "Australia/Adelaide",
    SubRegion.AU_WESTERN: "Australia/Perth",
}

#: Which market each band belongs to, so a campaign cannot be configured with a
#: sub-region from a different continent than its region.
SUBREGION_REGIONS: dict[SubRegion, Region] = {
    SubRegion.US_EASTERN: Region.USA,
    SubRegion.US_CENTRAL: Region.USA,
    SubRegion.US_MOUNTAIN: Region.USA,
    SubRegion.US_ARIZONA: Region.USA,
    SubRegion.US_PACIFIC: Region.USA,
    SubRegion.US_ALASKA: Region.USA,
    SubRegion.US_HAWAII: Region.USA,
    SubRegion.CA_ATLANTIC: Region.CANADA,
    SubRegion.CA_EASTERN: Region.CANADA,
    SubRegion.CA_CENTRAL: Region.CANADA,
    SubRegion.CA_MOUNTAIN: Region.CANADA,
    SubRegion.CA_PACIFIC: Region.CANADA,
    SubRegion.AU_EASTERN: Region.AUSTRALIA,
    SubRegion.AU_CENTRAL: Region.AUSTRALIA,
    SubRegion.AU_WESTERN: Region.AUSTRALIA,
}

#: US states that lie wholly within one zone, by full name and postal code.
#: The split states are deliberately absent -- see ``SPLIT_ADMIN_AREAS``.
_US_STATES: dict[str, SubRegion] = {
    "CONNECTICUT": SubRegion.US_EASTERN,
    "CT": SubRegion.US_EASTERN,
    "DELAWARE": SubRegion.US_EASTERN,
    "DE": SubRegion.US_EASTERN,
    "DISTRICT OF COLUMBIA": SubRegion.US_EASTERN,
    "DC": SubRegion.US_EASTERN,
    "GEORGIA": SubRegion.US_EASTERN,
    "GA": SubRegion.US_EASTERN,
    "MAINE": SubRegion.US_EASTERN,
    "ME": SubRegion.US_EASTERN,
    "MARYLAND": SubRegion.US_EASTERN,
    "MD": SubRegion.US_EASTERN,
    "MASSACHUSETTS": SubRegion.US_EASTERN,
    "MA": SubRegion.US_EASTERN,
    "NEW HAMPSHIRE": SubRegion.US_EASTERN,
    "NH": SubRegion.US_EASTERN,
    "NEW JERSEY": SubRegion.US_EASTERN,
    "NJ": SubRegion.US_EASTERN,
    "NEW YORK": SubRegion.US_EASTERN,
    "NY": SubRegion.US_EASTERN,
    "NORTH CAROLINA": SubRegion.US_EASTERN,
    "NC": SubRegion.US_EASTERN,
    "OHIO": SubRegion.US_EASTERN,
    "OH": SubRegion.US_EASTERN,
    "PENNSYLVANIA": SubRegion.US_EASTERN,
    "PA": SubRegion.US_EASTERN,
    "RHODE ISLAND": SubRegion.US_EASTERN,
    "RI": SubRegion.US_EASTERN,
    "SOUTH CAROLINA": SubRegion.US_EASTERN,
    "SC": SubRegion.US_EASTERN,
    "VERMONT": SubRegion.US_EASTERN,
    "VT": SubRegion.US_EASTERN,
    "VIRGINIA": SubRegion.US_EASTERN,
    "VA": SubRegion.US_EASTERN,
    "WEST VIRGINIA": SubRegion.US_EASTERN,
    "WV": SubRegion.US_EASTERN,
    "ALABAMA": SubRegion.US_CENTRAL,
    "AL": SubRegion.US_CENTRAL,
    "ARKANSAS": SubRegion.US_CENTRAL,
    "AR": SubRegion.US_CENTRAL,
    "ILLINOIS": SubRegion.US_CENTRAL,
    "IL": SubRegion.US_CENTRAL,
    "IOWA": SubRegion.US_CENTRAL,
    "IA": SubRegion.US_CENTRAL,
    "LOUISIANA": SubRegion.US_CENTRAL,
    "LA": SubRegion.US_CENTRAL,
    "MINNESOTA": SubRegion.US_CENTRAL,
    "MN": SubRegion.US_CENTRAL,
    "MISSISSIPPI": SubRegion.US_CENTRAL,
    "MS": SubRegion.US_CENTRAL,
    "MISSOURI": SubRegion.US_CENTRAL,
    "MO": SubRegion.US_CENTRAL,
    "OKLAHOMA": SubRegion.US_CENTRAL,
    "OK": SubRegion.US_CENTRAL,
    "WISCONSIN": SubRegion.US_CENTRAL,
    "WI": SubRegion.US_CENTRAL,
    "COLORADO": SubRegion.US_MOUNTAIN,
    "CO": SubRegion.US_MOUNTAIN,
    "MONTANA": SubRegion.US_MOUNTAIN,
    "MT": SubRegion.US_MOUNTAIN,
    "NEW MEXICO": SubRegion.US_MOUNTAIN,
    "NM": SubRegion.US_MOUNTAIN,
    "UTAH": SubRegion.US_MOUNTAIN,
    "UT": SubRegion.US_MOUNTAIN,
    "WYOMING": SubRegion.US_MOUNTAIN,
    "WY": SubRegion.US_MOUNTAIN,
    "ARIZONA": SubRegion.US_ARIZONA,
    "AZ": SubRegion.US_ARIZONA,
    "CALIFORNIA": SubRegion.US_PACIFIC,
    "CA": SubRegion.US_PACIFIC,
    "WASHINGTON": SubRegion.US_PACIFIC,
    "WA": SubRegion.US_PACIFIC,
    "NEVADA": SubRegion.US_PACIFIC,
    "NV": SubRegion.US_PACIFIC,
    "ALASKA": SubRegion.US_ALASKA,
    "AK": SubRegion.US_ALASKA,
    "HAWAII": SubRegion.US_HAWAII,
    "HI": SubRegion.US_HAWAII,
}

#: Canadian provinces, all of which sit wholly in one zone for business purposes.
_CA_PROVINCES: dict[str, SubRegion] = {
    "NOVA SCOTIA": SubRegion.CA_ATLANTIC,
    "NS": SubRegion.CA_ATLANTIC,
    "NEW BRUNSWICK": SubRegion.CA_ATLANTIC,
    "NB": SubRegion.CA_ATLANTIC,
    "PRINCE EDWARD ISLAND": SubRegion.CA_ATLANTIC,
    "PE": SubRegion.CA_ATLANTIC,
    "ONTARIO": SubRegion.CA_EASTERN,
    "ON": SubRegion.CA_EASTERN,
    "QUEBEC": SubRegion.CA_EASTERN,
    "QC": SubRegion.CA_EASTERN,
    "MANITOBA": SubRegion.CA_CENTRAL,
    "MB": SubRegion.CA_CENTRAL,
    "SASKATCHEWAN": SubRegion.CA_CENTRAL,
    "SK": SubRegion.CA_CENTRAL,
    "ALBERTA": SubRegion.CA_MOUNTAIN,
    "AB": SubRegion.CA_MOUNTAIN,
    "BRITISH COLUMBIA": SubRegion.CA_PACIFIC,
    "BC": SubRegion.CA_PACIFIC,
}

_AU_STATES: dict[str, SubRegion] = {
    "NEW SOUTH WALES": SubRegion.AU_EASTERN,
    "NSW": SubRegion.AU_EASTERN,
    "VICTORIA": SubRegion.AU_EASTERN,
    "VIC": SubRegion.AU_EASTERN,
    "QUEENSLAND": SubRegion.AU_EASTERN,
    "QLD": SubRegion.AU_EASTERN,
    "TASMANIA": SubRegion.AU_EASTERN,
    "TAS": SubRegion.AU_EASTERN,
    "AUSTRALIAN CAPITAL TERRITORY": SubRegion.AU_EASTERN,
    "ACT": SubRegion.AU_EASTERN,
    "SOUTH AUSTRALIA": SubRegion.AU_CENTRAL,
    "SA": SubRegion.AU_CENTRAL,
    "NORTHERN TERRITORY": SubRegion.AU_CENTRAL,
    "NT": SubRegion.AU_CENTRAL,
    "WESTERN AUSTRALIA": SubRegion.AU_WESTERN,
    "WA": SubRegion.AU_WESTERN,
}

#: States a name cannot resolve, because the boundary runs through them. Listed
#: rather than silently omitted: "Texas is missing from the map" and "Texas is
#: split" are different facts, and only one of them is a bug.
SPLIT_ADMIN_AREAS: frozenset[str] = frozenset(
    {
        "FLORIDA",
        "FL",
        "TEXAS",
        "TX",
        "TENNESSEE",
        "TN",
        "KENTUCKY",
        "KY",
        "INDIANA",
        "IN",
        "MICHIGAN",
        "MI",
        "KANSAS",
        "KS",
        "NEBRASKA",
        "NE",
        "NORTH DAKOTA",
        "ND",
        "SOUTH DAKOTA",
        "SD",
        "IDAHO",
        "ID",
        "OREGON",
        "OR",
    }
)

#: Longitude boundaries between the continental US zones, west-most first.
#: Approximate by nature -- the real boundaries follow county lines -- and used
#: only where a name could not settle it.
_US_LONGITUDE_BANDS: tuple[tuple[float, SubRegion], ...] = (
    (-115.0, SubRegion.US_PACIFIC),
    (-101.5, SubRegion.US_MOUNTAIN),
    (-87.5, SubRegion.US_CENTRAL),
)

#: Note that WA is Washington in the USA and Western Australia in Australia, and
#: SA is South Australia and South Africa's ISO code. Codes are only ever looked
#: up inside a known country for exactly this reason.
_BY_COUNTRY: dict[str, dict[str, SubRegion]] = {
    "US": _US_STATES,
    "CA": _CA_PROVINCES,
    "AU": _AU_STATES,
}


def subregion_from_admin_area(
    country_code: str | None, admin_area: str | None
) -> SubRegion:
    """The band a state or province belongs to, where its name settles it."""
    if not country_code or not admin_area:
        return SubRegion.UNSPECIFIED
    table = _BY_COUNTRY.get(country_code.strip().upper())
    if table is None:
        return SubRegion.UNSPECIFIED
    return table.get(admin_area.strip().upper(), SubRegion.UNSPECIFIED)


def subregion_from_longitude(
    country_code: str | None, longitude: float | None
) -> SubRegion:
    """The band a coordinate falls in. Continental USA only.

    Canada and Australia are not derived this way. Their populations sit in a
    few widely separated places and their provinces already resolve by name, so
    a meridian rule would add error without adding coverage.
    """
    if longitude is None or (country_code or "").strip().upper() != "US":
        return SubRegion.UNSPECIFIED
    if longitude < -140.0 or longitude > -60.0:
        # Outside the continental span. Alaska and Hawaii resolve by name; a
        # coordinate this far out is more likely bad data than a real address.
        return SubRegion.UNSPECIFIED
    for boundary, band in _US_LONGITUDE_BANDS:
        if longitude < boundary:
            return band
    return SubRegion.US_EASTERN


def subregion_for_location(
    country_code: str | None,
    admin_area: str | None,
    longitude: float | None = None,
) -> SubRegion:
    """The best band available for one business address.

    Name first, coordinates second, nothing third. A split state skips straight
    to the coordinates: "Texas" is a real answer to the wrong question, and
    accepting it would put a Houston business on El Paso's clock or the reverse.
    """
    normalized_area = (admin_area or "").strip().upper()
    if normalized_area not in SPLIT_ADMIN_AREAS:
        by_name = subregion_from_admin_area(country_code, admin_area)
        if by_name is not SubRegion.UNSPECIFIED:
            return by_name
    return subregion_from_longitude(country_code, longitude)


def timezone_for(subregion: SubRegion) -> str | None:
    return SUBREGION_TIMEZONES.get(subregion)


def belongs_to(subregion: SubRegion, region: Region) -> bool:
    """Whether a band is inside a market.

    UNSPECIFIED belongs to everything: it is the absence of a claim, not a claim
    about somewhere else.
    """
    if subregion is SubRegion.UNSPECIFIED:
        return True
    return SUBREGION_REGIONS.get(subregion) is region


__all__ = [
    "SPLIT_ADMIN_AREAS",
    "SUBREGION_REGIONS",
    "SUBREGION_TIMEZONES",
    "belongs_to",
    "subregion_for_location",
    "subregion_from_admin_area",
    "subregion_from_longitude",
    "timezone_for",
]
