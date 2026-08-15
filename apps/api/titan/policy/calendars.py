"""Days a business is closed, in the country the business is in.

The send window already knows about the working week -- Monday to Friday, or
Sunday to Thursday in the Gulf. It does not know that the 25th of December is
not a working day, so a campaign sending on weekdays sends on Christmas.

A cold approach arriving on a public holiday is not merely ignored. It arrives
at the top of a pile that is read on the first morning back, alongside everything
else that accumulated, and it is the message that announces it was sent by
something that did not know what day it was.

**Not a table in this repository.** The obvious implementation is a dict of
dates per market, and it is wrong in a way that is hard to see: it goes stale
every January without anything failing. Easter moves. A holiday landing on a
Saturday is observed on the following Monday in the UK and not in Germany. A
government adds a coronation day. None of that produces an error -- it produces
mail sent on a holiday, silently, a year after somebody stopped updating the
file. The ``holidays`` package tracks all of it and is the dependency this is
worth.

**Country-level, with the subdivision when it is offered.** National holidays
are the ones that close businesses, and they are the ones a wrong answer costs
most. Where the recipient's address carries a subdivision the library
recognises -- an Australian state, a Canadian province -- it is used, because
those markets have genuinely divergent calendars. Where it does not, the country
calendar stands rather than nothing.

**Only where a country is unambiguous.** A campaign declaring EUROPE or
MIDDLE_EAST names a market spanning many countries with different calendars, so
those fall back to nothing rather than to a guess: no calendar means no holiday
blocking, which is the same behaviour as before this existed and is safe.
"""

from __future__ import annotations

import datetime as dt
import logging
from functools import lru_cache

from titan.db.enums import Region

logger = logging.getLogger(__name__)

#: Markets that resolve to exactly one country. Europe and the Middle East are
#: absent on purpose -- each spans a dozen calendars, and picking one would put
#: a German business on French holidays.
REGION_COUNTRIES: dict[Region, str | None] = {
    Region.USA: "US",
    Region.CANADA: "CA",
    Region.UK: "GB",
    Region.AUSTRALIA: "AU",
    Region.EUROPE: None,
    Region.MIDDLE_EAST: None,
    Region.OTHER: None,
    Region.UNSPECIFIED: None,
}


@lru_cache(maxsize=64)
def _calendar(country: str, subdiv: str | None):  # type: ignore[no-untyped-def]
    """The holiday calendar for one country, cached.

    Built per country rather than per message: the library populates a year on
    first lookup and keeps it, so one object serves every send to that country
    for the life of the process.

    An unrecognised subdivision falls back to the country calendar rather than
    raising. Places returns administrative areas in whatever form the country
    uses, and most of them are not the ISO codes this library wants -- losing
    the state calendar is a small loss, and losing the national one over a
    string mismatch would be a large one.
    """
    import holidays

    if subdiv:
        try:
            return holidays.country_holidays(country, subdiv=subdiv)
        except Exception:
            logger.debug(
                "unrecognised subdivision; using the country calendar",
                extra={"country": country, "subdiv": subdiv},
            )
    try:
        return holidays.country_holidays(country)
    except Exception:
        logger.debug("no holiday calendar for country", extra={"country": country})
        return None


def resolve_country(
    recipient_country: str | None,
    region: Region,
) -> str | None:
    """Whose calendar applies.

    The recipient's own country wins: it is a fact about them, and the region is
    a fact about the campaign. The region answers only where it names one
    country unambiguously.
    """
    if recipient_country and recipient_country.strip():
        return recipient_country.strip().upper()
    return REGION_COUNTRIES.get(region)


def holiday_on(
    day: dt.date,
    *,
    country: str | None,
    subdiv: str | None = None,
) -> str | None:
    """The name of the holiday falling on ``day``, or None.

    Returns the name rather than a boolean so a deferral can say *which*
    holiday it is waiting out. "outside the send window" is a worse answer than
    "Christmas Day".
    """
    if not country:
        return None
    calendar = _calendar(country, (subdiv or "").strip().upper() or None)
    if calendar is None:
        return None
    try:
        name = calendar.get(day)
    except Exception:
        # A year the library cannot build -- far future, or a country whose data
        # starts later than the date asked about. Not a holiday, and not an
        # error worth stopping a send for.
        return None
    return str(name) if name else None


def is_working_day(
    day: dt.date,
    *,
    country: str | None,
    subdiv: str | None = None,
) -> bool:
    """Whether a business in this country is open on this date.

    Weekends are not considered here. The working week is the campaign's own
    ``send_days``, because it differs by market -- Sunday to Thursday in the
    Gulf -- and this module has no opinion about which days a campaign works.
    """
    return holiday_on(day, country=country, subdiv=subdiv) is None


def clear_cache() -> None:
    """Drop the cached calendars. For tests, and for a long-lived process that
    wants to pick up a library upgrade without a restart."""
    _calendar.cache_clear()


__all__ = [
    "REGION_COUNTRIES",
    "clear_cache",
    "holiday_on",
    "is_working_day",
    "resolve_country",
]
