"""Every finding a message may open with must have an offer that answers it.

The crawler grew four automation detectors -- no self-service booking, no way
to ask a question out of hours, nothing that follows up, nobody collecting
reviews -- long after the industry playbooks were written. Nothing in those
playbooks answered them.

So a lead whose only openable evidence was that set, which is precisely the
work this business does, reached drafting, found no offer for its headline and
was refused. Measured on 25 September: 23% of every research run in the estate
ended `no_offer_matching_the_evidence`.
"""

from __future__ import annotations

import pytest
from titan.activities.pipeline import _WORTH_OPENING_WITH, lead_rank
from titan.db.enums import Industry
from titan.intelligence.playbooks import PLAYBOOKS, select_offers

#: The industries carrying live campaigns. These must be complete.
CAMPAIGN_INDUSTRIES = (Industry.DENTIST, Industry.MED_SPA, Industry.LAW_FIRM)

AUTOMATION_FINDINGS = (
    "no_self_service_booking",
    "no_conversational_capability",
    "no_follow_up_automation",
    "no_review_automation",
)


@pytest.mark.parametrize("industry", CAMPAIGN_INDUSTRIES)
@pytest.mark.parametrize("issue_type", AUTOMATION_FINDINGS)
def test_a_campaign_industry_can_answer_every_automation_finding(
    industry: Industry, issue_type: str
) -> None:
    assert select_offers(industry, {issue_type}), (
        f"{industry.value} detects {issue_type} and has nothing to offer for "
        "it, so a lead whose only openable evidence is that finding is "
        "refused and discarded"
    )


@pytest.mark.parametrize("issue_type", AUTOMATION_FINDINGS)
def test_these_findings_are_all_ones_a_message_may_open_with(issue_type: str) -> None:
    """If they were tier 2 the gap would not matter -- they are not."""
    assert lead_rank(issue_type, None) <= _WORTH_OPENING_WITH


@pytest.mark.parametrize("industry", CAMPAIGN_INDUSTRIES)
def test_the_booking_offer_covers_both_ways_of_having_no_booking(
    industry: Industry,
) -> None:
    """Two detectors, one fix.

    `no_booking_or_enquiry_path` and `no_self_service_booking` describe the
    same lost appointment reached by different routes, and the same offer
    answers both.
    """
    by_path = {o.key for o in select_offers(industry, {"no_booking_or_enquiry_path"})}
    by_self_service = {o.key for o in select_offers(industry, {"no_self_service_booking"})}

    assert by_self_service, f"{industry.value} cannot answer no_self_service_booking"
    assert by_self_service & by_path, (
        "the same booking offer should answer both detectors"
    )


def test_no_playbook_offers_something_it_cannot_evidence() -> None:
    """The rule in the other direction, which the additions must not break.

    Every offer has to be reachable by at least one finding type, or it is a
    capability claim no evidence can justify.
    """
    for industry, playbook in PLAYBOOKS.items():
        for offer in playbook.offers:
            assert offer.requires_finding_types, (
                f"{industry.value}/{offer.key} requires no finding, so nothing "
                "could ever evidence it"
            )
