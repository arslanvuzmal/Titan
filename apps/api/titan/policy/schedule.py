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

**Sending opens an hour before work does.** The window is not the working day;
it is the working day with an hour in front of it. A cold approach gets one
reliable pass -- the first one, before the inbox has filled -- so arriving at
08:00 for a 09:00 market is worth more than arriving at 11:00. That hour is
derived from the market's working hours rather than typed in, because a literal
``8`` is right for a 09:00 market, an hour late for Germany, and carries no
record of which it was meant to be.

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


@dataclass(frozen=True, slots=True)
class WorkingHours:
    """When a market actually works, in its own local time.

    Distinct from a :class:`SendWindow` on purpose, and the distinction is the
    point: these are the recipient's hours, a fact about them, while the send
    window is ours and is *derived* from these. Collapsing the two -- which is
    what a bare ``start_hour = 8`` did -- loses the reason the window opens when
    it does, so nobody editing it later can tell whether 08:00 was a considered
    choice or a leftover.
    """

    #: First hour of the working day. End hour is exclusive.
    start_hour: int
    end_hour: int
    days: tuple[int, ...]


#: The local working day per market.
#:
#: Middle East is the one that is not Monday to Friday, and it is also the one
#: that is genuinely contested. Saudi Arabia and most of the Levant work Sunday
#: to Thursday. The UAE moved its public sector to Monday to Friday in 2022 and
#: its private sector did not follow uniformly. Sunday to Thursday is the safer
#: default of the two: sending on a Sunday to somebody who does not work Sundays
#: is a message read on Monday, while sending on a Friday to somebody observing
#: Jumu'ah is a message read as ignorant. A UAE-specific campaign should override
#: this, which is why the result lands in per-campaign columns rather than being
#: read from here at send time.
#:
#: Hours are conservative where a market is split. UK offices commonly run to
#: 17:30 and this says 17, because the end hour is exclusive and stopping before
#: people leave costs one hour of sending, where sending after they have left
#: costs a reply.
REGION_WORKING_HOURS: dict[Region, WorkingHours] = {
    Region.USA: WorkingHours(9, 17, MONDAY_TO_FRIDAY),
    Region.CANADA: WorkingHours(9, 17, MONDAY_TO_FRIDAY),
    Region.UK: WorkingHours(9, 17, MONDAY_TO_FRIDAY),
    # Germany is the representative clock, and German offices start at eight.
    # This is the one market whose lead-in reaches the floor below.
    Region.EUROPE: WorkingHours(8, 17, MONDAY_TO_FRIDAY),
    Region.AUSTRALIA: WorkingHours(9, 17, MONDAY_TO_FRIDAY),
    # Gulf offices commonly run 09:00-18:00.
    Region.MIDDLE_EAST: WorkingHours(9, 18, SUNDAY_TO_THURSDAY),
    Region.OTHER: WorkingHours(9, 17, MONDAY_TO_FRIDAY),
    Region.UNSPECIFIED: WorkingHours(9, 17, MONDAY_TO_FRIDAY),
}

#: The working week a new campaign starts with, per market. Derived rather than
#: written out a second time: two tables that must agree eventually will not.
REGION_SEND_DAYS: dict[Region, tuple[int, ...]] = {
    region: hours.days for region, hours in REGION_WORKING_HOURS.items()
}

#: How far before the working day a campaign may begin sending.
#:
#: Mail that arrives during the working day lands partway down an inbox that has
#: been filling since somebody sat down. Mail that arrives an hour before lands
#: at the top of the first pass, which is the only pass a cold approach reliably
#: gets. One hour, not three: at 06:00 the message is no longer near the top by
#: 09:00, it is merely older, and a timestamp far outside working hours is
#: itself a mark of automation.
LEAD_IN_HOURS = 1

#: The earliest local hour the lead-in may reach. Binds for Europe, whose
#: working day starts at 08:00. A floor rather than an assertion because the
#: table above is editable and a market entered with a 06:00 start would
#: otherwise open the window at 05:00, which reads as a machine.
EARLIEST_SEND_HOUR = 7

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


def working_hours_for(region: Region) -> WorkingHours:
    """The market's local working day, or a conventional one where it has none."""
    return REGION_WORKING_HOURS.get(region, WorkingHours(9, 17, MONDAY_TO_FRIDAY))


def lead_in_start_hour(working_start_hour: int) -> int:
    """The hour sending opens, given the hour work begins.

    Clamped at both ends. ``EARLIEST_SEND_HOUR`` stops the lead-in reaching into
    the night, and a working day starting at midnight cannot produce -1.
    """
    return max(EARLIEST_SEND_HOUR, working_start_hour - LEAD_IN_HOURS, 0)


def default_window_for(region: Region) -> SendWindow:
    """The send window a new campaign in this market starts with.

    The window opens ``LEAD_IN_HOURS`` before the market's working day and
    closes when it does, on the days that market works. So a US campaign sends
    08:00-17:00 Monday to Friday, and a Middle East one sends 08:00-18:00 Sunday
    to Thursday -- both an hour ahead of their own morning rather than an hour
    ahead of ours.

    This is a *starting point*, written into the campaign's own columns at
    creation. It is not consulted again at send time, so a human who edits the
    window keeps their edit.
    """
    hours = working_hours_for(region)
    return SendWindow(
        start_hour=lead_in_start_hour(hours.start_hour),
        end_hour=hours.end_hour,
        days=hours.days,
    )


def describe_derivation(region: Region) -> str:
    """Why the window is what it is, for an audit entry or an operator.

    Recorded at creation because the derivation is not recoverable from the
    stored hours afterwards: 08:00 could be a 09:00 market with an hour's
    lead-in, or a market that simply starts at eight.
    """
    hours = working_hours_for(region)
    window = default_window_for(region)
    opens = window.start_hour
    clamped = (
        " (held at the earliest permitted hour)"
        if opens > hours.start_hour - LEAD_IN_HOURS
        else ""
    )
    return (
        f"{region.value} works {hours.start_hour:02d}:00-{hours.end_hour:02d}:00 "
        f"local; sending opens {opens:02d}:00{clamped}, "
        f"{LEAD_IN_HOURS}h ahead of the working day"
    )


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
    "EARLIEST_SEND_HOUR",
    "LEAD_IN_HOURS",
    "MONDAY_TO_FRIDAY",
    "REGION_SEND_DAYS",
    "REGION_TIMEZONES",
    "REGION_WORKING_HOURS",
    "SUNDAY_TO_THURSDAY",
    "SendWindow",
    "WorkingHours",
    "default_window_for",
    "describe_derivation",
    "lead_in_start_hour",
    "local_time",
    "resolve_timezone",
    "working_hours_for",
]
