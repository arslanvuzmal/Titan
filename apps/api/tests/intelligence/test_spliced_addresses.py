"""An address that may be two things stuck together.

``Tel: 0161 234 0606info@207dentalcare.com`` tokenises as
``0606info@207dentalcare.com`` -- syntactically perfect, and wrong. No pattern
can split it: the boundary between the number and the mailbox is exactly where
the whitespace is missing, so this is not a parsing problem and cannot be fixed
by a better regex.

It is a claim problem. Provenance said the address was *published first party*,
which is what made it sendable. What a human actually published was a phone
number and a mailbox with nothing between them.

Measured cost: that one address was mailed twice and produced two of the five
bounces behind a 6.2% rate, which halved every mailbox's daily volume.
"""

from __future__ import annotations

from titan.db.enums import ContactSource, verification_permits_sending
from titan.intelligence.bounce_risk import assess
from titan.intelligence.contacts import DIGIT_RUN_PREFIX

WEBSITE = ContactSource.FIRST_PARTY_WEBSITE


def _sendable(email: str) -> bool:
    risk = assess(email=email, source=WEBSITE)
    return verification_permits_sending(risk.status, WEBSITE)


# ------------------------------------------------------------------ the shape


def test_the_live_address_is_recognised() -> None:
    assert DIGIT_RUN_PREFIX.match("0606info")
    assert DIGIT_RUN_PREFIX.match("560606info")


def test_a_two_digit_prefix_is_left_alone() -> None:
    """``07handyman@`` is an ordinary trades mailbox. Three digits, not two:
    the phone-number case has longer runs, and requiring three keeps the common
    legitimate shapes."""
    assert not DIGIT_RUN_PREFIX.match("07handyman")


def test_a_leading_number_that_is_the_business_is_left_alone() -> None:
    assert not DIGIT_RUN_PREFIX.match("3dprint")
    assert not DIGIT_RUN_PREFIX.match("info")


def test_digits_alone_are_not_the_shape() -> None:
    """It is digits *followed by letters* that indicates a splice."""
    assert not DIGIT_RUN_PREFIX.match("0161234")


# ------------------------------------------------------------- what it blocks


def test_the_address_that_bounced_can_no_longer_be_sent() -> None:
    """The whole point. It was stored PUBLISHED_FIRST_PARTY at 0.9 confidence
    and mailed twice."""
    assert not _sendable("0606info@207dentalcare.com")


def test_the_address_it_was_hiding_still_can() -> None:
    """The correct mailbox at the same domain is unaffected -- this holds back
    the ambiguous string, it does not blacklist the business."""
    assert _sendable("info@207dentalcare.com")


def test_a_legitimate_digit_led_address_still_can() -> None:
    """Downgrading every digit-led local part would discard real trades
    addresses, which is a worse trade than the one being made."""
    assert _sendable("07handyman@example.co.uk")


def test_it_downgrades_rather_than_refuses() -> None:
    """This shape does occur legitimately, so the verdict leaves room for a
    person or a verification service to release it."""
    risk = assess(email="0606info@207dentalcare.com", source=WEBSITE)
    codes = {s.code for s in risk.signals}

    assert "digit_run_prefix" in codes
    assert risk.status.value != "invalid", "refusing outright would be too strong"


def test_the_signal_explains_itself() -> None:
    """A held-back lead has to be explainable to whoever asks why."""
    risk = assess(email="0606info@207dentalcare.com", source=WEBSITE)
    detail = next(s.detail for s in risk.signals if s.code == "digit_run_prefix")

    assert "phone number" in detail
