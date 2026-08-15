"""How a campaign is doing, in one word.

The campaign already had a *status* -- draft, active, paused -- which records
what somebody intended, not what is happening. A campaign can be active,
correctly configured, and quietly bouncing a fifth of its mail; the status says
"active" throughout.

Six states, and the two that matter most are the ones that look like nothing is
wrong. LEARNING is a campaign that has not sent enough for any rate to mean
anything, and treating it as healthy is how a bad campaign gets scaled on four
data points. THROTTLED is a campaign whose volume has been cut, which is
invisible on a page showing the configured limit.

**Thresholds are not restated here.** ``classify`` calls
``deliverability.check_reputation`` and reads its signals, exactly as
``sender_health`` does. A campaign judged healthy by numbers the send gate
considers a pause condition would be worse than no judgement at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from titan.db.enums import CampaignStatus
from titan.delivery import deliverability
from titan.delivery.deliverability import ReputationWindow, Severity

#: Replies per contacted lead at or above which a campaign is worth more volume.
#: Cold outreach reply rates are single-digit percentages; five per cent is a
#: campaign finding people who want to hear from it.
SCALING_REPLY_RATE = 0.05

#: Contacted leads below which the reply rate is anecdote. Lower than the
#: deliverability sample floor because a reply is a much rarer event than a
#: bounce and waiting for fifty would mean never scaling anything.
MIN_CONTACTED_TO_SCALE = 20


class CampaignHealth(StrEnum):
    #: A human stopped it, or it is finished. Not a judgement.
    PAUSED = "paused"
    #: Too little has happened for any rate to mean anything.
    LEARNING = "learning"
    #: Bouncing or complained about. The manager reduces volume here.
    DEGRADED = "degraded"
    #: Volume has been cut below what the campaign is configured for.
    THROTTLED = "throttled"
    #: Safe, and earning more volume than it is currently allowed.
    SCALING = "scaling"
    HEALTHY = "healthy"


@dataclass(frozen=True, slots=True)
class CampaignWindow:
    """One campaign's recent outcomes, plus what it is currently allowed."""

    campaign_id: str
    status: CampaignStatus = CampaignStatus.ACTIVE

    window: ReputationWindow = field(default_factory=lambda: ReputationWindow(0, 0, 0, 0))
    #: Leads that received at least one message. Replies are per lead, not per
    #: message -- a person answers once however many times they were asked.
    contacted: int = 0
    replied: int = 0

    #: What a human configured, and what the campaign may actually send today.
    configured_limit: int = 0
    effective_limit: int = 0
    #: Qualified leads waiting. Volume is worth nothing without them.
    leads_available: int = 0

    @property
    def reply_rate(self) -> float:
        return self.replied / self.contacted if self.contacted else 0.0

    @property
    def is_throttled(self) -> bool:
        return self.effective_limit < self.configured_limit

    @property
    def has_headroom(self) -> bool:
        """Whether more volume would actually be used.

        A campaign at its ceiling with no leads waiting cannot use another
        message a day, and scaling it would be a decision with no effect and a
        row in the audit trail claiming otherwise.
        """
        return self.leads_available > 0 and self.effective_limit < self.configured_limit

    @property
    def can_judge_replies(self) -> bool:
        return self.contacted >= MIN_CONTACTED_TO_SCALE


def classify(window: CampaignWindow) -> CampaignHealth:
    """Reduce a campaign's recent history to one state, worst first."""
    if window.status is not CampaignStatus.ACTIVE:
        return CampaignHealth.PAUSED

    signals = deliverability.check_reputation(window.window)
    if any(s.severity is Severity.BLOCK for s in signals):
        return CampaignHealth.DEGRADED

    if not window.window.has_signal:
        # Deliberately before the WARN check. Below the sample floor
        # check_reputation returns nothing at all, so a campaign with three
        # sends and one bounce reaches here rather than looking healthy.
        return CampaignHealth.LEARNING

    if any(s.severity is Severity.WARN for s in signals):
        return CampaignHealth.DEGRADED

    if window.is_throttled:
        # Both states describe a campaign below its ceiling. What separates them
        # is why it is still there: one is being held down and the other is on
        # its way back up, and an operator needs to know which. Checking
        # throttled first without this would make SCALING unreachable -- the two
        # conditions overlap exactly.
        if (
            window.can_judge_replies
            and window.reply_rate >= SCALING_REPLY_RATE
            and window.has_headroom
        ):
            return CampaignHealth.SCALING
        return CampaignHealth.THROTTLED

    return CampaignHealth.HEALTHY


def explain(window: CampaignWindow, health: CampaignHealth) -> str:
    """One line, in the terms the decision was actually made on."""
    if health is CampaignHealth.PAUSED:
        return f"campaign status is {window.status.value}"

    counts = (
        f"{window.window.sent} sent, {window.window.hard_bounced} bounced, "
        f"{window.window.complained} complained, {window.replied} repl(ies) "
        f"from {window.contacted} contacted"
    )
    if health is CampaignHealth.LEARNING:
        return (
            f"{counts} -- below the sample floor of "
            f"{deliverability.MIN_SAMPLE_FOR_RATES}, so no rate means anything yet"
        )
    if health is CampaignHealth.DEGRADED:
        reasons = "; ".join(
            s.detail for s in deliverability.check_reputation(window.window)
        )
        return f"{counts}. {reasons}"
    if health is CampaignHealth.THROTTLED:
        return (
            f"{counts} -- sending {window.effective_limit} a day against a "
            f"configured {window.configured_limit}"
        )
    if health is CampaignHealth.SCALING:
        return (
            f"{counts} -- {window.reply_rate:.0%} reply rate with "
            f"{window.leads_available} lead(s) waiting"
        )
    return counts


__all__ = [
    "MIN_CONTACTED_TO_SCALE",
    "SCALING_REPLY_RATE",
    "CampaignHealth",
    "CampaignWindow",
    "classify",
    "explain",
]
