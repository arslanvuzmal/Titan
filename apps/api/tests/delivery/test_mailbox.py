"""Parsing what arrives in the mailbox.

No network and no database: every case here is a byte string that a real mail
server produced or could produce. The parser is the layer where a mistake is
silent -- a Message-ID that fails to match threads nothing, and the reply is
recorded against no lead while the sequence carries on writing to somebody who
already answered.
"""

from __future__ import annotations

import datetime as dt

from titan.delivery.mailbox import parse_email, strip_html
from titan.intelligence.replies import InboundMessage, ReplyKind, classify_reply


def build(raw: str) -> bytes:
    return raw.replace("\n", "\r\n").encode("utf-8")


PLAIN = build(
    """From: Dana Okafor <dana@bellrose-dental.test>
To: outreach@arslanvuzmallone.com
Subject: Re: A broken button on your booking page
Message-ID: <reply-001@bellrose-dental.test>
In-Reply-To: <abc123def456@arslanvuzmallone.com>
References: <older@arslanvuzmallone.com> <abc123def456@arslanvuzmallone.com>
Date: Mon, 10 Aug 2026 09:14:22 +0100
Content-Type: text/plain; charset="utf-8"

Thanks for flagging that, we had no idea. Could you send over pricing?

Dana
"""
)


def test_plain_reply_fields():
    parsed = parse_email(PLAIN)

    assert parsed.from_email == "dana@bellrose-dental.test"
    assert parsed.from_display == "Dana Okafor"
    assert parsed.subject == "Re: A broken button on your booking page"
    assert "Could you send over pricing?" in parsed.body_text
    assert parsed.date == dt.datetime(
        2026, 8, 10, 9, 14, 22, tzinfo=dt.timezone(dt.timedelta(hours=1))
    )


def test_message_ids_are_stripped_of_angle_brackets():
    """The join key against messages.provider_message_id, which is stored bare.

    SmtpProvider records ``digest@domain``; the header carries ``<digest@domain>``.
    Comparing them unnormalised matches nothing, and every reply silently loses
    its lead -- with no error anywhere, because "no rows" is a valid result.
    """
    parsed = parse_email(PLAIN)

    assert parsed.message_id == "reply-001@bellrose-dental.test"
    assert parsed.in_reply_to == "abc123def456@arslanvuzmallone.com"
    assert parsed.references == (
        "older@arslanvuzmallone.com",
        "abc123def456@arslanvuzmallone.com",
    )


def test_thread_ids_put_the_nearest_ancestor_first():
    """In-Reply-To, then References newest-first.

    A long thread lists every ancestor; attributing a reply to the oldest one
    would credit it to the wrong step of the sequence.
    """
    parsed = parse_email(PLAIN)

    assert parsed.thread_ids() == (
        "abc123def456@arslanvuzmallone.com",
        "older@arslanvuzmallone.com",
    )


def test_html_only_autoresponder_is_still_recognised():
    """An HTML-only out-of-office must not read as a human reply.

    If the body were left empty, no auto-reply wording would match, the message
    would classify as HUMAN, and a live sequence would stop permanently for
    somebody who was simply on leave.
    """
    raw = build(
        """From: Ops <ops@fixture-business.test>
Subject: Automatic reply: your message
Message-ID: <ooo-1@fixture-business.test>
Content-Type: text/html; charset="utf-8"

<html><body><p>I am <b>out of the office</b> until 3 September.</p></body></html>
"""
    )

    parsed = parse_email(raw)

    assert "out of the office" in parsed.body_text
    assert "<b>" not in parsed.body_text
    classification = classify_reply(
        InboundMessage(
            from_email=parsed.from_email,
            subject=parsed.subject,
            body_text=parsed.body_text,
            headers=parsed.headers,
            content_type=parsed.content_type,
        )
    )
    assert classification.kind is ReplyKind.AUTO


def test_content_type_keeps_the_report_type_parameter():
    """The DSN marker survives the parser and is recognised by the classifier.

    Two ways this breaks. ``get_content_type()`` drops parameters entirely, so a
    parser returning it makes every DSN look like an ordinary multipart message.
    And Python's email policy re-serialises the value *quoted*
    (``report-type="delivery-status"``) where most MTAs send it bare, so a
    classifier matching only the bare form misses it too. Asserting on the
    signal rather than on the string checks the pair actually agree.
    """
    parsed = parse_email(DSN)

    assert "report-type" in parsed.content_type
    assert "delivery-status" in parsed.content_type

    classification = classify_reply(
        InboundMessage(
            from_email="someone@unrecognisable.test",
            subject="Zustellung fehlgeschlagen",
            body_text=parsed.body_text,
            headers=parsed.headers,
            content_type=parsed.content_type,
        )
    )
    assert "dsn_content_type" in classification.signals
    assert classification.kind is ReplyKind.BOUNCE


