"""Reading delivery outcomes out of Smartlead's per-lead statistics.

Smartlead will push callbacks for these events, and pushing is the better
design: it is immediate, and it reports each event once. It also requires a
public HTTPS endpoint for Smartlead to reach, and there is not one. The result
was a table, a parser and a state-rank guard that had never seen a row, while
every message the system actually sent went unmeasured.

So the outcomes are pulled instead, from ``/campaigns/{id}/statistics`` -- the
one endpoint that reports what happened to an *individual* send rather than a
campaign total. Totals cannot answer the two questions that change behaviour:
which address bounced, and did anyone reply.

**One row becomes several events.** A statistics row is a lead's current state
for one sequence step, carrying a sent time, an open time, a reply time and two
booleans side by side. Splitting them into separate events is what lets the
record say *when* each thing happened rather than only that it eventually did.

**The fingerprint is derived from the row's own identity**, not from when it was
read. ``stats_id`` is stable across pages and across calls, so polling the same
window twice writes nothing the second time. That is the whole basis for the
poller being safe to run on a schedule: it is a full re-read every time, and
re-reading is free.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from typing import Any

from titan.db.enums import SmartleadEventType

#: Fields copied into ``raw_payload``. ``email_message`` is deliberately absent:
#: it is the full rendered body, it is the largest field by two orders of
#: magnitude, and it is not evidence of delivery. Keeping it would make the
#: event log mostly a second copy of the outbox.
_KEPT_FIELDS = (
    "stats_id",
    "lead_email",
    "lead_name",
    "lead_category",
    "sequence_number",
    "email_campaign_seq_id",
    "seq_variant_id",
    "email_subject",
    "sent_time",
    "open_time",
    "click_time",
    "reply_time",
    "open_count",
    "click_count",
    "is_bounced",
    "is_unsubscribed",
)


@dataclass(frozen=True, slots=True)
class DeliveryEvent:
    """One thing that happened to one send, ready to be recorded."""

    fingerprint: str
    event_type: SmartleadEventType
    raw_event_type: str
    smartlead_campaign_id: str
    normalized_email: str
    occurred_at: dt.datetime
    sequence_number: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _parse_time(raw: Any) -> dt.datetime | None:
    if not raw:
        return None
    try:
        moment = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    # Smartlead returns naive timestamps on some fields. They are UTC; assuming
    # so is better than dropping the event, and better than letting a naive
    # value reach a timezone-aware column and raise at write time.
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def fingerprint(stats_id: str, event_type: SmartleadEventType) -> str:
    """A stable id for one event, from the row's identity and the event's kind.

    Smartlead's callbacks carry no event id, which is why the table dedupes on
    a fingerprint rather than on a provider id. Deriving it the same way here
    means a polled event and a pushed one for the same fact collapse onto a
    single row if a webhook endpoint ever does exist.
    """
    return hashlib.sha256(f"{stats_id}:{event_type.value}".encode()).hexdigest()


def events_from_row(row: dict[str, Any], *, campaign_id: str) -> list[DeliveryEvent]:
    """Every delivery event a single statistics row is evidence of.

    Returns nothing for a row with no ``stats_id`` or no address: without the
    first there is no stable identity to dedupe on, and without the second the
    event cannot be attributed to anyone. Recording either would be recording
    that something happened to somebody.
    """
    stats_id = str(row.get("stats_id") or "").strip()
    email = str(row.get("lead_email") or "").strip().lower()
    if not stats_id or not email:
        return []

    sent_at = _parse_time(row.get("sent_time"))
    raw = {key: row.get(key) for key in _KEPT_FIELDS if key in row}
    sequence_number = row.get("sequence_number")
    if sequence_number is not None:
        try:
            sequence_number = int(sequence_number)
        except (TypeError, ValueError):
            sequence_number = None

    def build(
        event_type: SmartleadEventType, raw_name: str, occurred_at: dt.datetime | None
    ) -> DeliveryEvent | None:
        if occurred_at is None:
            return None
        return DeliveryEvent(
            fingerprint=fingerprint(stats_id, event_type),
            event_type=event_type,
            raw_event_type=raw_name,
            smartlead_campaign_id=campaign_id,
            normalized_email=email,
            occurred_at=occurred_at,
            sequence_number=sequence_number,
            raw=raw,
        )

    candidates = [
        build(SmartleadEventType.SENT, "sent_time", sent_at),
        build(SmartleadEventType.OPENED, "open_time", _parse_time(row.get("open_time"))),
        build(
            SmartleadEventType.CLICKED, "click_time", _parse_time(row.get("click_time"))
        ),
        build(
            SmartleadEventType.REPLIED, "reply_time", _parse_time(row.get("reply_time"))
        ),
    ]

    # Bounces and unsubscribes arrive as booleans with no time of their own, so
    # they are dated to the send that caused them. That is the wrong instant by
    # up to a few days, and it is the only honest one available -- inventing
    # "now" would make an event that happened last week look like it happened
    # during this poll, which is exactly the distortion an event log exists to
    # prevent.
    if row.get("is_bounced"):
        candidates.append(build(SmartleadEventType.BOUNCED, "is_bounced", sent_at))
    if row.get("is_unsubscribed"):
        candidates.append(
            build(SmartleadEventType.UNSUBSCRIBED, "is_unsubscribed", sent_at)
        )

    return [event for event in candidates if event is not None]


__all__ = ["DeliveryEvent", "events_from_row", "fingerprint"]
