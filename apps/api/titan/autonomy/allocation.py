"""Dividing a workspace's daily sending between the campaigns competing for it.

Campaign limits were never a division of anything. Each campaign has its own,
and their sum is not checked against the workspace's -- in the live database
twenty active campaigns hold a hundred sends a day between them against a
workspace cap of five. The workspace cap is real and enforced at send time, so
what actually happened was that the first campaign the outbox worker claimed
from consumed the whole allowance and the other nineteen got nothing.

Not by merit. By claim order.

So this apportions the scarce number instead of leaving it to a race, and the
two hard bounds are the ones that already existed: no campaign exceeds its own
configured limit, and the total never exceeds the workspace's. Both are the
human's numbers, and the allocator can only ever divide what they permit.

**Capacity follows health, and health already encodes performance.** The weights
below are keyed on ``CampaignHealth`` rather than on reply rates directly,
because SCALING already means "safe, replying, and with leads waiting" -- reading
the reply rate again here would be the same evidence counted twice, in two places
free to disagree about what good looks like.

**A learning campaign is guaranteed a share.** A campaign with no history has no
reply rate, so a purely performance-weighted split starves it, which means it
never sends, which means it never acquires the history that would earn it
capacity. That trap closes silently and permanently, and the exploration floor
is what holds it open.

**Apportionment, not rounding.** Sends are indivisible and shares rarely divide
evenly, so this uses the highest-averages method -- each unit goes to whichever
campaign has the largest weight per unit already held. Proportional rounding
would need a tie-break and a correction pass for the remainder, and would still
hand out a different total than it was given.
"""

from __future__ import annotations

from dataclasses import dataclass

from titan.autonomy.health import CampaignHealth

#: Relative claim on spare capacity, by health.
#:
#: DEGRADED is zero rather than small: a campaign bouncing its mail should be
#: getting *less*, and anything above zero here would have it competing for the
#: capacity the healthy campaigns earned. It still receives its exploration
#: floor, because a campaign cut to nothing produces no evidence that it has
#: recovered.
#:
#: PAUSED is zero and gets no floor either. There is nothing to learn from a
#: campaign nobody has switched on.
HEALTH_WEIGHT: dict[CampaignHealth, int] = {
    CampaignHealth.PAUSED: 0,
    CampaignHealth.DEGRADED: 0,
    CampaignHealth.LEARNING: 1,
    CampaignHealth.THROTTLED: 1,
    CampaignHealth.HEALTHY: 2,
    CampaignHealth.SCALING: 3,
}

#: Sends a day a campaign keeps regardless of its weight, so that it continues
#: to produce the outcomes its next assessment needs. Small: it is a trickle to
#: keep evidence arriving, not a share of the capacity.
EXPLORATION_FLOOR = 2

#: Health states that receive an exploration floor at all.
_FLOOR_ELIGIBLE = frozenset(
    {
        CampaignHealth.LEARNING,
        CampaignHealth.DEGRADED,
        CampaignHealth.THROTTLED,
        CampaignHealth.HEALTHY,
        CampaignHealth.SCALING,
    }
)

#: The order floors are handed out in when there is not enough to go round.
#: Learning first: it is the state that most needs evidence and the one with
#: least ability to earn any without help.
_FLOOR_PRIORITY: tuple[CampaignHealth, ...] = (
    CampaignHealth.LEARNING,
    CampaignHealth.SCALING,
    CampaignHealth.HEALTHY,
    CampaignHealth.THROTTLED,
    CampaignHealth.DEGRADED,
)


