"""Lead source grading, and the query that feeds it.

The grades are advisory and the sample floors are low, because a discovery batch
returning twenty businesses is normal. What the tests defend is that the two
questions stay separate: a source can be perfectly safe and useless, and it must
not be graded as though those were the same failure.
"""

from __future__ import annotations

import pytest
from titan.intelligence.lead_sources import (
    BOUNCE_RATE_POOR,
    BOUNCE_RATE_WATCH,
    CONTACTABILITY_POOR,
    MIN_LEADS_TO_GRADE,
    MIN_SENDS_TO_GRADE_SAFETY,
    LeadSourceWindow,
    SourceGrade,
    classify,
    explain,
    rank,
    roll_up,
)


def window(**overrides) -> LeadSourceWindow:
    """A batch that behaved well: reachable, delivered, and answered."""
    base: dict = {
        "source_id": "src-1",
        "kind": "google_places",
        "label": "dentists in Manchester",
        "cost_usd": 4.0,
        "leads": 20,
        "contactable": 18,
        "contacted": 15,
        "sent": 30,
        "delivered": 29,
        "bounced": 0,
        "complained": 0,
        "replied": 3,
    }
    base.update(overrides)
    return LeadSourceWindow(**base)


# ==========================================================================
# The control
# ==========================================================================
def test_a_good_batch_grades_strong() -> None:
    assert classify(window()) is SourceGrade.STRONG


def test_safe_but_silent_is_steady_not_poor() -> None:
    """The distinction the module exists for. A quiet week wastes research
    budget; a bouncing batch spends sending reputation. Grading them the same
    would tell an operator to stop doing the harmless one."""
    quiet = window(replied=0)

    assert classify(quiet) is SourceGrade.STEADY
    assert classify(quiet) is not SourceGrade.POOR


# ==========================================================================
# Sample floors
# ==========================================================================
def test_a_small_batch_is_not_graded() -> None:
    small = window(leads=MIN_LEADS_TO_GRADE - 1, contactable=0, sent=0, replied=0)

    assert classify(small) is SourceGrade.UNKNOWN
    assert "too few to judge" in explain(small, classify(small))


def test_safety_is_not_graded_on_too_few_sends() -> None:
    """A batch can hold fifty leads and have had two messages sent from it. One
    bounce out of two is 50% and is two messages."""
    barely = window(sent=MIN_SENDS_TO_GRADE_SAFETY - 1, bounced=2, replied=0)

    assert barely.bounce_rate > BOUNCE_RATE_POOR
    assert barely.has_enough_sends is False
    assert classify(barely) is SourceGrade.STEADY


def test_contactable_but_never_written_to_is_unknown() -> None:
    """Calling this steady would claim the safety half had been measured."""
    fresh = window(contacted=0, sent=0, delivered=0, replied=0)

    assert classify(fresh) is SourceGrade.UNKNOWN
    assert "none written to yet" in explain(fresh, classify(fresh))


# ==========================================================================
# Safety
# ==========================================================================
def test_a_bouncing_batch_is_poor() -> None:
    bad = window(sent=20, bounced=int(20 * BOUNCE_RATE_POOR) + 1, replied=0)

    assert classify(bad) is SourceGrade.POOR


def test_a_mildly_bouncing_batch_is_watched() -> None:
    mild = window(sent=100, bounced=8, replied=0)

    assert BOUNCE_RATE_WATCH <= mild.bounce_rate < BOUNCE_RATE_POOR
    assert classify(mild) is SourceGrade.WATCH


def test_one_complaint_is_poor_whatever_else_happened() -> None:
    """Somebody this search found reported Titan as spam. No rate applies, and
    a reply from someone else does not offset it."""
    complained = window(complained=1, replied=9)

    assert classify(complained) is SourceGrade.POOR
    assert "reported Titan as spam" in explain(complained, classify(complained))


def test_bouncing_outranks_replying() -> None:
    """Safety is checked first and on its own floor, so a batch that bounced
    badly is not rescued by having also produced a reply."""
    both = window(sent=20, bounced=10, replied=5)

    assert classify(both) is SourceGrade.POOR


