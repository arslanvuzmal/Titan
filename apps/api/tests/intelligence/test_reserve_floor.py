"""An absolute reserve target, for measuring research on its own.

The reserve is normally derived: ``send_capacity x RESERVE_DAYS``. That
coupling is deliberate and correct for running the estate -- research exists to
feed sending, and a reserve is only worth what it will be used for.

It makes one question unanswerable, though: *how much can the research side
actually do?* At 100 sends a day the target is 500, the estate holds 1,898
reachable leads, and the budget is zero at every send rate you can set. You
cannot measure the crawler's throughput by asking the thing that is throttling
it.

`floor` answers that and nothing else. The tests below pin both halves: it
raises the target when it is larger, and it is inert when it is not -- because
the failure that would matter is a floor left set by accident, quietly holding
a reserve above the staleness horizon for ever.

The horizon is the reason this is not a feature to leave switched on:
``STALE_AFTER`` returns a lead for re-measurement at 21 days and the send gate
refuses evidence outright at 30, so a reserve above roughly ``21 * send_rate``
is a set of leads that will be crawled twice and mailed once.
"""

from __future__ import annotations

import pytest
from titan.intelligence.fuel import RESERVE_DAYS, reserve_target


def test_unset_floor_leaves_the_derived_target_alone() -> None:
    """The default must be exactly the old behaviour."""
    assert reserve_target(100) == 100 * RESERVE_DAYS
    assert reserve_target(100, floor=0) == 100 * RESERVE_DAYS


@pytest.mark.parametrize("floor", [1, 100, 499])
def test_a_floor_below_the_derived_target_is_inert(floor: int) -> None:
    """Raising the target is the only thing it may do.

    A floor that could *lower* the reserve would be a way to starve the
    pipeline through a setting nobody associates with sending.
    """
    assert reserve_target(100, floor=floor) == 500


def test_a_floor_above_the_derived_target_wins() -> None:
    assert reserve_target(100, floor=3000) == 3000


def test_a_floor_applies_when_nothing_can_send() -> None:
    """Zero send capacity derives a target of zero.

    This is the case the floor exists for -- measuring research with sending
    switched off entirely -- so it must survive it.
    """
    assert reserve_target(0) == 0
    assert reserve_target(0, floor=2000) == 2000


def test_a_negative_floor_is_ignored_rather_than_subtracted() -> None:
    assert reserve_target(100, floor=-5000) == 500


def test_the_budget_reason_says_when_a_floor_is_doing_the_work() -> None:
    """A reserve held by a floor must not read as a reserve held by demand.

    Someone reading "12.7 days of fuel against a target of 3000" needs to know
    that 3000 came from a setting, not from what the estate can send -- it is
    the difference between a healthy pipeline and one propped open by a flag
    left on after a measurement.
    """
    from titan.intelligence.fuel import FuelState, research_budget

    state = FuelState(
        reachable_untouched=100,
        in_flight=0,
        extraction_rate=0.5,
        daily_send_capacity=100,
        crawl_rate_per_hour=90,
    )
    with_floor = research_budget(state, per_cycle_ceiling=25, floor=3000)
    without = research_budget(state, per_cycle_ceiling=25)

    assert "floor" in with_floor.reason
    assert "floor" not in without.reason
