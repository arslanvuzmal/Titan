"""One Smartlead campaign per market, each on that market's clock.

There was one campaign and its schedule read ``Europe/London``, Monday to
Friday, 08:00 to 17:00 -- for every lead in every market. A Dubai recipient was
written to on a Friday, the one weekday the Gulf largely does not work, and a
Los Angeles recipient at midnight.
"""

from __future__ import annotations

import pytest
from titan.db.enums import Region
from titan.outreach.smartlead_markets import (
    MARKETS,
    MIN_NEW_LEADS_PER_DAY,
    MarketSchedule,
    all_schedules,
    campaign_name,
    daily_capacity,
    excluded_mailboxes,
    schedule_for,
    smartlead_weekday,
)
from titan.policy.schedule import REGION_WORKING_HOURS

# ------------------------------------------------------------- the day numbering


def test_monday_to_friday_survives_the_translation() -> None:
    """The failure this function exists to prevent.

    Titan counts Monday as 0; Smartlead counts Sunday as 0. Handing one to the
    other unchanged shifts the whole working week by a day -- mail on Sunday,
    silence on Friday -- and every field involved is a plausible small integer,
    so nothing rejects it.
    """
    uk = schedule_for(Region.UK)

    assert uk.days == (1, 2, 3, 4, 5)


def test_the_gulf_working_week_survives_it_too() -> None:
    """Sunday to Thursday, which is 6,0,1,2,3 in Titan's numbering and 0,1,2,3,4
    in Smartlead's. The two are easy to mistake for each other, and the mistake
    is invisible in either system."""
    gulf = schedule_for(Region.MIDDLE_EAST)

    assert gulf.days == (0, 1, 2, 3, 4)
    assert gulf.timezone == "Asia/Dubai"
    assert gulf.end_hour == "18:00"


def test_sunday_and_monday_are_the_two_that_matter() -> None:
    assert smartlead_weekday(0) == 1  # Monday
    assert smartlead_weekday(6) == 0  # Sunday


def test_the_translation_is_a_bijection() -> None:
    """Every day maps somewhere, and no two days map to the same place.

    A collision would silently drop a sending day from the week.
    """
    mapped = [smartlead_weekday(day) for day in range(7)]

    assert sorted(mapped) == list(range(7))


# ------------------------------------------------------------------ the markets


def test_every_market_gets_its_own_clock() -> None:
    schedules = all_schedules()

    assert len(schedules) == len(MARKETS)
    assert len({s.timezone for s in schedules}) == len(MARKETS), (
        "two markets share a timezone; one of them is on the wrong clock"
    )


def test_the_schedule_is_the_working_day_not_titans_send_window() -> None:
    """Titan opens an hour before work starts, deliberately. Smartlead is a
    second sender with its own bounds, and widening both by an hour compounds
    into two hours before anybody is at their desk."""
    for schedule in all_schedules():
        hours = REGION_WORKING_HOURS[schedule.region]

        assert schedule.start_hour == f"{hours.start_hour:02d}:00"
        assert schedule.end_hour == f"{hours.end_hour:02d}:00"


def test_no_market_is_left_on_london_time() -> None:
    """The state being replaced: one campaign, Europe/London, everybody."""
    on_london = [s.region for s in all_schedules() if s.timezone == "Europe/London"]

    assert on_london == [Region.UK]


def test_a_market_with_no_representative_clock_is_refused() -> None:
    """OTHER and UNSPECIFIED name no working week anybody keeps. A campaign on a
    guessed clock is worse than no campaign, because it looks configured."""
    with pytest.raises(ValueError):
        schedule_for(Region.UNSPECIFIED)
    with pytest.raises(ValueError):
        schedule_for(Region.OTHER)


def test_campaign_names_are_distinct_and_readable() -> None:
    """The operator picks a campaign out of this list by eye.

    Title-casing the enum yields "Titan-OS - Uk" and "Titan-OS - Usa", which is
    the kind of small wrong that reads as machine output.
    """
    names = [campaign_name(region) for region in MARKETS]

    assert len(set(names)) == len(names)
    assert campaign_name(Region.MIDDLE_EAST) == "Titan-OS - Middle East"
    assert campaign_name(Region.UK) == "Titan-OS - UK"
    assert campaign_name(Region.USA) == "Titan-OS - USA"


