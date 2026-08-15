"""When a campaign may write to somebody, in that person's own time.

The rule this replaces was one process-wide quiet-hours window, 20:00 to 08:00,
shared by every campaign in every market. It models the wrong thing twice.

It models *night*, not *work*. A message landing at 14:00 on a Sunday clears a
quiet-hours check comfortably and is still a cold approach arriving at somebody's
weekend. Working hours are what outreach actually wants, and they are bounded on
both sides.

And one window cannot be right for two markets at once. A single setting is
either correct for London or correct for Los Angeles; it has never been capable
of being correct for both, which is the whole difficulty of running six markets
from one process.

**Region supplies what the recipient does not.** Timezone comes off the
organisation's location where Titan has one, and a great many organisations have
none. The old check failed closed on that -- unknown timezone meant no send, ever
-- which is safe and quietly discards every lead whose address Places did not
resolve. A campaign declaring its market can now answer for them.

**The global quiet hours stay, underneath, as a floor.** A campaign window is
configuration, and configuration can be wrong: 22:00 to 23:00 is a legal pair of
integers. The process-wide setting remains a bound that no campaign can
configure its way past.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass, field

from titan.db.enums import Region, SubRegion
from titan.policy.subregions import timezone_for

#: Given a date, the name of the holiday falling on it, or None.
#: ``titan.policy.calendars.holiday_on`` bound to a country, in practice.
HolidayLookup = Callable[[dt.date], str | None]

#: A representative business timezone per market, used only when the recipient
#: has none of their own.
#:
#: Representative, not exhaustive -- the USA spans four zones and this names one.
#: That is a deliberate simplification of a market into a single clock, and it is
#: what regional segmentation exists to refine. Eastern is chosen for the USA
#: because it is the earliest: a window computed against it opens later in local
#: terms everywhere west, which errs toward sending inside somebody's day rather
#: than before it.
REGION_TIMEZONES: dict[Region, str | None] = {
    Region.USA: "America/New_York",
    Region.CANADA: "America/Toronto",
    Region.UK: "Europe/London",
    Region.EUROPE: "Europe/Berlin",
    Region.AUSTRALIA: "Australia/Sydney",
    Region.MIDDLE_EAST: "Asia/Dubai",
    Region.OTHER: None,
    Region.UNSPECIFIED: None,
}

#: Monday is 0, matching ``datetime.weekday()``.
MONDAY_TO_FRIDAY: tuple[int, ...] = (0, 1, 2, 3, 4)
SUNDAY_TO_THURSDAY: tuple[int, ...] = (6, 0, 1, 2, 3)

#: The working week a new campaign starts with, per market.
#:
#: Middle East is the one that is not Monday to Friday, and it is also the one
#: that is genuinely contested. Saudi Arabia and most of the Levant work Sunday
#: to Thursday. The UAE moved its public sector to Monday to Friday in 2022 and
#: its private sector did not follow uniformly. Sunday to Thursday is the safer
#: default of the two: sending on a Sunday to somebody who does not work Sundays
#: is a message read on Monday, while sending on a Friday to somebody observing
#: Jumu'ah is a message read as ignorant. A UAE-specific campaign should override
#: this, which is why it is a per-campaign field rather than a constant.
REGION_SEND_DAYS: dict[Region, tuple[int, ...]] = {
    Region.USA: MONDAY_TO_FRIDAY,
    Region.CANADA: MONDAY_TO_FRIDAY,
    Region.UK: MONDAY_TO_FRIDAY,
    Region.EUROPE: MONDAY_TO_FRIDAY,
    Region.AUSTRALIA: MONDAY_TO_FRIDAY,
    Region.MIDDLE_EAST: SUNDAY_TO_THURSDAY,
    Region.OTHER: MONDAY_TO_FRIDAY,
    Region.UNSPECIFIED: MONDAY_TO_FRIDAY,
}

DEFAULT_START_HOUR = 8
DEFAULT_END_HOUR = 17

#: How far ahead ``next_open`` will look before giving up. Ten days covers a
#: window closed for a long weekend either side of a working week; a window
#: configured with no open days at all is a configuration error and returns
#: nothing rather than looping.
_MAX_LOOKAHEAD_DAYS = 10


@dataclass(frozen=True, slots=True)
class SendWindow:
    """The hours and days a campaign may write, in the recipient's local time."""

    start_hour: int = DEFAULT_START_HOUR
    end_hour: int = DEFAULT_END_HOUR
    days: tuple[int, ...] = field(default=MONDAY_TO_FRIDAY)

    @property
    def is_usable(self) -> bool:
        """Whether this window can ever be open.

        A window with no days, or whose end is not after its start, closes the
        campaign permanently. Reported rather than corrected: guessing at what
        somebody meant by 17:00 to 08:00 would be inventing a policy.
        """
        return bool(self.days) and 0 <= self.start_hour < self.end_hour <= 24

    def is_open_at(
        self, local: dt.datetime, holidays: HolidayLookup | None = None
    ) -> bool:
        """Whether a local moment falls inside the window.

        The end hour is exclusive: 17 means the last minute is 16:59, not 17:59.
        A window ending "at five" that sends at 17:45 is the kind of small wrong
        that reads as automation.

        ``holidays`` answers whether a given date is a public holiday where the
        recipient is. Absent, the window knows only about the working week --
        which is the behaviour before calendars existed, and sends on Christmas.
        """
        if not self.is_usable:
            return False
        if local.weekday() not in self.days:
            return False
        if holidays is not None and holidays(local.date()) is not None:
            return False
        return self.start_hour <= local.hour < self.end_hour

    def next_open_from(
        self, local: dt.datetime, holidays: HolidayLookup | None = None
    ) -> dt.datetime | None:
        """The next local moment this window opens, at or after ``local``.

        Used to schedule a deferral. Without it a message refused at 18:00 local
        retries at the next UTC midnight, which for a Pacific recipient is 16:00
        their afternoon the *previous* day in the worst case and never lines up
        with their morning in the best.
        """
        if not self.is_usable:
            return None
        if self.is_open_at(local, holidays):
            return local

        def opens(day: dt.datetime) -> bool:
            if day.weekday() not in self.days:
                return False
            return holidays is None or holidays(day.date()) is None

        candidate = local
        for _ in range(_MAX_LOOKAHEAD_DAYS):
            if opens(candidate) and candidate.hour < self.start_hour:
                return candidate.replace(
                    hour=self.start_hour, minute=0, second=0, microsecond=0
                )
            # Past the window today, or not a working day: try tomorrow morning.
            candidate = (candidate + dt.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            if opens(candidate):
                return candidate.replace(hour=self.start_hour)
        return None

    def describe(self) -> str:
        if not self.is_usable:
            return "no valid send window configured"
        names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        chosen = ", ".join(names[d] for d in sorted(self.days))
        return f"{self.start_hour:02d}:00-{self.end_hour:02d}:00 on {chosen}"


def default_window_for(region: Region) -> SendWindow:
    return SendWindow(days=REGION_SEND_DAYS.get(region, MONDAY_TO_FRIDAY))


def resolve_timezone(
    recipient_timezone: str | None,
    region: Region,
    *,
    recipient_subregion: SubRegion = SubRegion.UNSPECIFIED,
    campaign_subregion: SubRegion = SubRegion.UNSPECIFIED,
) -> str | None:
    """The clock to schedule against, most specific source first.

    1. The recipient's own timezone. A fact about them, and exact.
    2. The band their address falls in. Still a fact about them, derived from
       their state or their coordinates, and right to the hour everywhere the
       market's single clock is right only on one coast.
    3. The band the campaign declares. A US Pacific campaign should schedule its
       unresolved leads on Pacific rather than on the market default, which is
       Eastern and three hours early for them.
    4. The market. One clock for a continent, which is where this started.

    Where none of the four answers the caller must refuse rather than guess at a
    local hour.
    """
    if recipient_timezone and recipient_timezone.strip():
        return recipient_timezone.strip()
    for band in (recipient_subregion, campaign_subregion):
        zone = timezone_for(band)
        if zone:
            return zone
    return REGION_TIMEZONES.get(region)


def local_time(moment: dt.datetime, timezone: str | None) -> dt.datetime | None:
    """``moment`` in ``timezone``, or None when the zone is unusable.

    A bad zone name returns None rather than raising or falling back to UTC.
    Falling back to UTC is the dangerous option: it looks like an answer, and it
    is wrong by up to eleven hours.
    """
    if not timezone:
        return None
    try:
        return moment.astimezone(zoneinfo.ZoneInfo(timezone))
    except Exception:
        return None


__all__ = [
    "DEFAULT_END_HOUR",
    "DEFAULT_START_HOUR",
    "MONDAY_TO_FRIDAY",
    "REGION_SEND_DAYS",
    "REGION_TIMEZONES",
    "SUNDAY_TO_THURSDAY",
    "SendWindow",
    "default_window_for",
    "local_time",
    "resolve_timezone",
]
