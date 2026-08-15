"""What the campaign manager decides, given how a campaign is doing.

Pure. It reads a health verdict and returns proposals; it holds no session,
touches no row, and cannot apply anything itself. Applying is
:mod:`titan.autonomy.actuator`'s job and is bounded there, which means this
module can be wrong without being dangerous -- the worst a mistaken proposal
achieves is a clamped number and a row in the audit trail saying so.

**It moves one direction at a time.** A degrading campaign has its volume cut
*and* its lead bar raised, and both are the same intent: send less, to better
people. A recovering one gets volume back gradually and its bar returned in one
step, because a bar that is too high costs leads without protecting anything.

**Recovery is a return, never a raise.** Every proposal that increases volume
targets the human's configured number and stops there. There is no state in
which this module proposes more than a person approved.

**It proposes nothing it cannot justify.** LEARNING produces no proposals at
all: a campaign below the sample floor has no rates, and acting on the ones it
appears to have is how four bounces out of nine become a permanent throttle.
"""

from __future__ import annotations

from dataclasses import dataclass

from titan.autonomy.actuator import Actuation, Bounds, Proposal
from titan.autonomy.health import CampaignHealth, CampaignWindow, explain

#: Share of the configured limit a degrading campaign is cut to. A quarter,
#: matching the sender-level reduction in titan.delivery.adaptive_limits -- the
#: two are the same judgement about the same kind of trouble, and different
#: numbers would only invite an argument about which was right.
DEGRADED_LIMIT_RATIO = 0.25

#: How much of the gap to the ceiling a recovering campaign takes back per
#: cycle. Half: two or three cycles to full volume, which is slow enough that a
#: campaign that degrades again is caught before it is at full speed.
RECOVERY_FRACTION = 0.5

#: Added to the lead score bar when a campaign degrades. Small on purpose --
#: the actuator caps a single step anyway, and the intent is to be choosier
#: rather than to stop.
DEGRADED_SCORE_STEP = 5


@dataclass(frozen=True, slots=True)
class ManagedState:
    """What the manager currently has set, and what a human configured."""

    campaign_id: str
    bounds: Bounds
    managed_daily_limit: int | None = None
    managed_min_lead_score: int | None = None

    @property
    def current_limit(self) -> int:
        if self.managed_daily_limit is None:
            return self.bounds.configured_daily_limit
        return min(self.bounds.configured_daily_limit, self.managed_daily_limit)

    @property
    def current_score(self) -> int:
        if self.managed_min_lead_score is None:
            return self.bounds.configured_min_lead_score
        return max(self.bounds.configured_min_lead_score, self.managed_min_lead_score)


def confidence_for(window: CampaignWindow) -> float:
    """How much the evidence supports acting, 0 to 1.

    Sample size and nothing else. Recorded on every decision and acted on by
    none of them: a threshold here would be a second policy, unstated and
    interacting with the first in ways nobody had reasoned about.
    """
    from titan.delivery.deliverability import MIN_SAMPLE_FOR_RATES

    if not window.window.sent:
        return 0.0
    return min(1.0, window.window.sent / (MIN_SAMPLE_FOR_RATES * 4))


def plan(state: ManagedState, window: CampaignWindow) -> list[Proposal]:
    """Everything the manager wants to change about this campaign right now."""
    health = _health(window)
    reason = f"{health.value}: {explain(window, health)}"
    evidence = {
        "health": health.value,
        "sent": window.window.sent,
        "delivered": window.window.delivered,
        "bounced": window.window.hard_bounced,
        "complained": window.window.complained,
        "contacted": window.contacted,
        "replied": window.replied,
        "reply_rate": round(window.reply_rate, 4),
        "configured_limit": state.bounds.configured_daily_limit,
        "effective_limit": window.effective_limit,
        "leads_available": window.leads_available,
    }
    confidence = confidence_for(window)

    def propose(actuation: Actuation, current: int, proposed: int) -> Proposal:
        return Proposal(
            actuation=actuation,
            campaign_id=state.campaign_id,
            current=current,
            proposed=proposed,
            reason=reason,
            confidence=confidence,
            evidence=evidence,
        )

    # Nothing to decide. A paused campaign is somebody's decision already, and a
    # learning one has no rates to decide from.
    if health in (CampaignHealth.PAUSED, CampaignHealth.LEARNING):
        return []

    if health is CampaignHealth.DEGRADED:
        cut = max(
            state.bounds.floor_daily_limit,
            int(state.bounds.configured_daily_limit * DEGRADED_LIMIT_RATIO),
        )
        proposals = []
        if cut < state.current_limit:
            proposals.append(propose(Actuation.SET_DAILY_LIMIT, state.current_limit, cut))
        raised = state.current_score + DEGRADED_SCORE_STEP
        proposals.append(
            propose(Actuation.SET_MIN_LEAD_SCORE, state.current_score, raised)
        )
        return proposals

    # Everything below is a recovery. The campaign is behaving, so whatever the
    # manager did to it is walked back toward what a human configured.
    proposals = []

    ceiling = state.bounds.configured_daily_limit
    if state.current_limit < ceiling:
        gap = ceiling - state.current_limit
        step = max(1, int(gap * RECOVERY_FRACTION))
        proposals.append(
            propose(
                Actuation.SET_DAILY_LIMIT,
                state.current_limit,
                min(ceiling, state.current_limit + step),
            )
        )

    configured_score = state.bounds.configured_min_lead_score
    if state.current_score > configured_score:
        # In one step, unlike volume. A bar left too high costs qualified leads
        # and protects nothing, so there is no reason to walk it down slowly.
        proposals.append(
            propose(Actuation.SET_MIN_LEAD_SCORE, state.current_score, configured_score)
        )

    return proposals


def _health(window: CampaignWindow) -> CampaignHealth:
    from titan.autonomy.health import classify

    return classify(window)


__all__ = [
    "DEGRADED_LIMIT_RATIO",
    "DEGRADED_SCORE_STEP",
    "RECOVERY_FRACTION",
    "ManagedState",
    "confidence_for",
    "plan",
]
