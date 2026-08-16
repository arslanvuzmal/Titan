"""Turning Smartlead's per-lead statistics into delivery events.

The table and the enum for these existed and had never held a row: they were
built to receive callbacks, and nothing was configured to send any. So the
tests that matter are not the field mapping. They are the two properties that
decide whether polling is safe to run on a schedule at all -- that the same row
read twice produces nothing the second time, and that an event is never
attributed to somebody on a guess.
"""

from __future__ import annotations

import datetime as dt

from titan.db.enums import SmartleadEventType
from titan.delivery.smartlead_events import events_from_row, fingerprint

CAMPAIGN = "3770052"

SENT_AT = "2026-08-14T09:12:00.000Z"


def row(**overrides) -> dict:
    base = {
        "stats_id": "007abe50-74b7-4d33-ac14-dc5263454007",
        "lead_email": "Bedford@NRGGym.com",
        "sequence_number": 1,
        "sent_time": SENT_AT,
        "open_time": None,
        "click_time": None,
        "reply_time": None,
        "open_count": 0,
        "click_count": 0,
        "is_bounced": False,
        "is_unsubscribed": False,
        "email_subject": "Broken navigation link on nrggym.com",
        "email_message": "<p>" + "x" * 4000 + "</p>",
    }
    base.update(overrides)
    return base


def kinds(events) -> set[SmartleadEventType]:
    return {e.event_type for e in events}


# ------------------------------------------------------------------ identity


def test_the_same_row_read_twice_produces_the_same_fingerprints() -> None:
    """The property the whole poller rests on.

    Nothing tracks a cursor. Re-reading is meant to be free, and it is only free
    if a second read collides with the first on the unique constraint instead of
    inserting a duplicate.
    """
    first = events_from_row(row(), campaign_id=CAMPAIGN)
    second = events_from_row(row(), campaign_id=CAMPAIGN)

    assert [e.fingerprint for e in first] == [e.fingerprint for e in second]


def test_each_event_from_one_row_gets_its_own_fingerprint() -> None:
    """A send and the open that followed it are two facts, not one.

    Deriving the fingerprint from ``stats_id`` alone would collapse them, and
    the row would only ever record whichever event was seen first.
    """
    events = events_from_row(
        row(open_time=SENT_AT, reply_time=SENT_AT), campaign_id=CAMPAIGN
    )

    assert len({e.fingerprint for e in events}) == len(events) > 1


def test_a_different_step_to_the_same_lead_is_a_different_event() -> None:
    step_one = events_from_row(row(stats_id="a"), campaign_id=CAMPAIGN)
    step_two = events_from_row(row(stats_id="b"), campaign_id=CAMPAIGN)

    assert {e.fingerprint for e in step_one}.isdisjoint(
        {e.fingerprint for e in step_two}
    )


def test_the_fingerprint_fits_the_column() -> None:
    assert len(fingerprint("abc", SmartleadEventType.SENT)) == 64


# --------------------------------------------------------------- attribution


def test_a_row_with_no_address_is_not_recorded() -> None:
    """An event nobody can be identified from is not evidence about anybody."""
    assert events_from_row(row(lead_email=""), campaign_id=CAMPAIGN) == []
    assert events_from_row(row(lead_email=None), campaign_id=CAMPAIGN) == []


def test_a_row_with_no_stats_id_is_not_recorded() -> None:
    """Without a stable id there is no way to not record it twice."""
    assert events_from_row(row(stats_id=""), campaign_id=CAMPAIGN) == []


def test_the_address_is_normalised() -> None:
    """It is matched against ``smartlead_normalized_email``, which is lowercase.

    Carrying the provider's casing through would make attribution depend on how
    somebody typed their address into a form.
    """
    events = events_from_row(row(), campaign_id=CAMPAIGN)

    assert {e.normalized_email for e in events} == {"bedford@nrggym.com"}


# ------------------------------------------------------------------ outcomes


def test_a_plain_send_is_one_event() -> None:
    events = events_from_row(row(), campaign_id=CAMPAIGN)

    assert kinds(events) == {SmartleadEventType.SENT}


def test_nothing_is_recorded_for_a_row_that_never_sent() -> None:
    """Smartlead lists queued leads with a null ``sent_time``.

    Recording those as sends is how a system talks itself into a delivery rate
    it never achieved.
    """
    assert events_from_row(row(sent_time=None), campaign_id=CAMPAIGN) == []


def test_opens_clicks_and_replies_each_become_their_own_event() -> None:
    events = events_from_row(
        row(open_time=SENT_AT, click_time=SENT_AT, reply_time=SENT_AT),
        campaign_id=CAMPAIGN,
    )

    assert kinds(events) == {
        SmartleadEventType.SENT,
        SmartleadEventType.OPENED,
        SmartleadEventType.CLICKED,
        SmartleadEventType.REPLIED,
    }


def test_a_bounce_is_dated_to_the_send_that_caused_it() -> None:
    """Smartlead reports the bounce as a boolean with no time of its own.

    Dating it to the poll would put an event that happened days ago inside this
    hour, which is the one distortion an event log must not introduce.
    """
    events = events_from_row(row(is_bounced=True), campaign_id=CAMPAIGN)
    bounce = next(e for e in events if e.event_type is SmartleadEventType.BOUNCED)

    assert bounce.occurred_at == dt.datetime(2026, 8, 14, 9, 12, tzinfo=dt.UTC)


def test_an_unsubscribe_becomes_an_event() -> None:
    events = events_from_row(row(is_unsubscribed=True), campaign_id=CAMPAIGN)

    assert SmartleadEventType.UNSUBSCRIBED in kinds(events)


# ----------------------------------------------------------------- the payload


def test_the_rendered_body_is_not_kept() -> None:
    """``email_message`` is the whole email and is not evidence of delivery.

    Keeping it would make the event log mostly a second copy of the outbox, at
    roughly a hundred times the size of the fields that answer a question.
    """
    events = events_from_row(row(), campaign_id=CAMPAIGN)

    assert all("email_message" not in e.raw for e in events)
    assert all("sent_time" in e.raw for e in events)


def test_a_naive_timestamp_is_read_as_utc_rather_than_dropped() -> None:
    events = events_from_row(row(sent_time="2026-08-14 09:12:00"), campaign_id=CAMPAIGN)

    assert events[0].occurred_at.tzinfo is not None


def test_an_unparseable_timestamp_drops_only_that_event() -> None:
    events = events_from_row(
        row(open_time="not a date", reply_time=SENT_AT), campaign_id=CAMPAIGN
    )

    assert SmartleadEventType.OPENED not in kinds(events)
    assert SmartleadEventType.REPLIED in kinds(events)
