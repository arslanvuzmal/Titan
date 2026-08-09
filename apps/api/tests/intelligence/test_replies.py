"""Inbound reply classification tests.

The pair of errors these exist to prevent, in opposite directions:

* an out-of-office read as a reply stops outreach to an interested lead
  permanently;
* a human reply read as an out-of-office keeps Titan mailing somebody who
  already answered.
"""

from __future__ import annotations

import pytest
from titan.intelligence.replies import (
    InboundMessage,
    ReplyKind,
    classify_reply,
    is_hard_bounce,
)


def message(**overrides) -> InboundMessage:
    base: dict = {
        "from_email": "sam@fixture-business.test",
        "subject": "Re: The booking button on your homepage returns a 404",
        "body_text": "Thanks for flagging that. Can you send some times for a call?",
        "headers": {},
        "content_type": "text/plain",
    }
    base.update(overrides)
    return InboundMessage(**base)


# ==========================================================================
# The control case
# ==========================================================================
def test_a_person_writing_back_stops_the_sequence() -> None:
    result = classify_reply(message())

    assert result.kind is ReplyKind.HUMAN
    assert result.stops_the_sequence is True
    assert result.requires_suppression is False


# ==========================================================================
# Auto-replies must NOT stop the sequence
# ==========================================================================
@pytest.mark.parametrize(
    "headers",
    [
        {"Auto-Submitted": "auto-replied"},
        {"Auto-Submitted": "auto-generated"},
        {"X-Autoreply": "yes"},
        {"X-Autorespond": "vacation"},
        {"Precedence": "auto_reply"},
        {"Precedence": "bulk"},
        # Casing varies freely between mail systems.
        {"AUTO-SUBMITTED": "auto-replied"},
        {"auto-submitted": "auto-replied"},
    ],
)
def test_an_automation_header_marks_an_auto_reply(headers) -> None:
    """Headers are set deliberately by mail systems and a human message does
    not carry them, so they are checked before any wording."""
    result = classify_reply(message(headers=headers))

    assert result.kind is ReplyKind.AUTO
    # The lead has not read anything yet; writing again is correct.
    assert result.stops_the_sequence is False


@pytest.mark.parametrize(
    "subject",
    [
        "Out of Office: Re: your email",
        "Automatic reply: booking button",
        "Auto-Reply from the practice",
        "I am away from my desk until Monday",
        "On annual leave until 3rd September",
        "Thank you for contacting us",
        "Ticket #48213 has been created",
    ],
)
def test_an_auto_responder_subject_is_recognised(subject: str) -> None:
    result = classify_reply(message(subject=subject))

    assert result.kind is ReplyKind.AUTO
    assert result.stops_the_sequence is False


def test_a_human_reply_mentioning_a_holiday_is_still_a_human_reply() -> None:
    """Wording alone is weak evidence. A person writing about their holiday
    carries no automation header, and stopping here would be wrong."""
    result = classify_reply(
        message(
            subject="Re: booking button",
            body_text=(
                "Sorry for the slow reply, I was on leave last week. "
                "Could you call me on Thursday?"
            ),
        )
    )

    assert result.kind is ReplyKind.HUMAN
    assert result.stops_the_sequence is True


# ==========================================================================
# Opt-out outranks automation
# ==========================================================================
@pytest.mark.parametrize(
    "body",
    [
        "Please unsubscribe me from this list.",
        "Remove me from your mailing list.",
        "Take me off your list please.",
        "Do not contact me again.",
        "Please stop emailing me.",
        "I want to opt out.",
    ],
)
def test_an_opt_out_request_suppresses(body: str) -> None:
    result = classify_reply(message(body_text=body))

    assert result.kind is ReplyKind.UNSUBSCRIBE
    assert result.stops_the_sequence is True
    assert result.requires_suppression is True


def test_an_opt_out_inside_an_auto_reply_is_still_an_opt_out() -> None:
    """A direct request must be obeyed even when the mail system stamped the
    message as automatic. Obeying the header here would ignore the sentence."""
    result = classify_reply(
        message(
            subject="Automatic reply: your email",
            headers={"Auto-Submitted": "auto-replied"},
            body_text="I am away until Monday. Also please remove me from your list.",
        )
    )

    assert result.kind is ReplyKind.UNSUBSCRIBE
    assert result.requires_suppression is True


# ==========================================================================
# Complaints outrank everything
# ==========================================================================
@pytest.mark.parametrize(
    "body",
    [
        "This is spam, I never signed up.",
        "I am reporting this as spam.",
        "I did not consent to receiving this.",
        "This is a GDPR breach.",
    ],
)
def test_a_complaint_is_the_most_serious_signal(body: str) -> None:
    result = classify_reply(message(body_text=body))

    assert result.kind is ReplyKind.COMPLAINT
    assert result.stops_the_sequence is True
    assert result.requires_suppression is True


def test_a_complaint_outranks_an_automation_header() -> None:
    result = classify_reply(
        message(
            headers={"Auto-Submitted": "auto-replied"},
            body_text="This is spam. I am reporting this as spam.",
        )
    )

    assert result.kind is ReplyKind.COMPLAINT


# ==========================================================================
# Bounces
# ==========================================================================
def test_a_delivery_status_notification_is_a_bounce() -> None:
    result = classify_reply(
        message(
            from_email="MAILER-DAEMON@fixture-business.test",
            subject="Undeliverable: your message",
            content_type="multipart/report; report-type=delivery-status",
            body_text="Your message could not be delivered. 5.1.1 user unknown",
        )
    )

    assert result.kind is ReplyKind.BOUNCE
    assert is_hard_bounce(result) is True


def test_a_soft_bounce_does_not_suppress() -> None:
    """4.x.x is temporary -- a full mailbox accepts mail again next week.
    Suppressing here would discard a working address."""
    result = classify_reply(
        message(
            from_email="postmaster@fixture-business.test",
            subject="Delivery Status Notification (Delay)",
            body_text="4.2.2 The recipient's mailbox is full. Will retry.",
        )
    )

    assert result.kind is ReplyKind.BOUNCE
    assert is_hard_bounce(result) is False


@pytest.mark.parametrize(
    "body",
    [
        "5.1.1 user unknown",
        "No such user here",
        "Recipient address rejected: address does not exist",
        "550 mailbox unavailable",
    ],
)
def test_permanent_failure_language_marks_a_hard_bounce(body: str) -> None:
    result = classify_reply(
        message(
            from_email="mailer-daemon@x.test", subject="Returned mail", body_text=body
        )
    )

    assert is_hard_bounce(result) is True


def test_a_bounce_never_counts_as_a_human_reply() -> None:
    """A bounce is machine-to-machine. Recording it as a reply would stop the
    sequence on the strength of the recipient's server, not the recipient."""
    result = classify_reply(
        message(from_email="Mail Delivery Subsystem", subject="Delivery has failed")
    )

    assert result.kind is ReplyKind.BOUNCE
    assert result.stops_the_sequence is False


# ==========================================================================
# Diagnosability
# ==========================================================================
def test_every_classification_records_which_rule_fired() -> None:
    """So a misclassification can be diagnosed rather than argued about."""
    for msg in (
        message(),
        message(headers={"Auto-Submitted": "auto-replied"}),
        message(body_text="unsubscribe"),
        message(from_email="mailer-daemon@x.test", subject="Undeliverable"),
        message(body_text="this is spam"),
    ):
        assert classify_reply(msg).signals
