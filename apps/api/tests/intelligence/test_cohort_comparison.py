"""Telling a business where it stands against the ones we looked at.

The persuasive half of the absence design, and the one with a shape the rest
of Titan does not have: a statement about businesses *other than* the
recipient, in a cold commercial email, made for commercial gain.

That is defensible when it is a claim about our own measurement and indefensible
when it is a claim about the market. We crawled ten clinics in Manchester; we
did not survey Manchester. These tests are about keeping the sentence on the
right side of that line, and about the floors below which no sentence is made
at all.
"""

from __future__ import annotations

import datetime as dt

from titan.intelligence.cohort import (
    MIN_COHORT,
    MIN_SHARE,
    CohortReading,
    comparison_for,
)
from titan.intelligence.modernisation import Capability

NOW = dt.datetime(2026, 9, 8, 10, 0, tzinfo=dt.UTC)


def reading(**overrides) -> CohortReading:
    """Ten clinics looked at in Manchester, six of them booking online."""
    base = {
        "capability": Capability.SELF_SERVICE_BOOKING,
        "industry_noun": "private clinics",
        "market": "Manchester",
        "measured": 10,
        "present": 6,
        "oldest_crawl": NOW - dt.timedelta(days=3),
    }
    base.update(overrides)
    return CohortReading(**base)


# ==========================================================================
# The sentence, and what it is a claim about
# ==========================================================================
def test_the_sentence_is_about_what_we_looked_at() -> None:
    """Planted violation: claim the market rather than the sample.

    "Six of ten private clinics in Manchester" asserts a survey of Manchester
    that nobody ran. "Six of the ten I looked at" asserts a crawl that
    happened, and it is the version we could defend to the one recipient who
    asks how we know.
    """
    said = comparison_for(reading(), now=NOW).sentence

    assert "i looked at" in said.lower()
    assert "6" in said or "six" in said.lower()


def test_it_never_names_a_business() -> None:
    """Planted violation: name the competitor for punch.

    An aggregate is a claim about our own reading. Naming a business is a
    factual claim about a third party, published to a stranger, for
    commercial gain -- and if the crawl was stale we have misdescribed
    somebody who never agreed to be in the email.
    """
    named = reading(examples=("Northgate Clinic", "The Manchester Practice"))

    said = comparison_for(named, now=NOW).sentence

    assert "northgate" not in said.lower()
    assert "manchester practice" not in said.lower()


# ==========================================================================
# The floors -- when no sentence is made at all
# ==========================================================================
def test_a_cohort_below_the_floor_says_nothing() -> None:
    """Planted violation: drop the cohort floor.

    "Six of the seven I looked at" is noise wearing the clothes of a finding,
    and it invites the one reply that ends the conversation: which seven?

    A clear majority on purpose, so the share floor cannot be what refuses it.
    The first version of this test used three of seven and passed because of
    MIN_SHARE, proving nothing about the guard it was named for.
    """
    below = reading(measured=MIN_COHORT - 1, present=MIN_COHORT - 2)

    assert below.present / below.measured > MIN_SHARE, "the share floor must not fire"
    assert comparison_for(below, now=NOW) is None


def test_a_stale_cohort_says_nothing() -> None:
    """Planted violation: ignore how old the crawls are.

    A comparison built from crawls made two months ago describes a market
    that has moved, and the recipient is the one person able to check.
    """
    stale = reading(oldest_crawl=NOW - dt.timedelta(days=90))

    assert comparison_for(stale, now=NOW) is None


def test_nothing_is_said_when_nobody_in_the_cohort_has_it() -> None:
    """"None of the ten I looked at take bookings online" tells the recipient
    they are perfectly normal, which is the opposite of the intended effect.

    Held by the share floor rather than by a guard of its own -- a separate
    check was written, found unreachable, and removed. The behaviour is worth
    a test even where the mechanism is shared.
    """
    assert comparison_for(reading(present=0), now=NOW) is None


def test_nothing_is_said_when_it_would_not_move_anybody() -> None:
    """A minority is not a trend. One of ten having something is not a
    reason to worry about not having it, and saying so spends the strongest
    sentence in the message on the weakest fact."""
    assert comparison_for(reading(present=1), now=NOW) is None


# ==========================================================================
# What it must not turn into
# ==========================================================================
def test_it_states_a_count_and_never_a_consequence() -> None:
    """Planted violation: let the comparison editorialise.

    The count is measured. "You are losing patients to them" is not, and it
    is exactly the fabricated claim the validator refuses everywhere else.
    The fear comes from the true number, not from an adjective attached to it.
    """
    said = comparison_for(reading(), now=NOW).sentence.lower()

    for invented in ("losing", "behind", "falling", "risk", "competitors are"):
        assert invented not in said, f"the comparison editorialised: {invented!r}"


def test_the_evidence_carries_the_cohort_it_was_counted_from() -> None:
    """The claim map has to be able to trace this sentence to something, the
    way every other sentence in the message can."""
    comparison = comparison_for(reading(), now=NOW)

    assert comparison.measured == 10
    assert comparison.present == 6
    assert comparison.capability is Capability.SELF_SERVICE_BOOKING
