"""Policy engine tests.

These are the executable form of mission section 28. Each test names the
invariant it defends. The suite is deliberately exhaustive on *refusals*: the
expensive failure here is a false "allowed", so every gate gets an independent
test proving it alone is sufficient to block a send.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest
from titan.config import OperatingMode, Settings
from titan.db.enums import (
    CampaignStatus,
    ContactSource,
    LeadStatus,
    VerificationStatus,
)
from titan.policy.engine import DenyCode, SendContext, evaluate_send
from titan.policy.modes import Capability, resolve_mode

NOW = dt.datetime(2026, 8, 2, 14, 0, tzinfo=dt.UTC)


def fully_authorized_settings(**overrides) -> Settings:
    """Settings with every process-level gate deliberately opened."""
    base = {
        "environment": "test",
        "production_sending_enabled": True,
        "email_provider": "resend",
        "resend_api_key": "re_test_key_not_real_000000",
        "email_auth_preflight_acknowledged": True,
        "sender_mailing_address": "12 Fictional Row, Testville, TE1 1ST",
        "quiet_hours_enabled": False,
        "quota_min_spacing_seconds": 0,
    }
    base.update(overrides)
    return Settings(**base)


def sendable_context(**overrides) -> SendContext:
    """A context where every gate passes. Tests mutate one field at a time."""
    ctx = SendContext(
        settings=fully_authorized_settings(),
        now=NOW,
        workspace_mode=OperatingMode.CONTROLLED_AUTOPILOT,
        workspace_sending_authorized=True,
        campaign_mode=OperatingMode.CONTROLLED_AUTOPILOT,
        campaign_status=CampaignStatus.ACTIVE,
        campaign_sending_authorized=True,
        min_lead_score=70,
        require_verified_email=True,
        require_evidence_backed_claims=True,
        min_evidence_per_message=1,
        max_followups=3,
        allowed_contact_sources=frozenset(
            {ContactSource.FIRST_PARTY_WEBSITE, ContactSource.GOOGLE_PLACES}
        ),
        respect_quiet_hours=False,
        sender_authorization_errors=(),
        lead_status=LeadStatus.QUALIFIED,
        lead_score=82,
        lead_replied_at=None,
        followups_sent=0,
        last_contacted_at=None,
        contact_source=ContactSource.FIRST_PARTY_WEBSITE,
        contact_verification=VerificationStatus.PUBLISHED_FIRST_PARTY,
        contact_is_active=True,
        recipient_timezone="Europe/London",
        evidence_count=2,
        validation_passed=True,
        provider_idempotency_key="idem-abc-123",
        approval_decision="approved",
        approval_draft_version=1,
        draft_version=1,
        approval_expires_at=NOW + dt.timedelta(days=2),
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


# --------------------------------------------------------------------------
# The control case
# --------------------------------------------------------------------------
def test_fully_authorized_message_is_allowed() -> None:
    """If this ever fails, every other test in this file is vacuous."""
    decision = evaluate_send(sendable_context())
    assert decision.allowed, decision.reason_text()
    assert decision.denials == ()


def test_decision_snapshot_records_the_basis() -> None:
    decision = evaluate_send(sendable_context())
    snap = decision.snapshot
    assert snap["allowed"] is True
    assert snap["effective_mode"] == "controlled_autopilot"
    assert snap["lead_score"] == 82
    assert snap["evidence_count"] == 2


# --------------------------------------------------------------------------
# Invariant 21 / 8: production sending is off by default
# --------------------------------------------------------------------------
def test_default_settings_forbid_sending() -> None:
    """A Settings object with no configuration must not permit delivery."""
    default = Settings(environment="test")
    assert default.production_sending_enabled is False
    errors = default.sending_preflight_errors()
    assert errors, "default settings reported no blockers"

    decision = evaluate_send(sendable_context(settings=default))
    assert not decision.allowed
    assert DenyCode.GLOBAL_SENDING_DISABLED in decision.codes


def test_global_kill_switch_alone_blocks_everything() -> None:
    settings = fully_authorized_settings(production_sending_enabled=False)
    decision = evaluate_send(sendable_context(settings=settings))
    assert not decision.allowed
    assert DenyCode.GLOBAL_SENDING_DISABLED in decision.codes
    # The kill switch also drops the process ceiling, so mode forbids delivery.
    assert DenyCode.MODE_FORBIDS in decision.codes


def test_kill_switch_caps_effective_mode_below_autopilot() -> None:
    """No workspace or campaign setting may reach autopilot past the switch."""
    settings = fully_authorized_settings(production_sending_enabled=False)
    decision = evaluate_send(sendable_context(settings=settings))
    assert decision.effective_mode is not None
    assert decision.effective_mode.mode is OperatingMode.DRAFT_ONLY
    assert decision.effective_mode.limited_by == "process"


# --------------------------------------------------------------------------
# Mode resolution (mission section 3)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "process,workspace,campaign,expected,limited_by",
    [
        (
            OperatingMode.CONTROLLED_AUTOPILOT,
            OperatingMode.CONTROLLED_AUTOPILOT,
            OperatingMode.CONTROLLED_AUTOPILOT,
            OperatingMode.CONTROLLED_AUTOPILOT,
            "process",
        ),
        (
            OperatingMode.CONTROLLED_AUTOPILOT,
            OperatingMode.RESEARCH_ONLY,
            OperatingMode.CONTROLLED_AUTOPILOT,
            OperatingMode.RESEARCH_ONLY,
            "workspace",
        ),
        (
            OperatingMode.CONTROLLED_AUTOPILOT,
            OperatingMode.CONTROLLED_AUTOPILOT,
            OperatingMode.DRAFT_ONLY,
            OperatingMode.DRAFT_ONLY,
            "campaign",
        ),
        (
            OperatingMode.DRAFT_ONLY,
            OperatingMode.CONTROLLED_AUTOPILOT,
            OperatingMode.CONTROLLED_AUTOPILOT,
            OperatingMode.DRAFT_ONLY,
            "process",
        ),
    ],
)
def test_effective_mode_is_the_most_restrictive(
    process, workspace, campaign, expected, limited_by
) -> None:
    mode = resolve_mode(process, workspace, campaign)
    assert mode.mode is expected
    assert mode.limited_by == limited_by


def test_research_only_cannot_draft_or_send() -> None:
    mode = resolve_mode(
        OperatingMode.RESEARCH_ONLY,
        OperatingMode.CONTROLLED_AUTOPILOT,
        OperatingMode.CONTROLLED_AUTOPILOT,
    )
    assert mode.allows(Capability.CRAWL_WEBSITE)
    assert mode.allows(Capability.SCORE_LEAD)
    assert not mode.allows(Capability.GENERATE_DRAFT)
    assert not mode.allows(Capability.QUEUE_MESSAGE)
    assert not mode.can_send


def test_draft_only_can_draft_but_not_queue() -> None:
    mode = resolve_mode(
        OperatingMode.DRAFT_ONLY, OperatingMode.DRAFT_ONLY, OperatingMode.DRAFT_ONLY
    )
    assert mode.allows(Capability.GENERATE_DRAFT)
    assert not mode.allows(Capability.REQUEST_APPROVAL)
    assert not mode.allows(Capability.QUEUE_MESSAGE)
    assert not mode.can_send


def test_approval_required_never_grants_auto_approve() -> None:
    """The distinguishing property of approval_required (mission 3.3)."""
    mode = resolve_mode(
        OperatingMode.APPROVAL_REQUIRED,
        OperatingMode.APPROVAL_REQUIRED,
        OperatingMode.APPROVAL_REQUIRED,
    )
    assert mode.allows(Capability.QUEUE_MESSAGE)
    assert not mode.allows(Capability.AUTO_APPROVE)


def test_research_only_campaign_blocks_a_fully_authorized_message() -> None:
    decision = evaluate_send(sendable_context(campaign_mode=OperatingMode.RESEARCH_ONLY))
    assert not decision.allowed
    assert DenyCode.MODE_FORBIDS in decision.codes


# --------------------------------------------------------------------------
# Each gate alone must be sufficient to refuse
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "overrides,expected_code",
    [
        ({"workspace_sending_authorized": False}, DenyCode.WORKSPACE_NOT_AUTHORIZED),
        ({"campaign_sending_authorized": False}, DenyCode.CAMPAIGN_NOT_AUTHORIZED),
        ({"campaign_status": CampaignStatus.PAUSED}, DenyCode.CAMPAIGN_NOT_ACTIVE),
        ({"campaign_status": CampaignStatus.DRAFT}, DenyCode.CAMPAIGN_NOT_ACTIVE),
        ({"campaign_status": CampaignStatus.ARCHIVED}, DenyCode.CAMPAIGN_NOT_ACTIVE),
        (
            {"sender_authorization_errors": ("DKIM is not confirmed",)},
            DenyCode.SENDER_NOT_AUTHORIZED,
        ),
        ({"lead_score": 55}, DenyCode.SCORE_BELOW_THRESHOLD),
        ({"lead_score": None}, DenyCode.SCORE_BELOW_THRESHOLD),
        ({"lead_status": LeadStatus.DISQUALIFIED}, DenyCode.LEAD_TERMINAL),
        ({"lead_status": LeadStatus.SUPPRESSED}, DenyCode.LEAD_TERMINAL),
        ({"lead_status": LeadStatus.MEETING_BOOKED}, DenyCode.LEAD_TERMINAL),
        ({"contact_is_active": False}, DenyCode.CONTACT_INACTIVE),
        (
            {"contact_verification": VerificationStatus.UNVERIFIED},
            DenyCode.CONTACT_NOT_VERIFIED,
        ),
        (
            {"contact_verification": VerificationStatus.RISKY},
            DenyCode.CONTACT_NOT_VERIFIED,
        ),
        ({"is_suppressed": True}, DenyCode.SUPPRESSED),
        ({"evidence_count": 0}, DenyCode.NO_EVIDENCE),
        ({"validation_passed": False}, DenyCode.VALIDATION_FAILED),
        ({"provider_idempotency_key": None}, DenyCode.IDEMPOTENCY_KEY_MISSING),
        ({"provider_idempotency_key": ""}, DenyCode.IDEMPOTENCY_KEY_MISSING),
        ({"followups_sent": 9}, DenyCode.FOLLOWUP_LIMIT),
        ({"quota_exhausted_scope": "recipient_domain"}, DenyCode.QUOTA_EXHAUSTED),
    ],
)
def test_single_gate_failure_blocks_send(overrides, expected_code) -> None:
    decision = evaluate_send(sendable_context(**overrides))
    assert not decision.allowed, f"{overrides} was allowed"
    assert expected_code in decision.codes, decision.reason_text()


# --------------------------------------------------------------------------
# Invariant 6: no guessed addresses, ever
# --------------------------------------------------------------------------
def test_pattern_guessed_address_is_refused() -> None:
    decision = evaluate_send(sendable_context(contact_source=ContactSource.PATTERN_GUESS))
    assert not decision.allowed
    assert DenyCode.CONTACT_GUESSED in decision.codes


def test_guessed_address_refused_even_if_campaign_policy_lists_it() -> None:
    """A misconfigured campaign policy must not be able to permit a guess.

    This is the specific attack the ELIGIBLE_CONTACT_SOURCES frozenset defends:
    campaign policy is data and could be edited; eligibility is code.
    """
    decision = evaluate_send(
        sendable_context(
            contact_source=ContactSource.PATTERN_GUESS,
            allowed_contact_sources=frozenset(
                {ContactSource.PATTERN_GUESS, ContactSource.FIRST_PARTY_WEBSITE}
            ),
            contact_verification=VerificationStatus.PROVIDER_VERIFIED,
        )
    )
    assert not decision.allowed
    assert DenyCode.CONTACT_GUESSED in decision.codes


def test_source_not_permitted_by_campaign_is_refused() -> None:
    decision = evaluate_send(
        sendable_context(
            contact_source=ContactSource.PUBLIC_DIRECTORY,
            allowed_contact_sources=frozenset({ContactSource.FIRST_PARTY_WEBSITE}),
        )
    )
    assert not decision.allowed
    assert DenyCode.CONTACT_SOURCE_NOT_ALLOWED in decision.codes


# --------------------------------------------------------------------------
# Invariant 15: a replied lead gets no further outreach
# --------------------------------------------------------------------------
def test_replied_lead_cannot_receive_another_message() -> None:
    decision = evaluate_send(
        sendable_context(lead_replied_at=NOW - dt.timedelta(hours=3))
    )
    assert not decision.allowed
    assert DenyCode.LEAD_REPLIED in decision.codes


def test_reply_blocks_even_when_status_lags_behind() -> None:
    """replied_at is authoritative; a stale status must not permit a send."""
    decision = evaluate_send(
        sendable_context(
            lead_status=LeadStatus.CONTACTED,
            lead_replied_at=NOW - dt.timedelta(minutes=1),
        )
    )
    assert not decision.allowed
    assert DenyCode.LEAD_REPLIED in decision.codes


# --------------------------------------------------------------------------
# Approval integrity
# --------------------------------------------------------------------------
def test_missing_approval_blocks_in_approval_required_mode() -> None:
    decision = evaluate_send(
        sendable_context(
            workspace_mode=OperatingMode.APPROVAL_REQUIRED,
            campaign_mode=OperatingMode.APPROVAL_REQUIRED,
            approval_decision=None,
        )
    )
    assert not decision.allowed
    assert DenyCode.APPROVAL_MISSING in decision.codes


def test_rejected_approval_blocks() -> None:
    decision = evaluate_send(sendable_context(approval_decision="rejected"))
    assert not decision.allowed
    assert DenyCode.APPROVAL_MISSING in decision.codes


def test_edit_after_approval_invalidates_it() -> None:
    """approve -> edit -> send must not bypass human review."""
    decision = evaluate_send(sendable_context(approval_draft_version=1, draft_version=2))
    assert not decision.allowed
    assert DenyCode.APPROVAL_STALE in decision.codes


def test_expired_approval_blocks() -> None:
    decision = evaluate_send(
        sendable_context(approval_expires_at=NOW - dt.timedelta(seconds=1))
    )
    assert not decision.allowed
    assert DenyCode.APPROVAL_EXPIRED in decision.codes


def test_autopilot_does_not_require_a_human_approval_record() -> None:
    """The one place approval may be absent -- autopilot, and opted in."""
    decision = evaluate_send(
        sendable_context(
            campaign_auto_approve=True,
            approval_decision=None,
            approval_draft_version=None,
            approval_expires_at=None,
        )
    )
    assert decision.allowed, decision.reason_text()


def test_autopilot_alone_does_not_drop_the_human_gate() -> None:
    """Autopilot is permission to auto-approve, not an instruction to.

    The mode is the minimum of process, workspace and campaign, and the process
    ceiling comes from the global sending kill switch -- so without this, turning
    production sending on would have removed the human gate from every campaign
    at once, which is a per-campaign decision nobody made.
    """
    decision = evaluate_send(
        sendable_context(
            campaign_auto_approve=False,
            approval_decision=None,
            approval_draft_version=None,
            approval_expires_at=None,
        )
    )
    assert not decision.allowed
    assert DenyCode.APPROVAL_MISSING in decision.codes


def test_opting_in_does_not_by_itself_grant_autopilot() -> None:
    """The flag is the second half of an AND, not a way round the mode ladder."""
    decision = evaluate_send(
        sendable_context(
            campaign_mode=OperatingMode.APPROVAL_REQUIRED,
            campaign_auto_approve=True,
            approval_decision=None,
            approval_draft_version=None,
            approval_expires_at=None,
        )
    )
    assert not decision.allowed
    assert DenyCode.APPROVAL_MISSING in decision.codes


# --------------------------------------------------------------------------
# Quiet hours and spacing
# --------------------------------------------------------------------------
def test_quiet_hours_block_and_fail_closed_on_unknown_timezone() -> None:
    settings = fully_authorized_settings(
        quiet_hours_enabled=True, quiet_hours_start=20, quiet_hours_end=8
    )
    # 03:00 UTC is inside a 20:00-08:00 window.
    night = dt.datetime(2026, 8, 2, 3, 0, tzinfo=dt.UTC)
    decision = evaluate_send(
        sendable_context(
            settings=settings,
            now=night,
            respect_quiet_hours=True,
            recipient_timezone="UTC",
        )
    )
    assert DenyCode.QUIET_HOURS in decision.codes

    # Unknown timezone is treated as quiet: sending at an unknown local hour is
    # a complaint generator, so the safe default is to wait.
    unknown = evaluate_send(
        sendable_context(
            settings=settings, respect_quiet_hours=True, recipient_timezone=None
        )
    )
    assert DenyCode.QUIET_HOURS in unknown.codes

    bogus = evaluate_send(
        sendable_context(
            settings=settings,
            respect_quiet_hours=True,
            recipient_timezone="Not/AZone",
        )
    )
    assert DenyCode.QUIET_HOURS in bogus.codes


def test_daytime_send_is_not_blocked_by_quiet_hours() -> None:
    settings = fully_authorized_settings(
        quiet_hours_enabled=True, quiet_hours_start=20, quiet_hours_end=8
    )
    decision = evaluate_send(
        sendable_context(
            settings=settings,
            now=dt.datetime(2026, 8, 2, 14, 0, tzinfo=dt.UTC),
            respect_quiet_hours=True,
            recipient_timezone="UTC",
        )
    )
    assert DenyCode.QUIET_HOURS not in decision.codes


def test_minimum_spacing_is_enforced() -> None:
    settings = fully_authorized_settings(quota_min_spacing_seconds=90)
    too_soon = evaluate_send(
        sendable_context(
            settings=settings, last_contacted_at=NOW - dt.timedelta(seconds=30)
        )
    )
    assert DenyCode.SPACING in too_soon.codes

    long_enough = evaluate_send(
        sendable_context(
            settings=settings, last_contacted_at=NOW - dt.timedelta(seconds=120)
        )
    )
    assert DenyCode.SPACING not in long_enough.codes


# --------------------------------------------------------------------------
# General properties
# --------------------------------------------------------------------------
def test_all_denials_are_reported_not_just_the_first() -> None:
    """An operator fixing a blocked campaign should see every blocker at once."""
    decision = evaluate_send(
        sendable_context(
            workspace_sending_authorized=False,
            campaign_sending_authorized=False,
            campaign_status=CampaignStatus.PAUSED,
            lead_score=10,
            evidence_count=0,
            validation_passed=False,
            is_suppressed=True,
        )
    )
    assert not decision.allowed
    assert len(decision.denials) >= 6
    for code in (
        DenyCode.WORKSPACE_NOT_AUTHORIZED,
        DenyCode.CAMPAIGN_NOT_AUTHORIZED,
        DenyCode.CAMPAIGN_NOT_ACTIVE,
        DenyCode.SCORE_BELOW_THRESHOLD,
        DenyCode.NO_EVIDENCE,
        DenyCode.VALIDATION_FAILED,
        DenyCode.SUPPRESSED,
    ):
        assert code in decision.codes


def test_decision_is_falsy_when_denied() -> None:
    assert not evaluate_send(sendable_context(is_suppressed=True))
    assert evaluate_send(sendable_context())


def test_every_field_flip_is_either_neutral_or_restrictive() -> None:
    """No single field change may turn a denial into an approval unsafely.

    Sweeps each boolean gate to its unsafe value and asserts the decision never
    becomes allowed. Guards against a future refactor inverting a condition.
    """
    unsafe_flips = [
        {"workspace_sending_authorized": False},
        {"campaign_sending_authorized": False},
        {"contact_is_active": False},
        {"validation_passed": False},
        {"is_suppressed": True},
    ]
    for flip in unsafe_flips:
        ctx = sendable_context(**flip)
        assert not evaluate_send(ctx).allowed, f"{flip} should never be allowed"


def test_context_is_pure_data_and_engine_does_no_io() -> None:
    """The engine must be a pure function so it is exhaustively testable."""
    ctx = sendable_context()
    first = evaluate_send(ctx)
    second = evaluate_send(dataclasses.replace(ctx))
    assert first.allowed == second.allowed
    assert first.codes == second.codes
