"""The follow-up decision.

Mission section 13. Answers one question: *does this lead get another message,
and if so which step, and when?*

The single largest gap in the 0.2 build was that nothing ever answered it. The
``email_sequences`` and ``sequence_steps`` tables existed, ``max_followups`` was
enforced at the send boundary, and no code path ever created a second message --
so every campaign contacted each lead exactly once and stopped.

Built like :mod:`titan.policy.engine` and for the same reason: pure, no I/O, so
every rule can be exercised exhaustively without a database. The caller
assembles state inside one transaction and persists the result.

**Invariant 15 lives here.** A lead that replied gets nothing further, and that
is checked before anything else. The outbox worker checks it again immediately
before delivery; this module stops the follow-up ever being created.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum


class SkipReason(StrEnum):
    """Why a lead is not receiving a follow-up. Recorded, never silent."""

    REPLIED = "lead_replied"
    TERMINAL = "lead_status_terminal"
    SUPPRESSED = "recipient_suppressed"
    NEVER_CONTACTED = "no_first_message_sent"
    SEQUENCE_COMPLETE = "no_further_steps"
    SEQUENCE_INACTIVE = "sequence_not_active"
    FOLLOWUP_LIMIT = "campaign_followup_limit_reached"
    NOT_DUE_YET = "step_delay_has_not_elapsed"
    NO_ELIGIBLE_CONTACT = "no_eligible_contact_channel"
    ALREADY_PENDING = "a_draft_for_this_step_already_exists"


@dataclass(frozen=True, slots=True)
class Step:
    """One step of a sequence, as the planner needs it."""

    id: str
    step_number: int
    delay_days: int
    template_key: str
    requires_new_evidence: bool = True


@dataclass(frozen=True, slots=True)
class FollowUpContext:
    """Everything the decision depends on. Plain data; this module does no I/O."""

    now: dt.datetime

    # lead
    lead_status_is_terminal: bool
    replied_at: dt.datetime | None
    last_contacted_at: dt.datetime | None
    followups_sent: int

    # campaign policy
    max_followups: int

    # sequence
    sequence_is_active: bool
    steps: tuple[Step, ...]
    #: Step numbers already sent or already queued for this lead.
    completed_step_numbers: frozenset[int]

    # recipient
    has_eligible_contact: bool
    is_suppressed: bool

    #: True when a draft for the next step is already sitting in review.
    draft_pending_for_next_step: bool = False


@dataclass(frozen=True, slots=True)
class FollowUpPlan:
    """The decision. ``step`` is set only when ``due`` is True."""

    due: bool
    step: Step | None = None
    #: When this lead should next be looked at. None means "never again".
    next_action_at: dt.datetime | None = None
    skip_reason: SkipReason | None = None
    detail: str | None = None

    def __bool__(self) -> bool:
        return self.due


def plan_followup(ctx: FollowUpContext) -> FollowUpPlan:
    """Decide whether a follow-up is owed, and when.

    Fails closed: every branch that cannot establish a positive answer returns
    ``due=False`` with a reason. There is no default-yes path.
    """
    # ---- invariant 15, before anything else --------------------------------
    if ctx.replied_at is not None:
        return _skip(
            SkipReason.REPLIED,
            f"replied at {ctx.replied_at.isoformat()}; the sequence stops permanently",
        )
    if ctx.lead_status_is_terminal:
        return _skip(SkipReason.TERMINAL, "lead is in a terminal status")
    if ctx.is_suppressed:
        return _skip(SkipReason.SUPPRESSED, "recipient is suppressed")
    if not ctx.has_eligible_contact:
        return _skip(SkipReason.NO_ELIGIBLE_CONTACT, "no eligible contact channel")

    # ---- a follow-up follows something -------------------------------------
    if ctx.last_contacted_at is None:
        return _skip(
            SkipReason.NEVER_CONTACTED,
            "no first message has been delivered, so there is nothing to follow up",
        )

    if not ctx.sequence_is_active:
        return _skip(SkipReason.SEQUENCE_INACTIVE, "sequence is not active")

    # ---- campaign ceiling ---------------------------------------------------
    # Counted against messages already sent, not against steps defined: a
    # campaign that lowers max_followups must stop leads mid-sequence.
    if ctx.followups_sent >= ctx.max_followups:
        return _skip(
            SkipReason.FOLLOWUP_LIMIT,
            f"{ctx.followups_sent} follow-ups sent, campaign allows {ctx.max_followups}",
        )

    # ---- which step is next -------------------------------------------------
    remaining = sorted(
        (s for s in ctx.steps if s.step_number not in ctx.completed_step_numbers),
        key=lambda s: s.step_number,
    )
    if not remaining:
        return _skip(SkipReason.SEQUENCE_COMPLETE, "every step has been sent")

    step = remaining[0]

    if ctx.draft_pending_for_next_step:
        return _skip(
            SkipReason.ALREADY_PENDING,
            f"a draft for step {step.step_number} is already awaiting review",
        )

    # ---- has the delay elapsed ---------------------------------------------
    due_at = ctx.last_contacted_at + dt.timedelta(days=step.delay_days)
    if ctx.now < due_at:
        return FollowUpPlan(
            due=False,
            step=None,
            next_action_at=due_at,
            skip_reason=SkipReason.NOT_DUE_YET,
            detail=(
                f"step {step.step_number} is due {due_at.isoformat()}, "
                f"{step.delay_days} day(s) after the last contact"
            ),
        )

    return FollowUpPlan(due=True, step=step, next_action_at=ctx.now)


def _skip(reason: SkipReason, detail: str) -> FollowUpPlan:
    """A permanent stop: next_action_at is None so nothing reschedules it."""
    return FollowUpPlan(
        due=False, step=None, next_action_at=None, skip_reason=reason, detail=detail
    )


__all__ = [
    "FollowUpContext",
    "FollowUpPlan",
    "SkipReason",
    "Step",
    "plan_followup",
]
