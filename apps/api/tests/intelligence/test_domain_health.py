"""Recipient domain health tests.

The classifier's whole difficulty is sample size. Titan sends at most two
messages a day to one recipient domain, so almost every window here holds one to
four messages -- and a rule tuned for the hundreds a sender window sees would
either condemn a domain on its first wrong address or never fire at all.

These pin down where the line sits in both directions.
"""

from __future__ import annotations

import pytest
from titan.db.enums import ContactSource, VerificationStatus
from titan.intelligence.bounce_risk import Verdict, assess
from titan.intelligence.domain_health import (
    BOUNCES_TO_BLOCK,
    MIN_SENDS_FOR_RATE,
    DomainHealth,
    DomainWindow,
    classify,
    explain,
)
from titan.intelligence.mx import MxCheck, MxStatus

MX_OK = MxCheck(MxStatus.PRESENT, "harborline-legal.test", hosts=("mx1.test",))


def window(**kwargs: int) -> DomainWindow:
    return DomainWindow(domain="harborline-legal.test", **kwargs)


def codes(risk) -> set[str]:
    return {s.code for s in risk.signals}


# ==========================================================================
# No history is not a verdict
# ==========================================================================
def test_a_domain_never_written_to_is_unknown() -> None:
    assert classify(window()) is DomainHealth.UNKNOWN


def test_a_send_with_nothing_back_yet_is_unknown() -> None:
    """In flight. No delivery confirmation and no failure is not evidence."""
    assert classify(window(sent=2)) is DomainHealth.UNKNOWN


def test_no_history_produces_no_signal_at_all() -> None:
    risk = assess(
        email="info@harborline-legal.test",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_OK,
        history=window(),
    )

    assert codes(risk) == set()
    assert risk.status is VerificationStatus.PUBLISHED_FIRST_PARTY


# ==========================================================================
# Healthy
# ==========================================================================
def test_delivered_and_nothing_wrong_is_healthy() -> None:
    assert classify(window(sent=3, delivered=3)) is DomainHealth.HEALTHY


def test_a_healthy_domain_does_not_confirm_the_mailbox() -> None:
    """The same trap MX presence sets. Titan having delivered to this domain
    before says the domain accepts mail, not that this mailbox exists."""
    risk = assess(
        email="newperson@harborline-legal.test",
        source=ContactSource.PUBLIC_DIRECTORY,
        mx=MX_OK,
        history=window(sent=8, delivered=8),
    )

    assert "recipient_domain_healthy" in codes(risk)
    assert [s.verdict for s in risk.signals] == [Verdict.NOTE]
    # PUBLIC_DIRECTORY provenance with nothing conclusive stays unsendable.
    assert risk.status is VerificationStatus.UNKNOWN
    assert risk.permits_sending is False


# ==========================================================================
# Watch: visible, no effect
# ==========================================================================
def test_one_bounce_among_deliveries_is_only_watched() -> None:
    """One bad address at a business that otherwise accepts our mail is one bad
    address, not a bad domain."""
    health = classify(window(sent=4, delivered=3, bounced=1))
    assert health is DomainHealth.WATCH


def test_watch_does_not_change_the_verdict() -> None:
    risk = assess(
        email="info@harborline-legal.test",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_OK,
        history=window(sent=4, delivered=3, bounced=1),
    )

    assert "recipient_domain_watch" in codes(risk)
    assert risk.status is VerificationStatus.PUBLISHED_FIRST_PARTY
    assert risk.permits_sending is True


def test_two_bounces_with_a_delivery_is_still_only_watched() -> None:
    """Below both thresholds: not three-with-none-delivered, and not enough
    sends to compute a rate from."""
    assert classify(window(sent=3, delivered=1, bounced=2)) is DomainHealth.WATCH


# ==========================================================================
# Degraded: the rate threshold, and its sample floor
# ==========================================================================
def test_half_the_mail_bouncing_is_degraded() -> None:
    assert classify(window(sent=4, delivered=2, bounced=2)) is DomainHealth.DEGRADED


