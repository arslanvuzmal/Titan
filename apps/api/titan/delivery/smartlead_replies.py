"""Reading replies out of Smartlead, so outcomes can be observed at all.

Titan has a complete inbound path. ``ingest_inbound`` classifies a reply, halts
the sequence when a human answered, suppresses on an unsubscribe or complaint,
opens a meeting on a request to talk, and notifies. Every self-tuning part of
the system -- the A/B test, campaign health, the budget allocator -- reads the
result of that classification.

**It has only ever had one intake, and that intake has never run.** The reply
poller needs IMAP credentials which are not configured, so ``inbound_messages``
holds zero rows, ``reply_classifications`` holds zero rows, and every outcome
number in the system is a zero being read as evidence rather than as absence.

Smartlead has the replies. It is connected to the same mailboxes over its own
IMAP, ``/campaigns/{id}/statistics`` reports ``reply_time`` per send, and
``/campaigns/{id}/leads/{lead}/message-history`` returns the reply itself. So
the loop can be closed without Titan holding a mailbox password.

**Why this matters more than it sounds.** The one reply in the account is an
out-of-office:

    Thank you for your email. I am currently on annual leave until
    Wed 19th August 2026

Under ``count(leads) WHERE replied_at IS NOT NULL`` that is a success, and the
variant that produced it wins the A/B test. Classified, it is
``ReplyClass.OUT_OF_OFFICE``: not a human reply, not a reason to stop sending,
not evidence of anything except that somebody is on leave. The whole point of
collecting the body rather than just the timestamp is being able to tell those
two readings apart.

This module is the translation only -- Smartlead's payload shapes into Titan's
``InboundMessage``. It performs no I/O so the parsing can be tested against real
payloads without a network or a database.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from dataclasses import dataclass
from typing import Any

from titan.intelligence.replies import InboundMessage

#: Smartlead's own word for an inbound message in a thread. Sends are ``SENT``.
REPLY_TYPE = "REPLY"

#: Tags dropped with their content rather than just unwrapped. Keeping the text
#: inside a <style> block would put CSS into the classifier's input, and CSS
#: contains words like "important" and "block" that read as intent.
_DROPPED_BLOCKS = re.compile(
    r"<(script|style|head)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TAGS = re.compile(r"<[^>]+>")
_BREAKS = re.compile(r"<\s*(br|/p|/div|/tr)\s*/?>", re.IGNORECASE)
#: Space, tab, non-breaking space, and the byte-order mark. The last two
#: are written as escapes rather than literals: both are invisible in a
#: diff, so a reviewer cannot tell a stray one from an intended one.
_WHITESPACE = re.compile("[ \t\u00a0\ufeff]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def to_text(email_body: str | None) -> str:
    """Readable text from Smartlead's HTML body.

    Line structure is preserved before tags are stripped, because the classifier
    reads an out-of-office and a signature block partly by their shape. Flatten
    everything to one line and "I am currently on annual leave until" runs into
    the office phone number, which is how a quoted original ends up looking like
    part of the reply.
    """
    if not email_body:
        return ""
    text = _DROPPED_BLOCKS.sub(" ", email_body)
    text = _BREAKS.sub("\n", text)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    # The BOM arrives inline in Smartlead's payloads, mid-body rather than at
    # the front, so it is stripped as whitespace rather than by decoding.
    text = _WHITESPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


@dataclass(frozen=True, slots=True)
class SmartleadReply:
    """One inbound message Smartlead holds, ready for ``ingest_inbound``."""

    message: InboundMessage
    received_at: dt.datetime | None
    #: Smartlead's own id for this message. Used as ``provider_inbound_id``, so
    #: re-reading a thread cannot ingest the same reply twice.
    provider_inbound_id: str
    #: The send this answers, when the payload names it.
    stats_id: str | None


def replies_from_history(
    history: list[dict[str, Any]] | None, *, fallback_from: str | None = None
) -> list[SmartleadReply]:
    """Every inbound message in one lead's thread.

    Sends are skipped by type rather than by direction heuristics: Smartlead
    labels them and guessing from addresses breaks the moment a prospect is
    also in the sender pool, which happens when a campaign targets an agency.

    An entry with no usable body is dropped. An empty ``body_text`` classifies
    as ``UNKNOWN`` and would halt a sequence on the strength of nothing -- worse
    than not seeing the message, because it looks like a considered decision.
    """
    found: list[SmartleadReply] = []
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type") or "").strip().upper() != REPLY_TYPE:
            continue

        body = to_text(entry.get("email_body"))
        if not body:
            continue

        sender = str(entry.get("from") or fallback_from or "").strip()
        if not sender:
            continue

        identifier = str(entry.get("message_id") or entry.get("stats_id") or "").strip()
        if not identifier:
            # No stable id to dedupe on. ``ingest_inbound`` derives one from the
            # content when this is None, which is weaker but still idempotent.
            identifier = ""

        found.append(
            SmartleadReply(
                message=InboundMessage(
                    from_email=sender,
                    subject=str(entry.get("subject") or "").strip(),
                    body_text=body,
                    headers={"Message-ID": identifier} if identifier else {},
                ),
                received_at=_parse_time(entry.get("time")),
                provider_inbound_id=identifier or f"smartlead:{sender}:{body[:64]}",
                stats_id=(str(entry.get("stats_id")) if entry.get("stats_id") else None),
            )
        )
    return found


def leads_with_replies(rows: list[dict[str, Any]] | None) -> dict[str, str]:
    """Which leads in a campaign have replied, by address.

    Read from the statistics rows the delivery poller already fetches, so
    finding the replies costs no extra request. Only the addresses that answered
    are followed up with a message-history call -- one campaign is one
    statistics request plus one request per replying lead, rather than per lead.
    """
    found: dict[str, str] = {}
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("reply_time"):
            continue
        address = str(row.get("lead_email") or "").strip().lower()
        if address:
            found.setdefault(address, str(row.get("stats_id") or ""))
    return found


__all__ = [
    "REPLY_TYPE",
    "SmartleadReply",
    "leads_with_replies",
    "replies_from_history",
    "to_text",
]