@dataclass(frozen=True, slots=True)
class CampaignDemand:
    """What one campaign could use, and what it has earned."""

    campaign_id: str
    health: CampaignHealth
    #: The human's ceiling for this campaign. Never exceeded.
    configured_limit: int
    #: Qualified leads waiting. Capacity given to a campaign with none is
    #: capacity nobody sends, taken from a campaign that would have.
    leads_available: int = 0

    @property
    def weight(self) -> int:
        return HEALTH_WEIGHT.get(self.health, 0)

    @property
    def wants_more(self) -> bool:
        return self.weight > 0 and self.leads_available > 0

    @property
    def takes_a_floor(self) -> bool:
        return self.health in _FLOOR_ELIGIBLE and self.leads_available > 0


@dataclass(frozen=True, slots=True)
class Allocation:
    """The division, and enough to explain any one campaign's share."""

    per_campaign: dict[str, int]
    workspace_limit: int
    #: Capacity nobody could use: every campaign either at its ceiling or with
    #: no leads. Worth surfacing rather than hiding -- it means discovery is the
    #: bottleneck, not sending.
    unallocated: int = 0

    @property
    def total(self) -> int:
        return sum(self.per_campaign.values())

    def share_of(self, campaign_id: str) -> float:
        if not self.total:
            return 0.0
        return self.per_campaign.get(campaign_id, 0) / self.total


def allocate(demands: list[CampaignDemand], workspace_limit: int) -> Allocation:
    """Divide ``workspace_limit`` between the campaigns that can use it.

    Deterministic: given the same demands and the same limit it returns the same
    division every time, with ties broken by campaign id. A reallocation that
    shuffled on every cycle would churn every campaign's volume for no reason
    and make the audit trail unreadable.
    """
    allocated = {d.campaign_id: 0 for d in demands}
    remaining = max(0, workspace_limit)

    # ---- exploration floors, worst-served first -------------------------
    by_priority = sorted(
        (d for d in demands if d.takes_a_floor),
        key=lambda d: (_FLOOR_PRIORITY.index(d.health), d.campaign_id),
    )
    for demand in by_priority:
        if remaining <= 0:
            break
        floor = min(EXPLORATION_FLOOR, demand.configured_limit, remaining)
        allocated[demand.campaign_id] = floor
        remaining -= floor

    # ---- the rest, by highest averages ----------------------------------
    # One send at a time to whichever campaign has the greatest weight per unit
    # it already holds. Exact, order-independent, and it can never hand out more
    # than it was given -- which proportional rounding, on integers, can.
    candidates = [d for d in demands if d.wants_more]
    while remaining > 0 and candidates:
        eligible = [
            d for d in candidates if allocated[d.campaign_id] < d.configured_limit
        ]
        if not eligible:
            break
        winner = max(
            eligible,
            key=lambda d: (d.weight / (allocated[d.campaign_id] + 1), d.campaign_id),
        )
        allocated[winner.campaign_id] += 1
        remaining -= 1

    return Allocation(
        per_campaign=allocated,
        workspace_limit=workspace_limit,
        unallocated=remaining,
    )


def explain(demand: CampaignDemand, allocation: Allocation) -> str:
    """Why this campaign got what it got."""
    given = allocation.per_campaign.get(demand.campaign_id, 0)
    if given == 0:
        if demand.health is CampaignHealth.PAUSED:
            return "no capacity: campaign is not active"
        if demand.leads_available == 0:
            return "no capacity: no qualified leads waiting"
        return (
            "no capacity: the workspace limit was exhausted by higher-weighted campaigns"
        )

    share = allocation.share_of(demand.campaign_id)
    detail = (
        f"{given} of {allocation.workspace_limit} workspace sends "
        f"({share:.0%} of what was allocated), health {demand.health.value}"
    )
    if given >= demand.configured_limit:
        return f"{detail} -- at its own configured ceiling"
    if given <= EXPLORATION_FLOOR and demand.weight == 0:
        return f"{detail} -- exploration floor only, so it keeps producing evidence"
    return detail


__all__ = [
    "EXPLORATION_FLOOR",
    "HEALTH_WEIGHT",
    "Allocation",
    "CampaignDemand",
    "allocate",
    "explain",
]
