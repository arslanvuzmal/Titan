"""One Smartlead campaign per market, each on that market's clock.

There was one campaign, ``Titan-OS``, and its schedule read:

    {"tz": "Europe/London", "days": [1,2,3,4,5],
     "startHour": "08:00", "endHour": "17:00"}

Every lead went out on London time. A Dubai recipient was written to at 12:00
their time on a Friday -- the one weekday the Gulf largely does not work -- and
a Los Angeles recipient at midnight. Titan has known each market's real working
hours since Phase 02 and had no way to tell the system that does the sending.

**Derived from the same table the rest of Titan uses.** Hours, days and the
representative timezone all come from ``titan.policy.schedule``. Typing "09:00"
into a Gulf campaign is two errors at once -- the Gulf works to 18:00 and works
Sunday to Thursday -- and a second copy of the working week would drift from the
first the day either is edited.

**One campaign per market, not per timezone band.** Smartlead holds one clock
per campaign, so a fully correct split would be seventeen campaigns across five
US bands, four Canadian and three Australian. The market's representative zone
is the earliest in each case, which errs toward sending inside somebody's day
rather than before it -- the same trade ``REGION_TIMEZONES`` already documents.
Per-lead precision is not lost: discovery stamps every organisation with its own
metro's zone, and Titan's own send window uses that.

**Nothing here starts a campaign or attaches a lead.** Smartlead creates a
campaign DRAFTED and this leaves it there. Leads reach it only through the
existing import path, behind verification, approval, suppression and compliance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from titan.db.enums import Region
from titan.policy.schedule import REGION_TIMEZONES, REGION_WORKING_HOURS

#: Campaign name prefix. The market follows it, so the account lists as
#: "Titan-OS - UK", "Titan-OS - Middle East" and so on.
NAME_PREFIX = "Titan-OS"

#: The markets that get a campaign. OTHER and UNSPECIFIED are excluded: neither
#: names a working week anybody keeps, and a campaign on a guessed clock is
#: worse than no campaign, because it looks configured.
MARKETS: tuple[Region, ...] = (
    Region.UK,
    Region.USA,
    Region.CANADA,
    Region.EUROPE,
    Region.MIDDLE_EAST,
    Region.AUSTRALIA,
)

#: Minutes between two sends from the same mailbox. Copied from the live
#: campaign rather than chosen here: it is a deliverability setting the operator
#: has already tuned, and this module is about clocks.
MIN_MINUTES_BETWEEN_EMAILS = 10

#: Titan's four-step sequence as Smartlead holds it. The bodies are merge
#: variables, not content: Titan composes and a human approves each message, and
#: Smartlead is handed the approved text per lead. Delays match
#: ``STEP_DELAYS_IN_DAYS``.
#:
#: Steps two onward carry an empty subject, which is how Smartlead threads a
#: follow-up onto the original message rather than starting a new conversation.
#:
#: The delay key is ``delay_in_days`` on write and comes back as
#: ``delayInDays`` on read. Sending the shape the API returns earns a 400,
#: which is the sort of asymmetry that is only ever learned from the error.
SEQUENCE_STEPS: tuple[dict[str, Any], ...] = (
    {
        "seq_number": 1,
        "seq_delay_details": {"delay_in_days": 0},
        "subject": "{{approved_subject}}",
        "email_body": "{{approved_body}}",
    },
    {
        "seq_number": 2,
        "seq_delay_details": {"delay_in_days": 3},
        "subject": "",
        "email_body": "{{approved_body}}",
    },
    {
        "seq_number": 3,
        "seq_delay_details": {"delay_in_days": 4},
        "subject": "",
        "email_body": "{{approved_body}}",
    },
    {
        "seq_number": 4,
        "seq_delay_details": {"delay_in_days": 5},
        "subject": "",
        "email_body": "{{approved_body}}",
    },
)


#: How each market is written in the account listing. Spelled out rather than
#: title-cased from the enum, which yields "Uk" and "Usa" -- the operator reads
#: this list, and a campaign is picked from it by eye.
MARKET_LABELS: dict[Region, str] = {
    Region.UK: "UK",
    Region.USA: "USA",
    Region.CANADA: "Canada",
    Region.EUROPE: "Europe",
    Region.MIDDLE_EAST: "Middle East",
    Region.AUSTRALIA: "Australia",
}


def campaign_name(region: Region) -> str:
    label = MARKET_LABELS.get(region)
    if label is None:
        raise ValueError(f"{region.value} is not an outreach market")
    return f"{NAME_PREFIX} - {label}"


def smartlead_weekday(python_weekday: int) -> int:
    """Python's weekday numbering into Smartlead's.

    ``datetime.weekday()`` is Monday 0 through Sunday 6. Smartlead is Sunday 0
    through Saturday 6. Passing one where the other is expected shifts the whole
    working week by a day: Monday-to-Friday becomes Sunday-to-Thursday, which
    for the Gulf happens to be right and for everywhere else means mail on a
    Sunday and silence on a Friday.
    """
    return (python_weekday + 1) % 7


@dataclass(frozen=True, slots=True)
class MarketSchedule:
    """What one market's Smartlead campaign should be set to."""

    region: Region
    timezone: str
    days: tuple[int, ...]
    start_hour: str
    end_hour: str

    def body(self, *, max_new_leads_per_day: int) -> dict[str, Any]:
        """The request body for ``POST /campaigns/{id}/schedule``.

        ``max_new_leads_per_day`` is required by the endpoint -- omitting it
        returns a 400 -- and is passed in rather than stored because it is a
        fact about the account's mailboxes, not about this market's clock.
        """
        return {
            "timezone": self.timezone,
            "days_of_the_week": list(self.days),
            "start_hour": self.start_hour,
            "end_hour": self.end_hour,
            "min_time_btw_emails": MIN_MINUTES_BETWEEN_EMAILS,
            "max_new_leads_per_day": max_new_leads_per_day,
        }

    def describe(self) -> str:
        return f"{self.timezone} {self.start_hour}-{self.end_hour} days={list(self.days)}"