# ==========================================================================
# Contactability
# ==========================================================================
def test_a_search_returning_unreachable_businesses_is_poor() -> None:
    """The fastest signal available, and the one that costs nothing to be wrong
    about -- no mail was sent either way."""
    unreachable = window(leads=40, contactable=8, contacted=6, sent=8, replied=0)

    assert unreachable.contactability < CONTACTABILITY_POOR
    assert classify(unreachable) is SourceGrade.POOR
    assert "no address to write to" in explain(unreachable, classify(unreachable))


def test_middling_contactability_is_watched() -> None:
    middling = window(leads=40, contactable=20, contacted=18, replied=0)

    assert classify(middling) is SourceGrade.WATCH


# ==========================================================================
# Rates and cost
# ==========================================================================
def test_reply_rate_is_per_lead_not_per_message() -> None:
    """Dividing by messages would make a source look worse the more follow-ups
    it received, which is a property of the sequence, not of the search."""
    one_touch = window(contacted=10, sent=10, replied=2)
    four_touches = window(contacted=10, sent=40, replied=2)

    assert one_touch.reply_rate == four_touches.reply_rate == 0.2


def test_cost_per_contactable_is_none_when_nothing_was_reachable() -> None:
    """Not zero. A batch that produced nothing usable has an undefined cost per
    lead, and 0.00 would read as free."""
    barren = window(contactable=0, contacted=0, sent=0, replied=0)

    assert barren.cost_per_contactable is None
    assert barren.cost_per_reply is None


def test_cost_per_reply_divides_by_replies() -> None:
    assert window(cost_usd=9.0, replied=3).cost_per_reply == 3.0


# ==========================================================================
# Ranking and roll-up
# ==========================================================================
def test_ranking_puts_the_worst_first() -> None:
    """The list is read to find what to stop doing. Opening with the best
    performer would bury the batch costing reputation at the bottom."""
    good = window(source_id="a", label="a")
    bad = window(source_id="b", label="b", sent=20, bounced=10, replied=0)
    quiet = window(source_id="c", label="c", replied=0)

    ordered = [grade for _, grade in rank([good, quiet, bad])]

    assert ordered[0] is SourceGrade.POOR
    assert ordered[-1] is SourceGrade.STRONG


def test_ranking_is_stable_for_equal_grades() -> None:
    first = window(source_id="a", label="alpha", replied=0)
    second = window(source_id="b", label="beta", replied=0)

    assert [w.label for w, _ in rank([second, first])] == ["alpha", "beta"]


def test_roll_up_sums_one_kind_and_ignores_the_rest() -> None:
    """An individual search rarely clears the sample floor. The kind is where
    enough accumulates to say anything."""
    places_a = window(source_id="a", kind="google_places", leads=8, contactable=7)
    places_b = window(source_id="b", kind="google_places", leads=8, contactable=6)
    csv = window(source_id="c", kind="csv_import", leads=50, contactable=50)

    combined = roll_up([places_a, places_b, csv], "google_places")

    assert combined.leads == 16
    assert combined.contactable == 13
    assert combined.kind == "google_places"
    assert combined.has_enough_leads is True
    assert places_a.has_enough_leads is False


def test_roll_up_of_an_absent_kind_is_empty_not_wrong() -> None:
    combined = roll_up([window()], "referral")

    assert combined.leads == 0
    assert classify(combined) is SourceGrade.UNKNOWN


# ==========================================================================
# Every grade explains itself
# ==========================================================================
@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"replied": 0},
        {"leads": 2, "contactable": 1, "contacted": 0, "sent": 0, "replied": 0},
        {"contacted": 0, "sent": 0, "delivered": 0, "replied": 0},
        {"sent": 20, "bounced": 10, "replied": 0},
        {"sent": 100, "bounced": 8, "replied": 0},
        {"complained": 2},
        {"leads": 40, "contactable": 8, "contacted": 6, "sent": 8, "replied": 0},
    ],
)
def test_no_grade_produces_an_empty_or_broken_line(overrides: dict) -> None:
    w = window(**overrides)
    line = explain(w, classify(w))

    assert line
    assert "None" not in line
    assert "%%" not in line
