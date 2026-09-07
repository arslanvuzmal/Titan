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


# ==========================================================================
# "Permanent" and "the address is bad" are not the same claim
# ==========================================================================
#
# Every body below is quoted from a bounce this system actually received.
#
# The rule that used to run here was "any 5.x.x code means the address is
# dead". It is one character away from right and it cost a mailbox. A 5.7.x is
# the security-and-policy class: the message was refused. Reading that as a
# dead address suppressed a working first-party address permanently, and
# counted a spam filter's opinion of our copy as a hard bounce -- which pushed
# the sending mailbox over the 2% pause threshold and stopped it.
def bounce(body: str) -> InboundMessage:
    return message(
        from_email="mailer-daemon@rs5-lon.serverhostgroup.com",
        subject="Mail delivery failed: returning message to sender",
        body_text=body,
        content_type="multipart/report; report-type=delivery-status",
    )


def test_a_filter_refusing_the_message_is_not_a_dead_address() -> None:
    """1 September, info@thefrederickdentalclinic.com. Their server accepted
    it and was forwarding to a Gmail account when the forwarder's filter
    refused it. Nothing was wrong with the address."""
    result = classify_reply(
        bounce(
            "550 5.7.1 [CS] Message blocked. If this is a false positive, "
            "please report this to your hosting service provider."
        )
    )

    assert result.kind is ReplyKind.BOUNCE
    assert not is_hard_bounce(result), "a working address would be suppressed"


def test_a_policy_refusal_says_that_is_what_it_is() -> None:
    """It is still a deliverability alarm -- just about the message rather than
    the list, which is a different problem with a different fix."""
    result = classify_reply(bounce("550 5.7.1 [CS] Message blocked."))

    assert "policy_rejection" in result.signals


def test_a_policy_code_carrying_an_address_verdict_is_still_permanent() -> None:
    """27 August, reception@cavershamheightsdentalpractice.co.uk. Servers pick
    the code loosely; when one says in words that the mailbox is not there, it
    has answered the question whatever number it put in front of it."""
    result = classify_reply(bounce("554 5.7.1 sorry, no mailbox here by that name"))

    assert is_hard_bounce(result)


def test_a_genuinely_missing_address_is_still_permanent() -> None:
    """27 August, info@folddentistry.co.uk. The case the rule is for."""
    result = classify_reply(
        bounce(
            "550 5.1.1 <info@folddentistry.co.uk>: Email address could not be "
            "found, or was misspelled (G8)"
        )
    )

    assert is_hard_bounce(result)


def test_exchange_rejecting_the_recipient_is_still_permanent() -> None:
    """27 August, katie@reading-smiles.co.uk. Microsoft answers a great many
    things with 5.4.1, but it names the recipient as the problem."""
    result = classify_reply(
        bounce("550 5.4.1 Recipient address rejected: Access denied.")
    )

    assert is_hard_bounce(result)


def test_a_full_mailbox_is_still_temporary() -> None:
    """The direction this rule must never drift: a 4.x.x is a real mailbox."""
    result = classify_reply(
        bounce("452 4.2.2 The email account that you tried to reach is over quota.")
    )

    assert not is_hard_bounce(result)
