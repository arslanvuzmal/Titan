"""A blocked mailbox has to be able to earn its way back.

Found on a live mailbox. ``outreach@`` took five hard bounces over 94 sends --
5.32% against a 2% pause threshold -- every one of them from a fortnight
earlier, before address verification existed. The gate blocked it, correctly.

Then nothing happened, and nothing could. The rate is bounces over sends, the
only way down is more clean sends, and a blocked mailbox sends nothing. It
could not earn its way out; it could only wait for the thirty-day window to
roll past the bad days. A gate that can be entered and not left is a gate with
a bug in it, however right the entry condition was.
"""

from __future__ import annotations

import pytest
from titan.delivery.adaptive_limits import (
    PROBATION_QUIET_DAYS,
    PROBATION_VOLUME,
    daily_limit,
)
from titan.delivery.sender_health import SenderHealth

BLOCKED = (SenderHealth.BLOCKED,)


def test_a_blocked_mailbox_that_has_stopped_bouncing_may_send_a_little() -> None:
    """The deadlock, broken. Not a lifted block -- a floor above zero."""
    decision = daily_limit(50, recent=BLOCKED, days_since_bounce=14)

    assert decision.effective == PROBATION_VOLUME


def test_a_mailbox_still_bouncing_keeps_its_zero() -> None:
    """The distinction the whole mechanism rests on. Yesterday's bounce is
    evidence about now; a fortnight-old one is evidence about a fortnight
    ago."""
    decision = daily_limit(50, recent=BLOCKED, days_since_bounce=1)

    assert decision.effective == 0


@pytest.mark.parametrize("days", [0, 1, PROBATION_QUIET_DAYS - 1])
def test_the_quiet_period_has_to_have_elapsed(days: int) -> None:
    assert daily_limit(50, recent=BLOCKED, days_since_bounce=days).effective == 0


@pytest.mark.parametrize("days", [PROBATION_QUIET_DAYS, PROBATION_QUIET_DAYS + 30])
def test_past_the_quiet_period_probation_applies(days: int) -> None:
    assert (
        daily_limit(50, recent=BLOCKED, days_since_bounce=days).effective
        == PROBATION_VOLUME
    )


def test_a_block_with_no_bounce_behind_it_gets_no_probation() -> None:
    """Caught by an existing test, and it was right.

    None means never bounced, or nobody looked. A mailbox blocked without any
    bounce behind it was blocked for some other reason -- failed
    authentication, or a complaint rate -- and neither of those is repaired by
    sending five more. Probation is the remedy for one specific deadlock, not a
    general floor under every block.
    """
    assert daily_limit(50, recent=BLOCKED, days_since_bounce=None).effective == 0


def test_probation_never_exceeds_the_number_a_human_configured() -> None:
    """Same bound as every other autonomous decision here: this may only ever
    be more conservative than the operator's ceiling."""
    assert daily_limit(3, recent=BLOCKED, days_since_bounce=30).effective == 3


def test_a_disabled_mailbox_gets_no_probation_floor() -> None:
    """A mailbox configured to send nothing is not entitled to five."""
    assert daily_limit(0, recent=BLOCKED, days_since_bounce=30).effective == 0


def test_warm_up_does_not_cancel_probation() -> None:
    """A mailbox blocked *and* mid-warm-up still gets its five. Warm-up limits
    growth, and five is not growth -- applying the warm-up cap on top would
    reintroduce the deadlock for exactly the mailboxes least able to escape
    it."""
    decision = daily_limit(50, recent=BLOCKED, warmup_limit=2, days_since_bounce=30)

    assert decision.effective == PROBATION_VOLUME


def test_probation_is_small_enough_to_re_block_quickly() -> None:
    """A mailbox that really is mailing dead addresses should produce its next
    bounce almost immediately. Five a day is the whole point: it is a test, not
    a return to service."""
    assert PROBATION_VOLUME <= 5


# ------------------------------------------------------------------ unchanged
def test_a_healthy_mailbox_is_untouched() -> None:
    decision = daily_limit(50, recent=(SenderHealth.HEALTHY,), days_since_bounce=None)

    assert decision.effective == 50


def test_a_degraded_mailbox_keeps_its_quarter() -> None:
    """Probation is for BLOCKED alone. DEGRADED already has a working path
    back -- a quarter of the ceiling is enough to accumulate evidence."""
    decision = daily_limit(100, recent=(SenderHealth.DEGRADED,), days_since_bounce=30)

    assert decision.effective == 25


def test_warm_up_still_caps_a_healthy_mailbox() -> None:
    decision = daily_limit(
        50, recent=(SenderHealth.WARMING,), warmup_limit=6, days_since_bounce=None
    )

    assert decision.effective == 6


# ==========================================================================
# Saying so
#
# ``LimitDecision.explain`` had no reader until the CRM's "today" section
# needed a sentence next to each mailbox's allowance. The first live mailbox it
# described was ``outreach@``: blocked, on probation, sending five a day -- and
# the sentence read "5 of 50 a day; reduced to 0%: health is blocked". Every
# clause of that is individually true and the whole reads as a bug in the
# dashboard, which is the worst way for a correct system to present itself.
# ==========================================================================
def test_probation_is_named_rather_than_reported_as_a_reduction_to_zero() -> None:
    decision = daily_limit(50, recent=BLOCKED, days_since_bounce=PROBATION_QUIET_DAYS)

    sentence = decision.explain()

    assert decision.probation is True
    assert "probation" in sentence
    assert "0%" not in sentence, (
        "an allowance of five described as a reduction to nothing"
    )
    assert str(PROBATION_VOLUME) in sentence


def test_a_mailbox_that_is_not_on_probation_does_not_claim_to_be() -> None:
    """The flag marks the specific case where probation is the *only* reason
    the number is above zero. A blocked mailbox still bouncing is paused, and a
    healthy one was never blocked."""
    assert daily_limit(50, recent=BLOCKED, days_since_bounce=1).probation is False
    assert daily_limit(50, recent=(SenderHealth.HEALTHY,)).probation is False
    assert (
        daily_limit(
            50, recent=(SenderHealth.DEGRADED,), days_since_bounce=30
        ).probation
        is False
    ), "a quarter of the ceiling is the health factor, not the probation floor"