DSN = build(
    """From: Mail Delivery Subsystem <MAILER-DAEMON@mx.fixture-business.test>
To: outreach@arslanvuzmallone.com
Subject: Undeliverable: A broken button on your booking page
Message-ID: <dsn-77@mx.fixture-business.test>
Content-Type: multipart/report; report-type=delivery-status; boundary="bnd"

--bnd
Content-Type: text/plain; charset="utf-8"

Your message could not be delivered.
Reason: 5.1.1 user unknown

--bnd
Content-Type: message/delivery-status

Reporting-MTA: dns; mx.fixture-business.test
Final-Recipient: rfc822; sam@fixture-business.test
Action: failed
Status: 5.1.1

--bnd--
"""
)


def test_dsn_reports_the_address_that_actually_failed():
    """The whole point of parsing delivery reports.

    Without Final-Recipient, a bounce suppresses MAILER-DAEMON@ at the receiving
    host: the mailbox that rejected the mail stays in rotation and keeps
    bouncing, costing sender reputation on every retry, while a postmaster
    address nobody writes to is blocked forever.
    """
    parsed = parse_email(DSN)

    assert parsed.from_email == "mailer-daemon@mx.fixture-business.test"
    assert parsed.failed_recipient == "sam@fixture-business.test"


def test_dsn_classifies_as_a_permanent_bounce():
    parsed = parse_email(DSN)

    classification = classify_reply(
        InboundMessage(
            from_email=parsed.from_email,
            subject=parsed.subject,
            body_text=parsed.body_text,
            headers=parsed.headers,
            content_type=parsed.content_type,
        )
    )

    assert classification.kind is ReplyKind.BOUNCE
    assert "permanent_failure_code" in classification.signals


def test_x_failed_recipients_is_the_fallback():
    """Exim and others report the failure on the outer message instead."""
    raw = build(
        """From: postmaster@mx.other.test
Subject: Mail delivery failed: returning message to sender
Message-ID: <exim-1@mx.other.test>
X-Failed-Recipients: nobody@other.test
Content-Type: text/plain

This message was created automatically by mail delivery software.
The following address failed permanently: 5.1.1 no such user
"""
    )

    assert parse_email(raw).failed_recipient == "nobody@other.test"


def test_a_date_without_a_timezone_is_read_as_utc():
    """Never as local time.

    The poller's timezone is an accident of where it happens to be deployed. A
    naive Date parsed as local would store a reply as arriving hours before or
    after it did, and received_at is what the lead's replied_at is set from.
    """
    raw = build(
        """From: a@b.test
Subject: hello
Message-ID: <naive-1@b.test>
Date: Mon, 10 Aug 2026 09:00:00

hi
"""
    )

    parsed = parse_email(raw)

    assert parsed.date == dt.datetime(2026, 8, 10, 9, 0, tzinfo=dt.UTC)


def test_malformed_input_never_raises():
    """A broken message must not stop the ones behind it in the folder.

    The queue behind a poison message includes the unsubscribe requests, which
    are the ones with legal consequences for going unread.
    """
    for raw in (b"", b"\xff\xfe not a message at all", b"Subject: only a header"):
        parsed = parse_email(raw)
        assert parsed.from_email == "" or isinstance(parsed.from_email, str)


def test_missing_message_id_is_none_not_empty_string():
    """None routes to the content-hash fallback; "" would key every such
    message to the same row and silently discard all but the first."""
    raw = build(
        """From: a@b.test
Subject: no id here

body
"""
    )

    assert parse_email(raw).message_id is None


def test_strip_html_drops_script_and_style_content():
    value = "<style>p{color:red}</style><p>Hello</p><script>alert(1)</script>"

    assert strip_html(value) == "Hello"


def test_strip_html_unescapes_entities():
    assert "Tom & Jerry's" in strip_html("<p>Tom &amp; Jerry&#39;s</p>")