def schedule_for(region: Region) -> MarketSchedule:
    """The market's working day, as Smartlead needs it stated.

    The *working* hours, not Titan's send window. Titan opens an hour early on
    purpose -- a cold approach gets one reliable pass, the first one -- but that
    lead-in is Titan's policy about its own outbox. Smartlead is a second sender
    with its own bounds, and widening both by an hour compounds into two.
    """
    timezone = REGION_TIMEZONES.get(region)
    if timezone is None:
        raise ValueError(f"{region.value} has no representative timezone")
    hours = REGION_WORKING_HOURS[region]
    return MarketSchedule(
        region=region,
        timezone=timezone,
        days=tuple(smartlead_weekday(day) for day in hours.days),
        start_hour=f"{hours.start_hour:02d}:00",
        end_hour=f"{hours.end_hour:02d}:00",
    )


def all_schedules() -> tuple[MarketSchedule, ...]:
    return tuple(schedule_for(region) for region in MARKETS)


#: A campaign that asks for no new leads sends nothing at all, which is a
#: harder failure than sending slowly. Used when the account reports no usable
#: mailbox limit, so a missing field cannot silently stop outreach.
MIN_NEW_LEADS_PER_DAY = 1


def daily_capacity(mailboxes: list[dict[str, Any]], *, forbidden: set[str]) -> int:
    """How many messages a day the account actually permits.

    The sum of the per-mailbox limits the operator has set in Smartlead, across
    the mailboxes outreach is allowed to use. Read from the account rather than
    configured here: this is somebody else's setting, and a second copy of it in
    Titan would be wrong the first time it was changed in the Smartlead UI.

    ``projects@`` is excluded from the sum for the same reason it is excluded
    from the attachment -- capacity Titan may not use is not capacity.

    This is the platform's own ceiling, not a target. Smartlead still enforces
    each mailbox's limit individually, so a campaign allowed this many new leads
    cannot use them to exceed any one mailbox.
    """
    lowered = {address.strip().lower() for address in forbidden}
    total = 0
    for box in mailboxes:
        if str(box.get("from_email", "")).strip().lower() in lowered:
            continue
        limit = box.get("message_per_day")
        if isinstance(limit, int | float) and limit > 0:
            total += int(limit)
    return max(total, MIN_NEW_LEADS_PER_DAY)


def excluded_mailboxes(
    mailboxes: list[dict[str, Any]], *, forbidden: set[str]
) -> list[int]:
    """Which mailbox ids may be attached to an outreach campaign.

    ``projects@`` is held out under the operator's standing rule: it is a real
    working mailbox and attaching it to cold outreach puts its reputation behind
    a campaign it was never meant to carry. Filtering by address rather than by
    id because ids change when a mailbox is re-connected and the address does
    not.
    """
    lowered = {address.strip().lower() for address in forbidden}
    return [
        int(box["id"])
        for box in mailboxes
        if box.get("id") is not None
        and str(box.get("from_email", "")).strip().lower() not in lowered
    ]


__all__ = [
    "MARKETS",
    "MARKET_LABELS",
    "MIN_MINUTES_BETWEEN_EMAILS",
    "MIN_NEW_LEADS_PER_DAY",
    "NAME_PREFIX",
    "SEQUENCE_STEPS",
    "MarketSchedule",
    "all_schedules",
    "campaign_name",
    "daily_capacity",
    "excluded_mailboxes",
    "schedule_for",
    "smartlead_weekday",
]
