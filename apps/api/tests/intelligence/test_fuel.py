"""How much research to run, sized by the reserve rather than by today's sends.

Three places tied fuel production to the send budget: the research budget was
``min(remaining, ceiling)``, a spent budget ended the cycle before research was
planned, and the orchestrator skipped discovery on a ``BUDGET_SPENT`` verdict.

Together they cap intake at the send rate, so a buffer can never form. And since
only about a third of crawled sites yield an address, sustaining S sends a day
needs roughly 3S researched -- capping at S drains the pipeline rather than
holding it level. Measured live: 1,419 discovered, 189 with an address, **71
usable** against a 40-a-day target.
"""

from __future__ import annotations

import math

from titan.intelligence.fuel import (
    FALLBACK_EXTRACTION_RATE,
    MIN_EXTRACTION_SAMPLE,
    MIN_USABLE_EXTRACTION_RATE,
    RESERVE_DAYS,
    FuelState,
    research_budget,
    reserve_target,
    usable_rate,
)


def state(**overrides) -> FuelState:
    base = {
        "reachable_untouched": 71,
        "daily_send_capacity": 24,
        "extraction_rate": 0.33,
    }
    base.update(overrides)
    return FuelState(**base)


# ------------------------------------------------- the coupling that starved it


def test_the_budget_does_not_depend_on_sends_left_today() -> None:
    """The whole point. ``research_budget`` takes no argument for remaining
    sends, so the coupling cannot be reintroduced by accident."""
    import inspect

    params = set(inspect.signature(research_budget).parameters)

    assert "remaining" not in params
    assert params == {"state", "per_cycle_ceiling", "days"}


def test_a_spent_send_budget_still_orders_research() -> None:
    """A campaign that cannot send today still wants a warm pipeline for
    tomorrow -- which the authorization gate's own docstring already said."""
    assert research_budget(state(), per_cycle_ceiling=25).leads > 0


def test_research_outpaces_sending_rather_than_matching_it() -> None:
    """At a one-in-three extraction rate, researching as many leads as can be
    sent yields a third of what is needed. The budget has to exceed the send
    rate or the reserve can only ever fall."""
    budget = research_budget(state(reachable_untouched=0), per_cycle_ceiling=1000).leads

    assert budget > 24 * RESERVE_DAYS, "ordering only the shortfall ignores the yield"


# ----------------------------------------------------------------- the reserve


def test_a_full_reserve_orders_nothing() -> None:
    """The cost control. Without this the pipeline would crawl for ever."""
    budget = research_budget(state(reachable_untouched=10_000), per_cycle_ceiling=25)

    assert budget.leads == 0
    assert "reserve is covered" in budget.reason


def test_the_budget_is_bounded_per_cycle() -> None:
    """A deficit of thousands must not become thousands of crawls in one pass."""
    assert research_budget(state(reachable_untouched=0), per_cycle_ceiling=25).leads == 25


def test_the_reserve_scales_with_what_can_actually_be_sent() -> None:
    assert reserve_target(24) == 24 * RESERVE_DAYS
    assert reserve_target(50) > reserve_target(24)


def test_days_of_fuel_is_the_number_nobody_was_watching() -> None:
    assert state(reachable_untouched=71, daily_send_capacity=24).days_of_fuel < 3.0
    assert state(reachable_untouched=240, daily_send_capacity=24).days_of_fuel == 10.0


def test_no_capacity_is_not_an_emergency() -> None:
    """With nothing able to send, the reserve is never consumed. Reporting zero
    days would read as a fire when the question simply does not apply."""
    assert state(daily_send_capacity=0).days_of_fuel == math.inf


def test_no_capacity_still_keeps_the_pipeline_warm() -> None:
    """But it does not stop research altogether -- at a trickle, so a workspace
    whose mailboxes are all paused is not starting from nothing when they
    return."""
    budget = research_budget(state(daily_send_capacity=0), per_cycle_ceiling=25)

    assert budget.leads == 1


# ------------------------------------------------------- measuring, not guessing


def test_an_unmeasured_rate_is_not_a_zero_rate() -> None:
    """None means "too few crawls to say", which is a different thing from
    "measured and found nothing" and calls for the opposite response."""
    assert usable_rate(None) == FALLBACK_EXTRACTION_RATE
    assert usable_rate(None) > 0


def test_a_terrible_rate_cannot_demand_infinite_research() -> None:
    """A rate near zero would order a near-infinite amount of crawling for ever.
    Extraction being broken is a thing to fix, not to out-crawl."""
    assert usable_rate(0.0001) == MIN_USABLE_EXTRACTION_RATE


def test_a_good_measured_rate_is_used_as_measured() -> None:
    assert usable_rate(0.33) == 0.33


def test_the_sample_floor_is_high_enough_to_mean_something() -> None:
    """One lucky crawl in two is not a 50% rate."""
    assert MIN_EXTRACTION_SAMPLE >= 50


def test_a_worse_rate_orders_more_research() -> None:
    """The self-correction: the same reserve costs more crawls when fewer of
    them land."""
    poor = research_budget(state(extraction_rate=0.10), per_cycle_ceiling=10_000).leads
    good = research_budget(state(extraction_rate=0.50), per_cycle_ceiling=10_000).leads

    assert poor > good


# --------------------------------------------------------------- the safety bound


def test_the_reserve_drains_before_a_draft_can_expire() -> None:
    """Planted violation: raise RESERVE_DAYS above the approval TTL and this
    fails.

    This is the answer to the original objection to researching ahead -- that
    it manufactures drafts which quietly expire. It only does so if the tank
    holds more than the approval window can drain.
    """
    from titan.workflows.research import DEFAULT_APPROVAL_TTL

    assert RESERVE_DAYS < DEFAULT_APPROVAL_TTL.days


def test_the_budget_explains_itself() -> None:
    """It is written to the cycle's detail and is what an operator reads when
    asking why the crawler is busy."""
    reason = research_budget(state(), per_cycle_ceiling=25).reason

    assert "days of fuel" in reason
    assert "measured" in reason


# ----------------------------------------------------- fuel already on its way


def test_research_in_flight_counts_toward_the_reserve() -> None:
    """Planted violation: drop ``expected_from_in_flight`` from the deficit and
    this fails.

    Every campaign in the workspace plans in the same minute and sees the same
    shortfall. Without counting what is already being researched, twenty-three
    campaigns at a ceiling of twenty-five would have ordered 575 crawls to close
    a gap of 47 -- and the leads closing it were already in flight.
    """
    empty = research_budget(state(in_flight=0), per_cycle_ceiling=25).leads
    busy = research_budget(state(in_flight=423), per_cycle_ceiling=25).leads

    assert empty > 0
    assert busy == 0


def test_in_flight_is_discounted_by_the_yield_not_counted_whole() -> None:
    """400 leads being crawled are not 400 addresses. At a third, they are
    about 133, and treating them as 400 would stop research far too early."""
    s = state(extraction_rate=0.33, in_flight=400)

    assert s.expected_from_in_flight == 132


def test_a_lead_in_flight_is_worth_less_than_one_in_hand() -> None:
    """The tank still reports what it actually holds; only the *ordering*
    decision counts what is coming."""
    s = state(reachable_untouched=71, daily_send_capacity=24, in_flight=400)

    assert s.days_of_fuel < 3.0, "days of fuel must not be inflated by hopes"
    assert s.effective_supply > s.reachable_untouched
