"""Remembering what an address did last time.

The layer a paid verification service is actually selling. MillionVerifier does
not deduce that a mailbox is dead, it remembers watching it bounce; the
algorithm is cheap and the history is the product. Titan generated the same
evidence on every send and discarded it.

Pure tests over ``assess``: the layer is a signal, and what matters is which
verdict it produces and what it must never override.
"""

from __future__ import annotations

import datetime as dt

import pytest
from titan.db.enums import ContactSource, VerificationStatus
from titan.intelligence.address_history import (
    SOFT_BOUNCES_BEFORE_DOUBT,
    AddressHistory,
)
from titan.intelligence.bounce_risk import assess

GOOD = "hello@fixture-business.test"
SOURCE = ContactSource.FIRST_PARTY_WEBSITE
WHEN = dt.datetime(2026, 8, 21, 9, 0, tzinfo=dt.UTC)


def risk(prior: AddressHistory | None, *, email: str = GOOD):
    return assess(email=email, source=SOURCE, prior=prior)


# ------------------------------------------------ the gap this closes
def test_an_address_that_hard_bounced_is_invalid() -> None:
    """Planted violation: discard the bounce, as the estate did.

    Measured live before this existed: ten addresses had hard-bounced and not
    one was marked invalid. Six still carried ``published_first_party`` -- the
    strongest status the system assigns -- held by a mailbox a mail server had
    already said does not exist.
    """
    verdict = risk(AddressHistory(address=GOOD, hard_bounces=1, last_bounce_at=WHEN))

    assert verdict.status is VerificationStatus.INVALID
    assert any(s.code == "mailbox_previously_bounced" for s in verdict.signals)


def test_a_suppression_outlives_the_message_rows() -> None:
    """The case that motivated the whole layer.

    Retention empties message bodies and the lead can be deleted and
    rediscovered by a later crawl. The suppression is permanent, so it is what
    survives to say "we already tried this, and it was dead".
    """
    verdict = risk(AddressHistory(address=GOOD, suppressed_reason="hard_bounce"))

    assert verdict.status is VerificationStatus.INVALID


def test_a_complaint_is_as_conclusive_as_a_bounce() -> None:
    """A person marked us as spam. No sample size makes that ambiguous."""
    verdict = risk(AddressHistory(address=GOOD, suppressed_reason="complaint"))

    assert verdict.status is VerificationStatus.INVALID
    assert any(s.code == "recipient_complained_before" for s in verdict.signals)


# ------------------------------------------------ what it must NOT do
def test_one_soft_bounce_is_not_evidence() -> None:
    """Planted violation: treat every bounce alike.

    A soft bounce is a full mailbox, a greylist, a server having a bad
    afternoon. Refusing on one would discard live customers for a temporary
    condition, and nothing downstream would ever show it happened.
    """
    verdict = risk(AddressHistory(address=GOOD, soft_bounces=1))

    assert verdict.status is not VerificationStatus.INVALID


def test_repeated_soft_bounces_downgrade_rather_than_refuse() -> None:
    """A pattern is worth holding back for, not discarding over."""
    verdict = risk(AddressHistory(address=GOOD, soft_bounces=SOFT_BOUNCES_BEFORE_DOUBT))

    assert verdict.status is VerificationStatus.RISKY
    assert verdict.status is not VerificationStatus.INVALID


def test_no_history_changes_nothing() -> None:
    """Asked, and this address has never misbehaved.

    A zeroed history must land exactly where passing nothing lands, or every
    address Titan has never written to would be judged by this layer.
    """
    asked = risk(AddressHistory(address=GOOD))
    not_asked = risk(None)

    assert asked.status is not_asked.status


def test_absent_means_nobody_looked() -> None:
    """``None`` is not a verdict.

    Every optional layer in this module follows the convention: absent means
    "not checked", never "checked and found wanting". A call site that cannot
    reach the database must not thereby condemn an address.
    """
    assert risk(None).status is not VerificationStatus.INVALID


def test_a_clean_history_cannot_rescue_a_bad_address() -> None:
    """Planted violation: let "we sent to it fine before" outvote a refusal.

    Never having bounced is the weakest possible positive -- it is mostly true
    of addresses nobody has written to. It must not overturn a disposable
    domain, a malformed address, or anything else that refuses.
    """
    verdict = risk(AddressHistory(address="x@mailinator.com"), email="x@mailinator.com")

    assert verdict.status is VerificationStatus.INVALID, (
        "a clean history must not promote a disposable domain"
    )


# ------------------------------------------------ the helpers
def test_proven_dead_reads_both_records() -> None:
    """Either record alone is enough; neither is required."""
    assert AddressHistory(address=GOOD, hard_bounces=1).is_proven_dead
    assert AddressHistory(address=GOOD, suppressed_reason="hard_bounce").is_proven_dead
    assert not AddressHistory(address=GOOD, soft_bounces=9).is_proven_dead
    assert not AddressHistory(address=GOOD).is_proven_dead


@pytest.mark.parametrize("count", [0, 1, SOFT_BOUNCES_BEFORE_DOUBT - 1])
def test_the_doubt_threshold_is_a_threshold(count: int) -> None:
    assert not AddressHistory(address=GOOD, soft_bounces=count).is_doubtful


def test_the_description_says_which_record_it_came_from() -> None:
    """It lands in the claim detail an operator reads. "risky" is not a reason."""
    bounced = AddressHistory(address=GOOD, hard_bounces=2, last_bounce_at=WHEN)

    assert "hard-bounced 2 time(s)" in bounced.describe()
    assert "2026-08-21" in bounced.describe()
    assert (
        "suppressed"
        in AddressHistory(address=GOOD, suppressed_reason="hard_bounce").describe()
    )
