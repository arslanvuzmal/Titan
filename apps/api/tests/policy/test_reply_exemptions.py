"""A reply lifts the cold-outreach targeting rules, and nothing else."""

from __future__ import annotations

from coldops.db.enums import LeadStatus
from coldops.policy.engine import REPLY_EXEMPT, DenyCode, evaluate_send

from tests.policy.test_send_authorization import sendable_context


def codes(decision) -> set[DenyCode]:
    return {d.code for d in decision.denials}


def test_a_lead_that_replied_can_be_answered() -> None:
    cold = evaluate_send(sendable_context(lead_status=LeadStatus.REPLIED))
    assert DenyCode.LEAD_REPLIED in codes(cold)

    reply = evaluate_send(sendable_context(lead_status=LeadStatus.REPLIED, is_reply=True))
    assert reply.allowed, reply.denials


def test_a_reply_does_not_need_evidence_or_a_score() -> None:
    reply = evaluate_send(
        sendable_context(evidence_count=0, lead_score=None, is_reply=True)
    )
    assert not codes(reply) & {DenyCode.NO_EVIDENCE, DenyCode.SCORE_BELOW_THRESHOLD}


def test_suppression_still_stops_a_reply() -> None:
    reply = evaluate_send(
        sendable_context(
            is_suppressed=True, suppression_reason="unsubscribe", is_reply=True
        )
    )
    assert DenyCode.SUPPRESSED in codes(reply)


def test_a_suppressed_or_disqualified_lead_gets_no_reply() -> None:
    for status in (LeadStatus.SUPPRESSED, LeadStatus.DISQUALIFIED, LeadStatus.REJECTED):
        reply = evaluate_send(sendable_context(lead_status=status, is_reply=True))
        assert DenyCode.LEAD_TERMINAL in codes(reply), status


def test_the_exemptions_never_include_a_safety_gate() -> None:
    safety = {
        DenyCode.SUPPRESSED,
        DenyCode.MODE_FORBIDS,
        DenyCode.SENDER_NOT_AUTHORIZED,
        DenyCode.APPROVAL_MISSING,
        DenyCode.APPROVAL_STALE,
        DenyCode.CONTACT_INACTIVE,
        DenyCode.RECIPIENT_DOMAIN_BLOCKED,
        DenyCode.QUOTA_EXHAUSTED,
        DenyCode.OUTSIDE_SEND_WINDOW,
    }
    assert not REPLY_EXEMPT & safety