def test_the_rate_is_not_applied_below_the_sample_floor() -> None:
    """3 sends and 2 bounces is 67%, and it is also three messages. Acting on
    it would condemn a domain on an ordinary pair of scraped guesses."""
    small = window(sent=MIN_SENDS_FOR_RATE - 1, delivered=1, bounced=2)

    assert small.bounce_rate > 0.5
    assert classify(small) is DomainHealth.WATCH


def test_degraded_downgrades_but_does_not_refuse() -> None:
    risk = assess(
        email="info@harborline-legal.test",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_OK,
        history=window(sent=4, delivered=2, bounced=2),
    )

    assert risk.status is VerificationStatus.RISKY
    assert risk.permits_sending is False
    assert risk.refusals == ()
    assert "recipient_domain_degraded" in codes(risk)


# ==========================================================================
# Blocked
# ==========================================================================
def test_three_bounces_and_no_delivery_blocks_the_domain() -> None:
    assert classify(window(sent=3, bounced=BOUNCES_TO_BLOCK)) is DomainHealth.BLOCKED


def test_two_bounces_and_no_delivery_does_not_block() -> None:
    """Two bad addresses at one business is an ordinary scrape producing two
    guesses off the same page."""
    assert classify(window(sent=2, bounced=2)) is DomainHealth.WATCH


def test_bounces_do_not_block_once_something_has_been_delivered() -> None:
    """The domain demonstrably accepts our mail, so the addresses were the
    problem. Degraded on rate, but not conclusive."""
    health = classify(window(sent=6, delivered=1, bounced=5))
    assert health is DomainHealth.DEGRADED


@pytest.mark.parametrize(
    "history",
    [
        {"sent": 1, "delivered": 1, "complained": 1},
        {"sent": 20, "delivered": 19, "complained": 1},
        {"sent": 3, "delivered": 2, "bounced": 1, "complained": 1},
    ],
)
def test_a_single_complaint_blocks_the_domain(history: dict[str, int]) -> None:
    """Categorically different from a bounce, and not subject to a sample floor.

    Somebody at this business marked Titan as spam. Writing to their colleague
    next is how one complaint becomes a pattern.
    """
    assert classify(window(**history)) is DomainHealth.BLOCKED


def test_a_complaint_refuses_a_new_address_at_the_same_business() -> None:
    risk = assess(
        email="adifferentperson@harborline-legal.test",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_OK,
        history=window(sent=20, delivered=19, complained=1),
    )

    assert risk.status is VerificationStatus.INVALID
    assert risk.permits_sending is False
    assert "recipient_domain_blocked" in codes(risk)


# ==========================================================================
# Precedence against the other layers
# ==========================================================================
def test_a_blocked_domain_outranks_a_confirmed_mailbox() -> None:
    """The mailbox existing was never in doubt. Somebody there complained."""
    from titan.intelligence.verifier import VerificationResult

    risk = assess(
        email="info@harborline-legal.test",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_OK,
        history=window(sent=10, delivered=9, complained=1),
        verification=VerificationResult(
            status=VerificationStatus.PROVIDER_VERIFIED, provider="test"
        ),
    )

    assert risk.status is VerificationStatus.INVALID


def test_a_healthy_domain_does_not_rescue_a_lookalike() -> None:
    risk = assess(
        email="info@gmial.com",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_OK,
        history=DomainWindow(domain="gmial.com", sent=9, delivered=9),
    )

    assert risk.status is VerificationStatus.RISKY


# ==========================================================================
# The explanation
# ==========================================================================
@pytest.mark.parametrize(
    "history",
    [
        {},
        {"sent": 3, "delivered": 3},
        {"sent": 4, "delivered": 3, "bounced": 1},
        {"sent": 4, "delivered": 2, "bounced": 2},
        {"sent": 3, "bounced": 3},
        {"sent": 3, "delivered": 2, "complained": 1},
    ],
)
def test_every_verdict_explains_itself(history: dict[str, int]) -> None:
    """The detail is what an operator reads when a lead was refused, so no
    combination may produce an empty or placeholder sentence."""
    w = window(**history)
    sentence = explain(w, classify(w))

    assert sentence
    assert w.domain in sentence
    assert "None" not in sentence


def test_the_complaint_explanation_says_what_actually_happened() -> None:
    w = window(sent=3, delivered=2, complained=1)
    assert "marked Titan as spam" in explain(w, classify(w))