def test_a_market_outside_the_plan_has_no_name() -> None:
    """Refused rather than given a generated one: a campaign named after a
    market nobody chose would sit in the account looking deliberate."""
    with pytest.raises(ValueError):
        campaign_name(Region.OTHER)


def test_the_request_body_carries_every_field_smartlead_needs() -> None:
    """``max_new_leads_per_day`` is not optional: without it the endpoint
    answers 400 and the campaign is left created but unscheduled."""
    body = schedule_for(Region.USA).body(max_new_leads_per_day=100)

    assert body["timezone"] == "America/New_York"
    assert body["days_of_the_week"] == [1, 2, 3, 4, 5]
    assert body["start_hour"] == "09:00"
    assert body["end_hour"] == "17:00"
    assert body["min_time_btw_emails"] > 0
    assert body["max_new_leads_per_day"] == 100


# -------------------------------------------------------------- the daily ceiling


def test_capacity_is_what_the_account_permits_added_up() -> None:
    """Read from Smartlead, not configured in Titan. A second copy of somebody
    else's setting is wrong the first time they change it in their own UI."""
    boxes = [
        {"id": 1, "from_email": "sales@x.com", "message_per_day": 50},
        {"id": 2, "from_email": "outreach@x.com", "message_per_day": 50},
    ]

    assert daily_capacity(boxes, forbidden=set()) == 100


def test_capacity_excludes_the_mailbox_outreach_may_not_use() -> None:
    """Capacity Titan is not allowed to spend is not capacity.

    Counting ``projects@`` would let a campaign be configured for sixty more
    messages a day than it can actually send, and the shortfall would show up
    as leads that quietly never go out.
    """
    boxes = [
        {"id": 1, "from_email": "sales@x.com", "message_per_day": 50},
        {"id": 2, "from_email": "projects@x.com", "message_per_day": 60},
    ]

    assert daily_capacity(boxes, forbidden={"projects@x.com"}) == 50


def test_an_account_reporting_nothing_still_sends() -> None:
    """A missing field must not silently stop outreach.

    Zero new leads a day is a harder failure than a slow one, and it would look
    identical to a campaign nobody had leads for.
    """
    assert daily_capacity([], forbidden=set()) == MIN_NEW_LEADS_PER_DAY
    assert daily_capacity([{"id": 1, "from_email": "a@b.c"}], forbidden=set()) == (
        MIN_NEW_LEADS_PER_DAY
    )


def test_a_schedule_is_immutable() -> None:
    schedule = schedule_for(Region.UK)

    assert isinstance(schedule, MarketSchedule)
    with pytest.raises(AttributeError):
        schedule.timezone = "Pacific/Auckland"  # type: ignore[misc]


# ----------------------------------------------------------------- the mailboxes


def test_the_working_mailbox_is_never_attached_to_outreach() -> None:
    """A standing rule of the operator's, and the reason it is enforced in code
    rather than remembered: ``projects@`` is a real mailbox whose reputation was
    never meant to carry cold outreach."""
    boxes = [
        {"id": 1, "from_email": "sales@arslanvuzmallone.com"},
        {"id": 2, "from_email": "Projects@Arslanvuzmallone.com"},
        {"id": 3, "from_email": "outreach@arslanvuzmallone.com"},
    ]

    assert excluded_mailboxes(boxes, forbidden={"projects@arslanvuzmallone.com"}) == [
        1,
        3,
    ]


def test_a_mailbox_with_no_id_is_skipped_rather_than_crashing() -> None:
    """The account listing is somebody else's payload shape."""
    boxes = [
        {"from_email": "sales@arslanvuzmallone.com"},
        {"id": 7, "from_email": "x@y.z"},
    ]

    assert excluded_mailboxes(boxes, forbidden=set()) == [7]
