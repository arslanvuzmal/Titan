"""The send-authorization decision.

This module answers exactly one question: *may this specific message be handed
to a real email provider right now?* Everything in mission section 3.4 is
checked here, and the answer is always accompanied by every reason it was no --
not just the first -- so an operator can fix the whole problem in one pass.

Two properties matter more than anything else in this file:

1. **Fail closed.** Any unknown, missing, or unreadable input is a refusal. The
   default return is "no"; a "yes" requires every check to have actively passed.
2. **Evaluated twice.** Once when a draft is queued, and again by the outbox
   worker immediately before the provider call. State can change in between --
   the campaign may be paused, the recipient may reply or unsubscribe, the
   quota may be consumed by a sibling worker -- and the second evaluation is
   the one that actually governs delivery (invariants 5, 8, 9, 15).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from titan.config import OperatingMode, Settings
from titan.db.enums import (
    ELIGIBLE_CONTACT_SOURCES,
    TERMINAL_LEAD_STATUSES,
    CampaignStatus,
    ContactSource,
    LeadStatus,
    Region,
    SubRegion,
    VerificationStatus,
    verification_permits_sending,
)
from titan.intelligence.domain_health import DomainHealth
from titan.policy.calendars import holiday_on, resolve_country
from titan.policy.modes import Capability, EffectiveMode, resolve_mode
from titan.policy.schedule import SendWindow, local_time, resolve_timezone


class DenyCode(StrEnum):
    """Machine-readable refusal reasons. Surfaced in the UI and in metrics."""

    GLOBAL_SENDING_DISABLED = "global_sending_disabled"
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    EMAIL_AUTH_NOT_ACKNOWLEDGED = "email_auth_not_acknowledged"
    MAILING_ADDRESS_MISSING = "mailing_address_missing"
    MODE_FORBIDS = "mode_forbids_capability"
    WORKSPACE_NOT_AUTHORIZED = "workspace_not_authorized"
    CAMPAIGN_NOT_AUTHORIZED = "campaign_not_authorized"
    CAMPAIGN_NOT_ACTIVE = "campaign_not_active"
    SENDER_NOT_AUTHORIZED = "sender_identity_not_authorized"
    SCORE_BELOW_THRESHOLD = "lead_score_below_threshold"
    LEAD_TERMINAL = "lead_status_terminal"
    LEAD_REPLIED = "lead_already_replied"
    CONTACT_SOURCE_NOT_ALLOWED = "contact_source_not_allowed"
    CONTACT_GUESSED = "contact_address_was_guessed"
    CONTACT_NOT_VERIFIED = "contact_not_verified"
    CONTACT_INACTIVE = "contact_channel_inactive"
    RECIPIENT_DOMAIN_BLOCKED = "recipient_domain_blocked"
    SUPPRESSED = "recipient_suppressed"
    NO_EVIDENCE = "no_evidence_backed_claims"
    VALIDATION_FAILED = "message_validation_failed"
    APPROVAL_MISSING = "approval_missing"
    APPROVAL_STALE = "approval_does_not_match_draft_version"
    APPROVAL_EXPIRED = "approval_expired"
    FOLLOWUP_LIMIT = "followup_limit_reached"
    QUOTA_EXHAUSTED = "quota_exhausted"
    QUIET_HOURS = "recipient_quiet_hours"
    OUTSIDE_SEND_WINDOW = "outside_campaign_send_window"
    SPACING = "minimum_spacing_not_elapsed"
    IDEMPOTENCY_KEY_MISSING = "provider_idempotency_key_missing"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class Denial:
    code: DenyCode
    detail: str

    def __str__(self) -> str:
        return f"{self.code.value}: {self.detail}"


@dataclass(frozen=True, slots=True)
class Decision:
    """The result of a policy evaluation.

    ``allowed`` is True only when ``denials`` is empty. There is no partial
    approval and no override flag -- a caller cannot proceed past a denial.
    """

    allowed: bool
    denials: tuple[Denial, ...] = ()
    effective_mode: EffectiveMode | None = None
    #: Everything the decision was based on, stored on the approval record and
    #: the outbox row so a past decision can be explained after the fact.
    snapshot: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.allowed

    @property
    def codes(self) -> tuple[DenyCode, ...]:
        return tuple(d.code for d in self.denials)

    def reason_text(self) -> str:
        return "; ".join(str(d) for d in self.denials) or "allowed"


@dataclass(slots=True)
class SendContext:
    """Everything needed to decide. Plain data, so this module does no I/O.

    Callers assemble this from the database inside a single transaction; the
    policy engine itself is pure, which is what makes it exhaustively testable.
    """

    settings: Settings
    now: dt.datetime

    # workspace
    workspace_mode: OperatingMode
    workspace_sending_authorized: bool

    # campaign
    campaign_mode: OperatingMode
    campaign_status: CampaignStatus
    campaign_sending_authorized: bool
    min_lead_score: int
    require_verified_email: bool
    require_evidence_backed_claims: bool
    min_evidence_per_message: int
    max_followups: int
    allowed_contact_sources: frozenset[ContactSource]
    respect_quiet_hours: bool

    # sender identity
    sender_authorization_errors: tuple[str, ...]

    # lead
    lead_status: LeadStatus
    lead_score: int | None
    lead_replied_at: dt.datetime | None
    followups_sent: int
    last_contacted_at: dt.datetime | None

    # contact
    contact_source: ContactSource
    contact_verification: VerificationStatus
    contact_is_active: bool
    recipient_timezone: str | None

    # message
    evidence_count: int
    validation_passed: bool
    provider_idempotency_key: str | None

    # approval (None when the mode does not require one)
    approval_decision: str | None = None
    approval_draft_version: int | None = None
    draft_version: int | None = None
    approval_expires_at: dt.datetime | None = None

    # late-binding state re-read by the outbox worker
    is_suppressed: bool = False
    suppression_reason: str | None = None
    quota_exhausted_scope: str | None = None
    #: Live classification of the recipient's domain. Defaults to UNKNOWN, which
    #: denies nothing: a caller that cannot supply it loses a check rather than
    #: gaining a refusal.
    recipient_domain_health: DomainHealth = DomainHealth.UNKNOWN
    #: The campaign's working hours, in the recipient's local time. None means
    #: the caller did not supply one, and only the global quiet hours apply.
    send_window: SendWindow | None = None
    #: The campaign's market. Supplies a timezone for a recipient who has none.
    campaign_region: Region = Region.UNSPECIFIED
    #: The band this recipient's own address falls in, derived from their state
    #: and coordinates. More specific than anything the campaign can declare.
    recipient_subregion: SubRegion = SubRegion.UNSPECIFIED
    #: The band the campaign works, for recipients whose address resolved to none.
    campaign_subregion: SubRegion = SubRegion.UNSPECIFIED
    #: The recipient's own country, for their public-holiday calendar. Falls
    #: back to the campaign's market where that names one country unambiguously.
    recipient_country: str | None = None
    #: Their state or province, used when the calendar recognises it.
    recipient_admin_area: str | None = None


def evaluate_send(ctx: SendContext) -> Decision:
    """Decide whether one specific message may be delivered.

    Collects *all* denials rather than short-circuiting: an operator fixing a
    blocked campaign should see every blocker at once.
    """
    denials: list[Denial] = []

    mode = resolve_mode(
        process_mode=_process_mode(ctx.settings),
        workspace_mode=ctx.workspace_mode,
        campaign_mode=ctx.campaign_mode,
    )

    # ---- gate 1: the process ------------------------------------------
    for problem in ctx.settings.sending_preflight_errors():
        code = DenyCode.GLOBAL_SENDING_DISABLED
        if "EMAIL_PROVIDER" in problem or "RESEND_API_KEY" in problem:
            code = DenyCode.PROVIDER_NOT_CONFIGURED
        elif "PREFLIGHT" in problem:
            code = DenyCode.EMAIL_AUTH_NOT_ACKNOWLEDGED
        elif "MAILING_ADDRESS" in problem:
            code = DenyCode.MAILING_ADDRESS_MISSING
        denials.append(Denial(code, problem))

    if not mode.allows(Capability.DELIVER_MESSAGE):
        denials.append(
            Denial(
                DenyCode.MODE_FORBIDS,
                f"effective mode {mode.mode.value} (limited by {mode.limited_by}) "
                "does not permit delivery",
            )
        )

    # ---- gate 2: the workspace ----------------------------------------
    if not ctx.workspace_sending_authorized:
        denials.append(
            Denial(
                DenyCode.WORKSPACE_NOT_AUTHORIZED, "workspace sending is not authorized"
            )
        )

    # ---- gate 3: the campaign -----------------------------------------
    if not ctx.campaign_sending_authorized:
        denials.append(
            Denial(DenyCode.CAMPAIGN_NOT_AUTHORIZED, "campaign sending is not authorized")
        )
    if ctx.campaign_status is not CampaignStatus.ACTIVE:
        denials.append(
            Denial(
                DenyCode.CAMPAIGN_NOT_ACTIVE,
                f"campaign status is {ctx.campaign_status.value}",
            )
        )

    # ---- gate 4: the sender identity ----------------------------------
    for problem in ctx.sender_authorization_errors:
        denials.append(Denial(DenyCode.SENDER_NOT_AUTHORIZED, problem))

    # ---- the lead ------------------------------------------------------
    if ctx.lead_status in TERMINAL_LEAD_STATUSES:
        denials.append(
            Denial(DenyCode.LEAD_TERMINAL, f"lead status is {ctx.lead_status.value}")
        )
    if ctx.lead_replied_at is not None:
        denials.append(
            Denial(
                DenyCode.LEAD_REPLIED,
                f"lead replied at {ctx.lead_replied_at.isoformat()}",
            )
        )
    if ctx.lead_score is None:
        denials.append(Denial(DenyCode.SCORE_BELOW_THRESHOLD, "lead has not been scored"))
    elif ctx.lead_score < ctx.min_lead_score:
        denials.append(
            Denial(
                DenyCode.SCORE_BELOW_THRESHOLD,
                f"score {ctx.lead_score} is below the campaign threshold "
                f"{ctx.min_lead_score}",
            )
        )
    if ctx.followups_sent > ctx.max_followups:
        denials.append(
            Denial(
                DenyCode.FOLLOWUP_LIMIT,
                f"{ctx.followups_sent} follow-ups sent, limit is {ctx.max_followups}",
            )
        )

    # ---- the recipient -------------------------------------------------
    # Invariant 6. PATTERN_GUESS is not in ELIGIBLE_CONTACT_SOURCES, so this
    # refuses a guessed address even if a campaign policy were edited to list it.
    if ctx.contact_source is ContactSource.PATTERN_GUESS:
        denials.append(
            Denial(
                DenyCode.CONTACT_GUESSED,
                "address was pattern-guessed and is never eligible for sending",
            )
        )
    elif ctx.contact_source not in ELIGIBLE_CONTACT_SOURCES:
        denials.append(
            Denial(
                DenyCode.CONTACT_SOURCE_NOT_ALLOWED,
                f"contact source {ctx.contact_source.value} is not an eligible source",
            )
        )
    elif ctx.contact_source not in ctx.allowed_contact_sources:
        denials.append(
            Denial(
                DenyCode.CONTACT_SOURCE_NOT_ALLOWED,
                f"contact source {ctx.contact_source.value} is not permitted by this campaign",
            )
        )

    if not ctx.contact_is_active:
        denials.append(Denial(DenyCode.CONTACT_INACTIVE, "contact channel is inactive"))

    # Provenance is part of this test, not just the status. A CATCH_ALL domain
    # tells us the server will accept anything, so what decides is who put the
    # address in front of us -- see enums.verification_permits_sending.
    if ctx.require_verified_email and not verification_permits_sending(
        ctx.contact_verification, ctx.contact_source
    ):
        denials.append(
            Denial(
                DenyCode.CONTACT_NOT_VERIFIED,
                f"verification status is {ctx.contact_verification.value} for an "
                f"address sourced from {ctx.contact_source.value}; campaign "
                "requires a verified or first-party-published address",
            )
        )

    # The recipient's domain, as it is *now* rather than as it was when the
    # address was discovered. A contact channel's verification status is written
    # once and can be weeks old; a complaint arriving this morning has to stop
    # the message queued for that domain last week, and only a live read does
    # that. DEGRADED deliberately does not deny here -- it already refused the
    # address at discovery, and re-blocking mail a human has since approved on
    # the strength of a bounce rate over four messages is not worth the lead.
    if ctx.recipient_domain_health is DomainHealth.BLOCKED:
        denials.append(
            Denial(
                DenyCode.RECIPIENT_DOMAIN_BLOCKED,
                "recipient domain is blocked by its own delivery record",
            )
        )

    if ctx.is_suppressed:
        denials.append(
            Denial(
                DenyCode.SUPPRESSED,
                f"recipient is suppressed ({ctx.suppression_reason or 'unspecified'})",
            )
        )

    # ---- the message ---------------------------------------------------
    if ctx.require_evidence_backed_claims:
        if ctx.evidence_count < max(1, ctx.min_evidence_per_message):
            denials.append(
                Denial(
                    DenyCode.NO_EVIDENCE,
                    f"{ctx.evidence_count} evidence links, campaign requires "
                    f"{max(1, ctx.min_evidence_per_message)}",
                )
            )
    if not ctx.validation_passed:
        denials.append(
            Denial(DenyCode.VALIDATION_FAILED, "message policy validation did not pass")
        )
    if not ctx.provider_idempotency_key:
        denials.append(
            Denial(
                DenyCode.IDEMPOTENCY_KEY_MISSING,
                "no provider idempotency key; a retry could duplicate the email",
            )
        )

    # ---- the approval --------------------------------------------------
    denials.extend(_approval_denials(ctx, mode))

    # ---- late-bound resources ------------------------------------------
    if ctx.quota_exhausted_scope:
        denials.append(
            Denial(
                DenyCode.QUOTA_EXHAUSTED, f"{ctx.quota_exhausted_scope} quota exhausted"
            )
        )
    # The campaign's working hours first, then the process-wide quiet hours as
    # a floor beneath them. Only one of the two is reported: a message at 3am on
    # a Sunday is outside both, and saying so twice tells an operator nothing
    # they did not learn from the first line.
    if ctx.respect_quiet_hours:
        outside = _outside_send_window(ctx)
        if outside is not None:
            denials.append(Denial(DenyCode.OUTSIDE_SEND_WINDOW, outside))
        elif _in_quiet_hours(ctx):
            denials.append(
                Denial(
                    DenyCode.QUIET_HOURS,
                    f"local time for {ctx.recipient_timezone} is inside quiet hours",
                )
            )
    if _spacing_violated(ctx):
        denials.append(
            Denial(
                DenyCode.SPACING,
                f"minimum spacing of {ctx.settings.quota_min_spacing_seconds}s "
                "has not elapsed since the last send",
            )
        )

    return Decision(
        allowed=not denials,
        denials=tuple(denials),
        effective_mode=mode,
        snapshot=_snapshot(ctx, mode, denials),
    )


def _process_mode(settings: Settings) -> OperatingMode:
    """The process-level ceiling.

    Derived from the kill switch rather than configured separately: if global
    sending is off, the process ceiling is approval_required at most, so no
    amount of workspace or campaign configuration can reach autopilot.
    """
    if not settings.production_sending_enabled:
        return OperatingMode.DRAFT_ONLY
    return OperatingMode.CONTROLLED_AUTOPILOT


def _approval_denials(ctx: SendContext, mode: EffectiveMode) -> list[Denial]:
    denials: list[Denial] = []
    if mode.allows(Capability.AUTO_APPROVE) and ctx.approval_decision is None:
        # controlled_autopilot: the policy decision itself stands in for the
        # human one, and is recorded with the same rigour.
        return denials

    if ctx.approval_decision is None:
        return [Denial(DenyCode.APPROVAL_MISSING, "no approval record for this draft")]
    if ctx.approval_decision != "approved":
        return [
            Denial(
                DenyCode.APPROVAL_MISSING,
                f"approval decision is {ctx.approval_decision!r}, not 'approved'",
            )
        ]
    # Editing a draft bumps its version, which invalidates a prior approval.
    # Without this, "approve then edit then send" bypasses human review.
    if (
        ctx.approval_draft_version is not None
        and ctx.draft_version is not None
        and ctx.approval_draft_version != ctx.draft_version
    ):
        denials.append(
            Denial(
                DenyCode.APPROVAL_STALE,
                f"approval covers draft version {ctx.approval_draft_version} but the "
                f"draft is now version {ctx.draft_version}",
            )
        )
    if ctx.approval_expires_at is not None and ctx.approval_expires_at <= ctx.now:
        denials.append(
            Denial(
                DenyCode.APPROVAL_EXPIRED,
                f"approval expired at {ctx.approval_expires_at.isoformat()}",
            )
        )
    return denials


def _outside_send_window(ctx: SendContext) -> str | None:
    """Why this message may not go now, or None when the window is open.

    Returns a reason rather than a boolean because there are three distinct ways
    to be refused here and they call for different fixes: no window configured
    at all, no clock to measure against, or simply the wrong hour.

    Fails closed on an unresolvable timezone, as the quiet-hours check it
    replaces always did -- but it fails closed far less often, because a
    campaign that declares its market can now answer for a recipient whose
    location Places never resolved.
    """
    if ctx.send_window is None:
        return None
    if not ctx.send_window.is_usable:
        return f"campaign send window is not usable ({ctx.send_window.describe()})"

    timezone = resolve_timezone(
        ctx.recipient_timezone,
        ctx.campaign_region,
        recipient_subregion=ctx.recipient_subregion,
        campaign_subregion=ctx.campaign_subregion,
    )
    local = local_time(ctx.now, timezone)
    if local is None:
        return (
            "no usable timezone for the recipient and none implied by the "
            f"campaign's market ({ctx.campaign_region.value})"
        )
    # A public holiday closes the window for the day, wherever the recipient is.
    # Named rather than folded into the window text: "waiting out Christmas Day"
    # is a better answer than "outside the send window", and it is the answer
    # somebody reading a deferred queue in late December actually needs.
    country = resolve_country(ctx.recipient_country, ctx.campaign_region)
    holiday = holiday_on(local.date(), country=country, subdiv=ctx.recipient_admin_area)
    if holiday is not None:
        return f"{local:%a %d %b} is {holiday} in {country}"

    lookup = (
        (lambda day: holiday_on(day, country=country, subdiv=ctx.recipient_admin_area))
        if country
        else None
    )
    if ctx.send_window.is_open_at(local, lookup):
        return None
    return (
        f"{local:%a %H:%M} in {timezone} is outside the campaign's send window "
        f"({ctx.send_window.describe()})"
    )


def _in_quiet_hours(ctx: SendContext) -> bool:
    """Whether it is currently quiet hours for the recipient.

    Fails *closed* when the timezone is unknown: an unknown local time is
    treated as quiet, because sending at 3am is a complaint generator.
    """
    if not ctx.settings.quiet_hours_enabled:
        return False
    if not ctx.recipient_timezone:
        return True
    try:
        import zoneinfo

        local = ctx.now.astimezone(zoneinfo.ZoneInfo(ctx.recipient_timezone))
    except Exception:
        return True

    start = ctx.settings.quiet_hours_start
    end = ctx.settings.quiet_hours_end
    hour = local.hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    # Window wraps midnight, e.g. 20:00 -> 08:00.
    return hour >= start or hour < end


def _spacing_violated(ctx: SendContext) -> bool:
    spacing = ctx.settings.quota_min_spacing_seconds
    if spacing <= 0 or ctx.last_contacted_at is None:
        return False
    elapsed = (ctx.now - ctx.last_contacted_at).total_seconds()
    return elapsed < spacing


def _snapshot(
    ctx: SendContext, mode: EffectiveMode, denials: list[Denial]
) -> dict[str, Any]:
    return {
        "evaluated_at": ctx.now.isoformat(),
        "effective_mode": mode.mode.value,
        "mode_limited_by": mode.limited_by,
        "process_mode": mode.process_mode.value,
        "workspace_mode": mode.workspace_mode.value,
        "campaign_mode": mode.campaign_mode.value,
        "lead_status": ctx.lead_status.value,
        "lead_score": ctx.lead_score,
        "min_lead_score": ctx.min_lead_score,
        "contact_source": ctx.contact_source.value,
        "contact_verification": ctx.contact_verification.value,
        "evidence_count": ctx.evidence_count,
        "validation_passed": ctx.validation_passed,
        "is_suppressed": ctx.is_suppressed,
        "followups_sent": ctx.followups_sent,
        "denials": [{"code": d.code.value, "detail": d.detail} for d in denials],
        "allowed": not denials,
    }


__all__ = ["Decision", "Denial", "DenyCode", "SendContext", "evaluate_send"]
