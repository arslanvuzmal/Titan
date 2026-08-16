"""Market performance shading capacity, without being allowed to decide it.

Phase 06 asks for volume to move toward the markets that perform. The danger in
granting that is not that it moves too little -- it is that a mechanism which
reallocates on market performance can starve a market of the sending that would
have measured it, and then point at the absence of results as justification.

So the tests here are mostly about what it must *not* do: it must not act
without evidence, must not act on one market, must not override health, and must
not penalise a market for being new.
"""

from __future__ import annotations

from titan.autonomy.allocation import CampaignDemand, allocate
from titan.autonomy.health import CampaignHealth
from titan.autonomy.markets import (
    MAX_MULTIPLIER,
    MIN_MULTIPLIER,
    NEUTRAL,
    describe,
    multiplier_for,
    weigh,
)
from titan.db.enums import Region
from titan.delivery.deliverability import MIN_SAMPLE_FOR_RATES
from titan.intelligence.portfolio import RegionSlice, summarise


def market(region: Region, *, sent: int, replied: int) -> RegionSlice:
    return RegionSlice(
        region=region,
        campaigns=1,
        active_campaigns=1,
        leads=sent,
        contacted=sent,
        sent=sent,
        bounced=0,
        replied=replied,
    )


BIG = MIN_SAMPLE_FOR_RATES * 4


# ------------------------------------------------------------ silent until measured


def test_an_unmeasured_market_gets_no_opinion_not_a_low_score() -> None:
    """The asymmetry that matters most here.

    A market given a low multiplier for being unmeasured is starved of the very
    sending that would measure it, and stays unmeasured for as long as the
    system runs. That is a feedback loop, not a judgement.
    """
    book = summarise(
        [
            market(Region.UK, sent=BIG, replied=BIG // 10),
            market(Region.USA, sent=BIG, replied=0),
            market(Region.EUROPE, sent=MIN_SAMPLE_FOR_RATES - 1, replied=0),
        ]
    )

    weights = weigh(book)

    assert weights[Region.EUROPE].multiplier == NEUTRAL
    assert weights[Region.EUROPE].measured is False
    assert "not starved" in weights[Region.EUROPE].reason


def test_nothing_moves_until_two_markets_are_measured() -> None:
    """One market cannot be compared with anything.

    Comparing it against itself would hand it 1.0 by a longer route, and would
    make the module look like it had an opinion.
    """
    book = summarise(
        [
            market(Region.UK, sent=BIG, replied=BIG // 5),
            market(Region.USA, sent=MIN_SAMPLE_FOR_RATES - 1, replied=0),
        ]
    )

    weights = weigh(book)

    assert all(w.multiplier == NEUTRAL for w in weights.values())
    assert "nothing to compare" in weights[Region.UK].reason


def test_identical_markets_are_not_separated() -> None:
    book = summarise(
        [
            market(Region.UK, sent=BIG, replied=BIG // 10),
            market(Region.USA, sent=BIG, replied=BIG // 10),
        ]
    )

    assert all(w.multiplier == NEUTRAL for w in weigh(book).values())
    assert "none separated" in describe(weigh(book))


# ------------------------------------------------------------------- bounded


def test_the_best_market_cannot_run_away_with_everything() -> None:
    """Bounded on both sides, so a market shades the split and never inverts it.

    A wider band would let one good fortnight in one market empty the others.
    """
    book = summarise(
        [
            market(Region.UK, sent=BIG, replied=BIG // 2),
            market(Region.USA, sent=BIG, replied=0),
        ]
    )

    weights = weigh(book)

    assert weights[Region.UK].multiplier == MAX_MULTIPLIER
    assert weights[Region.USA].multiplier == MIN_MULTIPLIER
    assert MAX_MULTIPLIER / MIN_MULTIPLIER < 2.0, "one market could halve another"


def test_a_market_nobody_has_heard_of_is_not_penalised() -> None:
    """A campaign created since the window was computed must not be punished."""
    assert multiplier_for({}, Region.AUSTRALIA) == NEUTRAL


# --------------------------------------------------- health remains dominant


def test_a_good_market_cannot_revive_a_degraded_campaign() -> None:
    """Multiplied, not added: zero times anything is still zero.

    Being somewhere promising is not evidence that this campaign is well, and a
    manager that scaled a degrading campaign because its region was doing well
    would be scaling the thing that is producing the bounces.
    """
    degraded = CampaignDemand(
        campaign_id="a",
        health=CampaignHealth.DEGRADED,
        configured_limit=50,
        leads_available=100,
        market_multiplier=MAX_MULTIPLIER,
    )

    assert degraded.effective_weight == 0.0
    assert degraded.wants_more is False


def test_a_strong_market_shifts_volume_between_equally_healthy_campaigns() -> None:
    """The feature itself. Two Healthy campaigns, different markets."""
    good = CampaignDemand(
        campaign_id="good-market",
        health=CampaignHealth.HEALTHY,
        configured_limit=100,
        leads_available=500,
        market_multiplier=MAX_MULTIPLIER,
    )
    poor = CampaignDemand(
        campaign_id="poor-market",
        health=CampaignHealth.HEALTHY,
        configured_limit=100,
        leads_available=500,
        market_multiplier=MIN_MULTIPLIER,
    )

    split = allocate([good, poor], workspace_limit=100)

    assert split.per_campaign["good-market"] > split.per_campaign["poor-market"]
    assert sum(split.per_campaign.values()) <= 100


def test_without_market_evidence_the_split_is_unchanged() -> None:
    """Inert by default. The multiplier defaults to 1.0, so a workspace with no
    measured markets allocates exactly as it did before this existed."""
    a = CampaignDemand(
        campaign_id="a",
        health=CampaignHealth.HEALTHY,
        configured_limit=100,
        leads_available=500,
    )
    b = CampaignDemand(
        campaign_id="b",
        health=CampaignHealth.HEALTHY,
        configured_limit=100,
        leads_available=500,
    )

    split = allocate([a, b], workspace_limit=40)

    assert split.per_campaign["a"] == split.per_campaign["b"]


def test_the_human_ceiling_still_binds() -> None:
    """The bound that predates all of this. A market multiplier is not a licence
    to exceed the number a person configured."""
    demand = CampaignDemand(
        campaign_id="a",
        health=CampaignHealth.SCALING,
        configured_limit=5,
        leads_available=500,
        market_multiplier=MAX_MULTIPLIER,
    )

    split = allocate([demand], workspace_limit=100)

    assert split.per_campaign["a"] <= 5
