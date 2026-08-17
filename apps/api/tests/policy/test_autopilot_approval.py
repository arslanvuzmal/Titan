"""Autopilot and the approval record it no longer needs.

The gate used to require ``approval_decision is None`` before autopilot could
skip it. That made a draft which had once been reviewed *harder* to send than
one that never had: the version guard fired on the leftover record and refused.
Observed on the live workspace as "approval covers draft version 1 but the
draft is now version 3", on drafts nothing was waiting to review.
"""

from __future__ import annotations

from titan.config import OperatingMode
from titan.policy.engine import evaluate_send

from tests.policy.test_send_authorization import sendable_context


def _codes(**overrides: object) -> set[str]:
    ctx = sendable_context(campaign_auto_approve=True, **overrides)
    return {d.code.value for d in evaluate_send(ctx).denials}


def test_a_stale_approval_no_longer_blocks_an_autopilot_campaign() -> None:
    """The operator removed the requirement to review. A record left over from
    when there was one is history, not a veto."""
    codes = _codes(
        approval_decision="approved", approval_draft_version=1, draft_version=3
    )

    assert "approval_does_not_match_draft_version" not in codes


def test_no_approval_at_all_is_still_fine_under_autopilot() -> None:
    """The behaviour that already worked, kept."""
    assert "approval_missing" not in _codes(approval_decision=None)


def test_a_rejection_still_stands() -> None:
    """Somebody looked at this message and said no. Autonomy removed the
    requirement to review, not the authority of a review that happened."""
    assert "approval_missing" in _codes(approval_decision="rejected")


def test_changes_requested_is_a_refusal_too() -> None:
    """Anything that is not an approval is a person declining to send this."""
    assert "approval_missing" in _codes(approval_decision="changes_requested")


def test_a_campaign_not_opted_in_still_needs_its_approval_current() -> None:
    """Both halves are required. The mode is resolved from process, workspace
    and campaign, so autopilot alone would make the global kill switch a global
    answer to a per-campaign question."""
    ctx = sendable_context(
        campaign_auto_approve=False,
        approval_decision="approved",
        approval_draft_version=1,
        draft_version=3,
    )
    codes = {d.code.value for d in evaluate_send(ctx).denials}

    assert "approval_does_not_match_draft_version" in codes


def test_a_workspace_short_of_autopilot_still_needs_an_approval() -> None:
    codes = _codes(workspace_mode=OperatingMode.APPROVAL_REQUIRED, approval_decision=None)

    assert "approval_missing" in codes
