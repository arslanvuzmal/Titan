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


def test_the_walk_is_still_needed_after_the_playbook_gaps_were_filled() -> None:
    """Why this stays, now that the campaign industries are complete.

    The three industries carrying live campaigns can answer every automation
    finding -- ``test_automation_offers.py`` asserts that outright. Thirteen
    other playbooks still cannot, and the next detector added to the crawler
    will land in the same position all four automation detectors did: present
    in the evidence, absent from every playbook.

    The walk is what stops that costing the lead. Filling a gap is the better
    fix when the industry is one being sold into; not discarding the lead over
    it is the fix that holds for the ones nobody has written yet.
    """
    gaps = [
        (industry, issue_type)
        for industry in Industry
        for issue_type in (
            "no_self_service_booking",
            "no_conversational_capability",
            "no_follow_up_automation",
            "no_review_automation",
        )
        if not select_offers(industry, {issue_type})
    ]
    assert gaps, (
        "if every playbook answered every finding the walk would be dead code "
        "-- update this test rather than deleting it when that day comes"
    )

    # The campaign industries are not among them.
    campaign = {Industry.DENTIST, Industry.MED_SPA, Industry.LAW_FIRM}
    assert not (campaign & {industry for industry, _ in gaps})
