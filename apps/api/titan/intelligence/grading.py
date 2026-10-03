"""A-D grades, and which channel a graded lead gets first.

**Not a second score.** ``titan.intelligence.scoring`` already weighs eleven
named dimensions to a 0-100 total, deterministically and with a reason for
every point. A grade is that same total read as a letter, and the four parts
the operator reasons in -- need, ability to pay, fit, reachability -- are the
existing dimensions grouped, not new ones measured. Two scores for one lead is
how a dashboard ends up arguing with itself.

**The channel policy is a table, not a model.** It can be read, argued with
and tested row by row, and every decision carries its reason. Calling is in
the table and switched off: it costs money per minute, and nothing here
assumes a budget that has not been agreed. With calls off, every lead that
would have been called is written to instead, and the reason says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Lower bound of each letter, on the existing 0-100 total.
GRADE_FLOORS: tuple[tuple[str, int], ...] = (("A", 80), ("B", 65), ("C", 50))

#: Which scoring dimensions make up each part. Every dimension in
#: ``scoring.WEIGHTS`` belongs to exactly one part; a test holds that.
PARTS: dict[str, tuple[str, ...]] = {
    "need": (
        "finding_severity",
        "finding_confidence",
        "opportunity_breadth",
        "modernisation_gap",
    ),
    "ability_to_pay": ("commercial_impact", "business_activity"),
    "fit": ("industry_fit", "geographic_fit", "service_fit"),
    "reachability": ("contact_quality", "decision_maker"),
}


def letter_for(total: int | None) -> str | None:
    """The letter for a total, or None for a lead nobody has scored."""
    if total is None:
        return None
    for letter, floor in GRADE_FLOORS:
        if total >= floor:
            return letter
    return "D"


@dataclass(frozen=True, slots=True)
class Part:
    key: str
    points: float
    out_of: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Grade:
    letter: str | None
    total: int | None
    parts: tuple[Part, ...]

    def part(self, key: str) -> Part | None:
        return next((p for p in self.parts if p.key == key), None)


def grade_from_components(total: int | None, components: dict[str, Any] | None) -> Grade:
    """A grade from a stored ``LeadScore`` -- its total and its components JSON.

    Components are read as stored (``{key: {weight, weighted, reason}}``) so a
    score written under an older policy is grouped by what it actually said,
    not recomputed under today's weights.
    """
    stored = components or {}
    parts: list[Part] = []
    for key, dimensions in PARTS.items():
        points = out_of = 0.0
        reasons: list[str] = []
        for dimension in dimensions:
            entry = stored.get(dimension)
            if not isinstance(entry, dict):
                continue
            points += float(entry.get("weighted") or 0.0)
            out_of += float(entry.get("weight") or 0.0)
            if entry.get("reason"):
                reasons.append(str(entry["reason"]))
        parts.append(Part(key, round(points, 1), round(out_of, 1), tuple(reasons)))
    return Grade(letter=letter_for(total), total=total, parts=tuple(parts))


# --------------------------------------------------------------- the policy
EMAIL = "email"
CALL = "call"
HOLD = "hold"


@dataclass(frozen=True, slots=True)
class ChannelDecision:
    first: str
    then: str
    reason: str


def choose_channel(
    letter: str | None,
    *,
    has_phone: bool,
    has_email: bool,
    calls_enabled: bool,
    call_budget_left: int,
) -> ChannelDecision:
    """The first action for a graded lead, and what follows it."""
    if letter is None:
        return ChannelDecision(HOLD, "score it", "not scored yet")
    if letter == "D":
        return ChannelDecision(HOLD, "re-check in 90 days", "grade D: not contacted")

    can_call = letter == "A" and has_phone and calls_enabled and call_budget_left > 0
    if can_call:
        return ChannelDecision(
            CALL,
            "email with the evidence page the next working day",
            "grade A with a phone number and call budget left today",
        )
    if not has_email:
        if letter == "A" and has_phone and not calls_enabled:
            return ChannelDecision(
                HOLD,
                "call once calling is switched on",
                "grade A, phone only; calls are off",
            )
        return ChannelDecision(HOLD, "find an address", "no email address to write to")

    if letter == "A":
        why = "grade A"
        if has_phone and not calls_enabled:
            why += "; would be called, but calls are off"
        elif has_phone and call_budget_left <= 0:
            why += "; would be called, but today's call budget is spent"
        return ChannelDecision(EMAIL, "priority sequence", why)
    if letter == "B":
        return ChannelDecision(
            EMAIL, "sequence; promoted to a call after an evidence-page visit", "grade B"
        )
    return ChannelDecision(
        EMAIL, "sequence at a smaller daily share, no calls", "grade C"
    )


__all__ = [
    "CALL",
    "EMAIL",
    "GRADE_FLOORS",
    "HOLD",
    "PARTS",
    "ChannelDecision",
    "Grade",
    "Part",
    "choose_channel",
    "grade_from_components",
    "letter_for",
]
