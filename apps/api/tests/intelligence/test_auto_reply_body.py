"""Recognising an absence notice when there is no header and no subject to read.

The header and subject rules are the reliable ones and two of the three intakes
supply neither: Smartlead's message history returns a body, no headers, and a
null subject. The only reply this workspace has ever received arrived that way
and was classified as a human reply -- which stops outreach to that lead for
good and counts as an outcome in every rate the system tunes on.

The trap this must not fall into is the opposite error. Per the module's own
preamble, treating a human as a machine is the worse of the two, because Titan
then keeps mailing somebody who answered.
"""

from __future__ import annotations

from titan.intelligence.replies import InboundMessage, ReplyKind, classify_reply

# The live reply, verbatim from Smartlead.
LIVE_OUT_OF_OFFICE = (
    "Thank you for your email.\n"
    "I am currently on annual leave until Wed 19th August 2026\n"
    "If you require an urgent response to your email in my absence please "
    "contact the office on 0161 834 1515 or email info@olliers.com.\n"
    "Kind Regards\n"
    "Stacey"
)


def _reply(body: str, *, subject: str = "") -> InboundMessage:
    """No headers and no subject -- what the Smartlead intake actually supplies."""
    return InboundMessage(
        from_email="stacey@olliers.com", subject=subject, body_text=body
    )


def test_the_live_reply_is_recognised_as_an_absence_notice() -> None:
    """It was classified as a human reply, and the sequence stopped."""
    result = classify_reply(_reply(LIVE_OUT_OF_OFFICE))

    assert result.kind is ReplyKind.AUTO
    assert not result.stops_the_sequence


def test_a_past_tense_mention_is_a_person() -> None:
    """The error that would matter more.

    Somebody apologising for a slow reply is engaging, and reading them as a
    responder means Titan keeps writing to a lead who already answered.
    """
    body = (
        "Hi -- sorry for the slow reply, I was on annual leave last week. "
        "This looks interesting, can you send over some detail?"
    )
    result = classify_reply(_reply(body))

    assert result.kind is ReplyKind.HUMAN
    assert result.stops_the_sequence


def test_a_mention_late_in_a_real_message_is_still_a_person() -> None:
    """An automatic responder leads with its notice. A person raising it in
    passing does so partway through something real."""
    body = (
        "Thanks for getting in touch about the booking page. We rebuilt the "
        "site in March and I thought we had caught everything, so this is "
        "useful. Could you send more detail on what you found and roughly what "
        "it would cost to put right? "
        + "Happy to talk next week. " * 6
        + "I am currently on annual leave but back Monday."
    )
    result = classify_reply(_reply(body))

    assert result.kind is ReplyKind.HUMAN


def test_the_common_phrasings_are_covered() -> None:
    for body in (
        "I am out of the office until 3 September.",
        "I'm currently away and will respond on my return.",
        "This is an automated reply. Your message has been received.",
        "I will be out of office from Monday to Friday.",
        "I am on maternity leave until January.",
        "I will be back on the 14th of September.",
    ):
        assert classify_reply(_reply(body)).kind is ReplyKind.AUTO, body


def test_an_ordinary_reply_is_untouched() -> None:
    for body in (
        "Sounds good, send me a time.",
        "Not interested, thanks.",
        "Who is this? Please remove me.",
        "We already have an agency but I'll keep you in mind.",
    ):
        assert classify_reply(_reply(body)).kind is not ReplyKind.AUTO, body


def test_a_header_still_wins_where_there_is_one() -> None:
    """The body rule is the fallback, not the replacement. IMAP supplies real
    headers and those are the reliable signal."""
    message = InboundMessage(
        from_email="a@b.c",
        subject="",
        body_text="Sounds good, send me a time.",
        headers={"Auto-Submitted": "auto-replied"},
    )

    assert classify_reply(message).kind is ReplyKind.AUTO


def test_an_unsubscribe_still_outranks_the_body_rule() -> None:
    """Order matters: an absence notice that also says "remove me" is a
    request to stop, and must suppress rather than merely continue."""
    body = "I am currently on annual leave. Please unsubscribe me from this list."
    result = classify_reply(_reply(body))

    assert result.kind is ReplyKind.UNSUBSCRIBE
    assert result.requires_suppression


def test_the_signal_is_recorded_so_a_misread_can_be_diagnosed() -> None:
    """Which rule fired, not just what it decided."""
    result = classify_reply(_reply(LIVE_OUT_OF_OFFICE))

    assert "auto_reply_body" in result.signals
