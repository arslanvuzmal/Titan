"""The finding that leads, and the offer that answers it, chosen together.

Two rules have to hold for the *same* finding: it must be worth opening with
(tier 0 or 1), and this industry's playbook must contain an offer answering it.

Testing only the highest-ranked candidate and refusing on a miss threw the lead
away over its best finding. Measured on the live estate the morning this was
written: 23% of every research run ended `no_offer_matching_the_evidence`, with
an answerable second finding sitting unexamined underneath.
"""

from __future__ import annotations

import inspect

from titan.activities import pipeline
from titan.db.enums import Industry
from titan.intelligence.playbooks import select_offers


def test_the_walk_stops_at_the_first_answerable_finding() -> None:
    """Structural: it iterates, rather than indexing the first element."""
    src = inspect.getsource(pipeline.generate_draft)
    walk = src.split("Which finding leads, and which offer answers it")[1][:2600]

    assert "for candidate in pitchable:" in walk
    assert "headline = pitchable[0]" not in src, (
        "the headline must come from the walk, not from the first element"
    )


def test_rank_order_still_decides_which_finding_leads() -> None:
    """The fallback must not become 'whichever one happens to have an offer'.

    The walk goes in rank order and takes the first answerable candidate, so a
    conversion defect still outranks an automation gap whenever both can be
    answered.
    """
    src = inspect.getsource(pipeline.generate_draft)
    sort_at = src.index("pitchable.sort(")
    walk_at = src.index("for candidate in pitchable:")
    assert sort_at < walk_at, "the list must be ranked before it is walked"


def test_a_tier_two_finding_is_never_reached_by_the_walk() -> None:
    """The list is rank-sorted, so the walk breaks rather than continuing."""
    src = inspect.getsource(pipeline.generate_draft)
    walk = src.split("for candidate in pitchable:")[1][:400]
    assert "> _WORTH_OPENING_WITH" in walk
    assert "break" in walk.split("> _WORTH_OPENING_WITH")[1][:200]


def test_the_two_refusals_stay_distinct() -> None:
    """They are acted on differently and must not collapse into one code.

    Nothing worth opening with means wait for the next research pass. Evidence
    nothing can answer means the playbook has a gap. A single code would hide
    which of those is happening -- the exact failure that made this bug take
    four days to find.
    """
    src = inspect.getsource(pipeline.generate_draft)
    assert "no_offer_matching_the_evidence" in src
    assert "no_finding_worth_opening_with" in src
    assert "if considered" in src


def test_the_offer_still_answers_the_finding_that_leads() -> None:
    """The rule the walk must not weaken.

    select_offers is called with the candidate's own issue type and nothing
    else, so the offer can only ever answer the finding the message opens with.
    """
    src = inspect.getsource(pipeline.generate_draft)
    assert "select_offers(org_industry, {candidate.issue_type})" in src
    assert "select_offers(org_industry, evidenced" not in src


def test_the_gap_this_recovers_is_real() -> None:
    """The automation findings with no offer, which used to sink the lead.

    These are genuinely unanswerable by the current playbooks -- the fix is not
    to invent an offer for them, it is to keep looking at the lead's other
    evidence instead of discarding it.
    """
    unanswerable = [
        t
        for t in ("no_conversational_capability", "no_follow_up_automation",
                  "no_review_automation")
        if not select_offers(Industry.DENTIST, {t})
    ]
    assert unanswerable, "expected at least one tier-1 finding with no offer"

    # ...while the ones that carry the campaign are answerable, so a lead
    # holding both now leads with the second rather than being thrown away.
    assert select_offers(Industry.DENTIST, {"broken_internal_link"})
    assert select_offers(Industry.DENTIST, {"high_friction_contact_form"})
