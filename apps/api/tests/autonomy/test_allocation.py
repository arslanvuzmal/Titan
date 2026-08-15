"""Dividing a workspace's daily sending between competing campaigns.

The live database is the reason this exists: twenty active campaigns holding a
hundred sends a day between them against a workspace cap of five. The cap was
always enforced, so the first campaign the outbox worker claimed from took the
lot -- by claim order, not by merit.

Two hard bounds, and the tests that matter are the ones that hold them: no
campaign above its own configured limit, and the total never above the
workspace's. Both are the human's numbers.
"""

from __future__ import annotations

import pytest
from titan.autonomy.allocation import (
    EXPLORATION_FLOOR,
    HEALTH_WEIGHT,
    CampaignDemand,
    allocate,
    explain,
)
from titan.autonomy.health import CampaignHealth

H = CampaignHealth


def demand(
    name: str,
    health: CampaignHealth = H.HEALTHY,
    *,
    ceiling: int = 100,
    leads: int = 50,
) -> CampaignDemand:
    return CampaignDemand(
        campaign_id=name,
        health=health,
        configured_limit=ceiling,
        leads_available=leads,
    )


# ==========================================================================
# The two hard bounds
# ==========================================================================
@pytest.mark.parametrize("workspace_limit", [0, 1, 5, 50, 500])
def test_the_total_never_exceeds_the_workspace_limit(workspace_limit: int) -> None:
    result = allocate([demand(f"c{i}", H.HEALTHY) for i in range(20)], workspace_limit)
    assert result.total <= workspace_limit


def test_no_campaign_exceeds_its_own_ceiling() -> None:
    """Even when there is capacity going spare and it is the only campaign."""
    result = allocate([demand("small", ceiling=3)], workspace_limit=500)

    assert result.per_campaign["small"] == 3
    assert result.unallocated == 497


def test_the_live_shape_divides_instead_of_racing() -> None:
    """Twenty campaigns configured for a hundred, a workspace allowed five.

    Before this, one campaign took all five and nineteen got nothing.
    """
    demands = [demand(f"c{i}", H.HEALTHY, ceiling=5) for i in range(20)]
    result = allocate(demands, workspace_limit=5)

    assert result.total == 5
    served = [c for c, n in result.per_campaign.items() if n > 0]
    assert len(served) > 1, "one campaign took the whole allowance again"


# ==========================================================================
# Capacity follows health
# ==========================================================================
def test_a_scaling_campaign_outranks_a_healthy_one() -> None:
    result = allocate([demand("good", H.SCALING), demand("ok", H.HEALTHY)], 100)

    assert result.per_campaign["good"] > result.per_campaign["ok"]


def test_a_degraded_campaign_gets_a_floor_and_no_more() -> None:
    """It is bouncing its mail. Anything above the floor would have it competing
    for the capacity the healthy campaigns earned."""
    result = allocate([demand("bad", H.DEGRADED), demand("ok", H.HEALTHY)], 100)

    assert result.per_campaign["bad"] == EXPLORATION_FLOOR
    assert result.per_campaign["ok"] > result.per_campaign["bad"]


def test_a_paused_campaign_gets_nothing_at_all() -> None:
    """Not even a floor. There is nothing to learn from a campaign nobody has
    switched on."""
    result = allocate([demand("off", H.PAUSED), demand("on", H.HEALTHY)], 50)

    assert result.per_campaign["off"] == 0
    assert result.per_campaign["on"] == 50


def test_health_is_the_only_performance_input() -> None:
    """SCALING already means safe, replying and with leads waiting. Reading the
    reply rate again here would be the same evidence counted twice, in two
    places free to disagree about what good looks like."""
    assert HEALTH_WEIGHT[H.SCALING] > HEALTH_WEIGHT[H.HEALTHY]
    assert HEALTH_WEIGHT[H.DEGRADED] == 0
    assert HEALTH_WEIGHT[H.PAUSED] == 0
    assert set(HEALTH_WEIGHT) == set(CampaignHealth)


# ==========================================================================
# The trap the exploration floor exists to hold open
# ==========================================================================
def test_a_learning_campaign_is_never_starved() -> None:
    """No history means no reply rate, which on a purely performance-weighted
    split means no capacity, which means it never sends, which means it never
    acquires the history. That trap closes silently and permanently."""
    result = allocate(
        [demand("new", H.LEARNING), *[demand(f"est{i}", H.SCALING) for i in range(5)]],
        workspace_limit=60,
    )

    assert result.per_campaign["new"] >= EXPLORATION_FLOOR


def test_floors_go_to_the_neediest_when_there_is_not_enough() -> None:
    """Learning first: the state that most needs evidence and has least ability
    to earn any without help."""
    result = allocate(
        [demand("new", H.LEARNING), demand("bad", H.DEGRADED)], workspace_limit=2
    )

    assert result.per_campaign["new"] == 2
    assert result.per_campaign["bad"] == 0


# ==========================================================================
# Capacity nobody can use
# ==========================================================================
def test_a_campaign_with_no_leads_gets_nothing() -> None:
    """Capacity given to a campaign with none is capacity nobody sends, taken
    from a campaign that would have."""
    result = allocate([demand("dry", H.SCALING, leads=0), demand("wet", H.HEALTHY)], 40)

    assert result.per_campaign["dry"] == 0
    assert result.per_campaign["wet"] == 40


def test_unused_capacity_is_reported_rather_than_hidden() -> None:
    """It means discovery is the bottleneck, not sending -- which is a different
    problem and a different fix."""
    result = allocate([demand("only", ceiling=10)], workspace_limit=100)

    assert result.total == 10
    assert result.unallocated == 90
    assert "no qualified leads" not in explain(demand("only", ceiling=10), result)


# ==========================================================================
# Stability
# ==========================================================================
def test_the_same_inputs_give_the_same_division() -> None:
    """A reallocation that shuffled every cycle would churn every campaign's
    volume for no reason and make the audit trail unreadable."""
    demands = [demand("a", H.SCALING), demand("b", H.HEALTHY), demand("c", H.LEARNING)]

    first = allocate(demands, 37)
    second = allocate(list(reversed(demands)), 37)

    assert first.per_campaign == second.per_campaign


def test_nothing_to_allocate_is_not_an_error() -> None:
    assert allocate([], 50).total == 0
    assert allocate([demand("a")], 0).total == 0


# ==========================================================================
# Explanations
# ==========================================================================
def test_every_outcome_explains_itself() -> None:
    demands = [
        demand("scaling", H.SCALING),
        demand("degraded", H.DEGRADED),
        demand("paused", H.PAUSED),
        demand("dry", H.HEALTHY, leads=0),
        demand("capped", H.HEALTHY, ceiling=2),
    ]
    result = allocate(demands, 40)

    for d in demands:
        line = explain(d, result)
        assert line
        assert "None" not in line


def test_a_starved_campaign_says_why() -> None:
    demands = [demand("a", H.HEALTHY), demand("b", H.HEALTHY)]
    result = allocate(demands, workspace_limit=2)
    starved = [d for d in demands if result.per_campaign[d.campaign_id] == 0]

    for d in starved:
        assert "exhausted" in explain(d, result) or "ceiling" in explain(d, result)
