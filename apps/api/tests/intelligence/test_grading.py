"""A-D grades read from the existing score, and the channel table."""

from __future__ import annotations

import pytest
from coldops.intelligence import grading as g
from coldops.intelligence.scoring import WEIGHTS


def test_every_scoring_dimension_belongs_to_exactly_one_part() -> None:
    grouped = [d for dims in g.PARTS.values() for d in dims]
    assert sorted(grouped) == sorted(WEIGHTS), "a dimension is missing or doubled"


def test_the_parts_add_up_to_the_whole_score() -> None:
    total_weight = sum(WEIGHTS.values())
    parts_weight = sum(WEIGHTS[d] for dims in g.PARTS.values() for d in dims)
    assert parts_weight == total_weight == 100


@pytest.mark.parametrize(
    ("total", "letter"),
    [
        (100, "A"),
        (80, "A"),
        (79, "B"),
        (65, "B"),
        (64, "C"),
        (50, "C"),
        (49, "D"),
        (0, "D"),
    ],
)
def test_letter_boundaries(total, letter) -> None:
    assert g.letter_for(total) == letter


def test_an_unscored_lead_has_no_letter() -> None:
    assert g.letter_for(None) is None


def test_parts_are_grouped_from_stored_components() -> None:
    components = {
        "finding_severity": {
            "weight": 18,
            "weighted": 18,
            "reason": "critical booking defect",
        },
        "contact_quality": {"weight": 15, "weighted": 7.5, "reason": "role address"},
        "industry_fit": {"weight": 8, "weighted": 8, "reason": "dentist"},
    }
    grade = g.grade_from_components(84, components)
    assert grade.letter == "A"
    need = grade.part("need")
    assert need is not None and need.points == 18 and need.out_of == 18
    reach = grade.part("reachability")
    assert reach is not None and reach.points == 7.5
    assert reach.reasons == ("role address",)


def test_missing_components_read_as_nothing_not_an_error() -> None:
    grade = g.grade_from_components(70, None)
    assert grade.letter == "B"
    assert all(p.points == 0 for p in grade.parts)


# ------------------------------------------------------------------ policy
def choose(letter, **kw):
    base = {
        "has_phone": True,
        "has_email": True,
        "calls_enabled": False,
        "call_budget_left": 0,
    }
    base.update(kw)
    return g.choose_channel(letter, **base)


def test_with_calls_off_an_a_lead_is_written_to_and_told_why() -> None:
    decision = choose("A")
    assert decision.first == g.EMAIL
    assert "calls are off" in decision.reason


def test_an_a_lead_is_called_only_with_calls_on_and_budget_left() -> None:
    assert choose("A", calls_enabled=True, call_budget_left=3).first == g.CALL
    assert choose("A", calls_enabled=True, call_budget_left=0).first == g.EMAIL


def test_only_a_leads_are_called_first() -> None:
    for letter in ("B", "C"):
        assert choose(letter, calls_enabled=True, call_budget_left=9).first == g.EMAIL


def test_d_and_unscored_are_held() -> None:
    assert choose("D").first == g.HOLD
    assert choose(None).first == g.HOLD


def test_no_address_and_no_calls_is_held_not_guessed() -> None:
    decision = choose("A", has_email=False)
    assert decision.first == g.HOLD
    assert "calls are off" in decision.reason
    assert choose("B", has_email=False).first == g.HOLD
