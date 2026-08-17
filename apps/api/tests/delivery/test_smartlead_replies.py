"""Reading replies out of Smartlead, so outcomes can be observed at all.

``inbound_messages`` and ``reply_classifications`` both hold zero rows, because
the only intake needs IMAP credentials that are not configured. Every outcome
number in the system is therefore a zero being read as evidence rather than as
absence.

The payloads below are the real ones from the live account.
"""

from __future__ import annotations

from titan.delivery.smartlead_replies import (
    SmartleadReply,
    leads_with_replies,
    replies_from_history,
    to_text,
)

# The one reply in the account, verbatim.
OUT_OF_OFFICE = (
    "<html><head> <meta http-equiv='Content-Type' content='text/html'> </head>"
    "<body> ﻿ <p>Thank you for your email.</p><p>I am currently on annual "
    "leave until Wed 19th August 2026</p><p>If you require an urgent response "
    "please contact the office on 0161 834 1515.</p><p>Kind Regards</p>"
    "<p>Stacey</p></body></html>"
)


# ------------------------------------------------------------------- to_text


def test_the_real_reply_becomes_readable_text() -> None:
    """The whole reason for fetching the body rather than just the timestamp."""
    text = to_text(OUT_OF_OFFICE)

    assert "annual leave until Wed 19th August 2026" in text
    assert "<" not in text and ">" not in text


def test_the_inline_byte_order_mark_is_removed() -> None:
    """Smartlead's payloads carry it mid-body rather than at the front, so
    decoding does not deal with it and it lands in the classifier's input."""
    assert "﻿" not in to_text(OUT_OF_OFFICE)


def test_line_structure_survives() -> None:
    """The classifier reads an out-of-office partly by its shape.

    Flattened to one line, "annual leave until" runs into the office phone
    number, which is how a quoted original ends up looking like the reply.
    """
    assert "\n" in to_text(OUT_OF_OFFICE)


def test_style_and_script_blocks_are_dropped_with_their_contents() -> None:
    """CSS contains words like "important" and "block" that read as intent."""
    text = to_text("<style>.a{display:block !important}</style><p>Sounds good</p>")

    assert "important" not in text
    assert "Sounds good" in text


def test_an_empty_body_stays_empty() -> None:
    assert to_text(None) == ""
    assert to_text("<html><body>  </body></html>") == ""


# ------------------------------------------------------------ history parsing


def _history() -> list[dict[str, object]]:
    return [
        {
            "type": "SENT",
            "email_body": "<p>Hello Olliers</p>",
            "time": "2026-08-06T16:02:37.000Z",
        },
        {
            "type": "SENT",
            "email_body": "<p>Hello Olliers</p>",
            "time": "2026-08-17T08:22:19.000Z",
        },
        {
            "type": "REPLY",
            "email_body": OUT_OF_OFFICE,
            "time": "2026-08-17T08:24:39.000Z",
            "from": "staceymabrouk@olliers.com",
            "message_id": "<abc@olliers.com>",
            "stats_id": "3b2c1dc8-b34d-4aa1-9ab3-1aa5de818777",
        },
    ]


def test_only_inbound_messages_are_returned() -> None:
    """Skipped by Smartlead's own label, not by guessing from addresses --
    which breaks the moment a campaign targets an agency that is also in the
    sender pool."""
    found = replies_from_history(_history())

    assert len(found) == 1
    assert isinstance(found[0], SmartleadReply)


def test_the_reply_carries_everything_ingest_needs() -> None:
    reply = replies_from_history(_history())[0]

    assert reply.message.from_email == "staceymabrouk@olliers.com"
    assert "annual leave" in reply.message.body_text
    assert reply.provider_inbound_id == "<abc@olliers.com>"
    assert reply.received_at is not None
    assert reply.received_at.tzinfo is not None


def test_the_provider_id_is_what_makes_a_re_read_safe() -> None:
    """The poller re-reads whole threads on a schedule. Without a stable id per
    message, every pass would ingest the same reply again -- and each ingest
    halts a sequence, suppresses an address or opens a meeting."""
    first = replies_from_history(_history())[0]
    second = replies_from_history(_history())[0]

    assert first.provider_inbound_id == second.provider_inbound_id


def test_a_reply_with_no_body_is_dropped() -> None:
    """An empty body classifies as UNKNOWN and would halt a sequence on the
    strength of nothing -- worse than not seeing the message, because it looks
    like a considered decision."""
    history = [{"type": "REPLY", "email_body": "", "from": "a@b.c"}]

    assert replies_from_history(history) == []


def test_a_reply_with_no_sender_is_dropped() -> None:
    """Suppression and sequence-stopping are both keyed on the address."""
    history = [{"type": "REPLY", "email_body": "<p>hi</p>", "from": ""}]

    assert replies_from_history(history) == []


def test_a_missing_sender_can_be_supplied_by_the_caller() -> None:
    """The statistics row names the lead even when the thread entry does not."""
    history = [{"type": "REPLY", "email_body": "<p>hi</p>", "message_id": "m1"}]

    found = replies_from_history(history, fallback_from="lead@example.com")

    assert found[0].message.from_email == "lead@example.com"


def test_rubbish_entries_do_not_crash_the_pass() -> None:
    """The shape is somebody else's payload."""
    assert replies_from_history([None, "x", {}, {"type": "REPLY"}]) == []  # type: ignore[list-item]
    assert replies_from_history(None) == []


# ------------------------------------------------------- who to ask about


def test_only_the_leads_that_answered_are_followed_up() -> None:
    """One statistics request per campaign plus one per replying lead, rather
    than one per lead. On this account that is 1 request, not 76."""
    rows = [
        {
            "lead_email": "a@x.com",
            "reply_time": "2026-08-17T08:24:39.000Z",
            "stats_id": "s1",
        },
        {"lead_email": "b@x.com", "reply_time": None},
        {"lead_email": "c@x.com"},
    ]

    assert leads_with_replies(rows) == {"a@x.com": "s1"}


def test_addresses_are_normalised() -> None:
    """They are matched against Titan's own normalised column."""
    rows = [
        {"lead_email": "  Stacey@Olliers.COM ", "reply_time": "2026-08-17T08:24:39.000Z"}
    ]

    assert list(leads_with_replies(rows)) == ["stacey@olliers.com"]


def test_no_replies_means_no_requests() -> None:
    assert leads_with_replies([]) == {}
    assert leads_with_replies(None) == {}
