"""How much research to run, sized by the reserve rather than by today's sends.

Three separate places tied fuel production to the send budget:

* ``budget = min(remaining, max_new_research)`` throttled research to the sends
  left today;
* ``if remaining == 0`` ended the cycle before research was planned at all;
* and the orchestrator skipped discovery entirely on a ``BUDGET_SPENT`` verdict.

Each is defensible alone. Together they mean the intake rate can never exceed
the send rate, so a buffer can never form -- and when the ramp cut both
mailboxes to 12 a day on a bounce rate, the whole supply of new leads collapsed
with it. A delivery problem became a discovery problem.

The arithmetic makes it worse than a standstill. Only about a third of crawled
sites yield an address, so sustaining *S* sends a day needs roughly *3S* leads
researched a day. Capping research at *S* does not hold the pipeline level; it
drains it. Measured on the live workspace: 1,419 leads discovered, 199 with an
address, **82 untouched** against a 40-a-day target.

Two comments in the repository already said this should not be so. The research
budget is documented as "a ceiling on children started this cycle, *independent
of the send budget*", and the authorization gate says outright that a campaign
"whose sending is paused may still legitimately want its pipeline warm for when
it resumes". The intent was written down twice and implemented neither time.

**So fuel is sized by the reserve.** Research runs until the stock of reachable,
unwritten-to leads covers a few days of sending, and then stops. That fills the
tank, stops paying to overfill it, and self-corrects as the extraction rate
moves -- and it produces the days-of-fuel number that nothing was measuring
while the tank ran dry.

**The reserve is a workspace question, not a campaign one.** Twenty-three
campaigns each trying to fill the whole reserve would buy twenty-three times the
research needed. Each cycle asks how short the *workspace* is and takes at most
its own ceiling of that, so the deficit shrinks as it is filled and later cycles
find less to do.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.enums import LeadStatus
from titan.db.models import (
    Contact,
    ContactChannel,
    CrawlRun,
    Lead,
    Message,
    SuppressionEntry,
)
from titan.db.models.research import ResearchRun

#: Days of sending to keep in reserve.
#:
#: Not a week: research goes stale, and evidence gathered today is weaker by the
#: time a message quoting it goes out. Not one day either -- that is the policy
#: that produced the shortage, since a day's reserve consumed in a day leaves
#: nothing while the next batch is still crawling. Five is roughly the window in
#: which a site audit still describes the site.
RESERVE_DAYS = 5

#: Below this many measured crawls, the extraction rate means nothing and the
#: fallback is used instead. One lucky crawl in two is not a 50% rate.
MIN_EXTRACTION_SAMPLE = 50

#: What share of crawled sites yield a usable address, before there is enough
#: evidence to say. Measured at 32% on the live workspace (199 of 617); the
#: fallback is deliberately a little lower, because over-estimating the rate
#: under-orders research and starving is the failure being fixed.
FALLBACK_EXTRACTION_RATE = 0.25

#: Never divide by a rate below this, however bad the measurement.
#:
#: A rate near zero would demand a near-infinite amount of research to fill the
#: reserve, and the per-cycle ceiling would then be hit every cycle for ever --
#: spending the entire budget on a pipeline whose real problem is that
#: extraction is broken, which is a thing to fix rather than to out-crawl.
MIN_USABLE_EXTRACTION_RATE = 0.05


@dataclass(frozen=True, slots=True)
class FuelState:
    """What the tank holds, and how fast it empties."""

    #: Leads with a usable address that have never been written to.
    reachable_untouched: int
    #: What the mailboxes are allowed to send today, in total.
    daily_send_capacity: int
    #: Share of crawled sites that yielded an address, or None when too few
    #: crawls have been measured to say. None is *not* zero: "not measured" and
    #: "measured and found nothing" call for opposite responses.
    extraction_rate: float | None
    #: Leads already being researched. Fuel that is on its way but has not
    #: arrived, and the reason ordering more would be waste.
    in_flight: int = 0

    @property
    def expected_from_in_flight(self) -> int:
        """How many addresses the research already running should produce.

        Without this every campaign in the workspace sees the same shortfall in
        the same minute and orders the whole of it. Twenty-three campaigns at a
        ceiling of twenty-five would have bought 575 crawls to close a gap of
        47 -- and the leads doing the closing were already in flight.
        """
        return math.floor(self.in_flight * usable_rate(self.extraction_rate))

    @property
    def effective_supply(self) -> int:
        """Fuel in the tank, plus fuel on its way to it."""
        return self.reachable_untouched + self.expected_from_in_flight

    @property
    def days_of_fuel(self) -> float:
        """How many days of sending the reserve covers.

        Infinite when nothing can send: with no capacity the reserve is never
        consumed, and reporting zero days would read as an emergency when the
        truth is that the question does not apply.
        """
        if self.daily_send_capacity <= 0:
            return math.inf
        return self.reachable_untouched / self.daily_send_capacity


@dataclass(frozen=True, slots=True)
class FuelBudget:
    """How much research to start, and the reasoning, for the audit trail."""

    leads: int
    reason: str


def usable_rate(measured: float | None) -> float:
    """The extraction rate to plan with."""
    if measured is None:
        return FALLBACK_EXTRACTION_RATE
    return max(measured, MIN_USABLE_EXTRACTION_RATE)


def reserve_target(daily_send_capacity: int, *, days: int = RESERVE_DAYS) -> int:
    """How many reachable leads the workspace should be holding."""
    return max(0, daily_send_capacity) * days


def research_budget(
    state: FuelState, *, per_cycle_ceiling: int, days: int = RESERVE_DAYS
) -> FuelBudget:
    """How many leads to research this cycle.

    Deliberately takes no argument about how many sends are left today. That
    coupling is the thing this module exists to remove.
    """
    if per_cycle_ceiling <= 0:
        return FuelBudget(0, "the cycle allows no new research")

    target = reserve_target(state.daily_send_capacity, days=days)
    if target == 0:
        # Nothing can send, so nothing is being consumed. Research anyway, at a
        # trickle: a workspace whose mailboxes are all paused still wants a warm
        # pipeline for when they come back, and that is exactly what the
        # authorization gate already says out loud.
        return FuelBudget(
            min(per_cycle_ceiling, 1),
            "no send capacity; keeping the pipeline warm at a trickle",
        )

    deficit = target - state.effective_supply
    if deficit <= 0:
        return FuelBudget(
            0,
            f"reserve is covered: {state.reachable_untouched} reachable plus "
            f"{state.expected_from_in_flight} expected from {state.in_flight} "
            f"in flight, against a target of {target}",
        )

    rate = usable_rate(state.extraction_rate)
    needed = math.ceil(deficit / rate)
    measured = (
        f"{state.extraction_rate:.0%} measured"
        if state.extraction_rate is not None
        else f"{rate:.0%} assumed, too few crawls to measure"
    )
    return FuelBudget(
        min(needed, per_cycle_ceiling),
        f"{state.days_of_fuel:.1f} days of fuel against a {days}-day target; "
        f"{deficit} short after counting {state.in_flight} in flight, "
        f"{needed} to research at {measured}",
    )


# ==========================================================================
# Reading the state
# ==========================================================================
def _reachable_untouched_query(workspace_id: uuid.UUID) -> Select[tuple[int]]:
    """Leads holding an address, not suppressed, never yet written to.

    All three conditions are the difference between a lead and a *usable* lead.
    Counting leads with an address alone was how 199 looked like enough when 117
    of them had already been contacted and only 82 could actually be worked.
    """
    has_email = (
        select(ContactChannel.id)
        .join(Contact, Contact.id == ContactChannel.contact_id)
        .where(
            Contact.organization_id == Lead.organization_id,
            ContactChannel.channel_type == "email",
            ~select(SuppressionEntry.id)
            .where(
                SuppressionEntry.workspace_id == workspace_id,
                SuppressionEntry.normalized_value == ContactChannel.normalized_value,
            )
            .exists(),
        )
        .exists()
    )
    already_written = select(Message.id).where(Message.lead_id == Lead.id).exists()
    return (
        select(func.count())
        .select_from(Lead)
        .where(Lead.workspace_id == workspace_id, has_email, ~already_written)
    )


def _extraction_rate_query(workspace_id: uuid.UUID) -> Select[tuple[int, int]]:
    """Crawled leads, and how many of them yielded an address.

    Measured over leads that were actually *crawled*, not over every lead ever
    discovered. A lead still waiting its turn has not failed to produce an
    address; it has not been asked yet, and counting it as a failure would drag
    the rate down and over-order research to compensate.
    """
    crawled = (
        select(CrawlRun.id)
        .join(ResearchRun, ResearchRun.id == CrawlRun.research_run_id)
        .where(ResearchRun.lead_id == Lead.id)
        .exists()
    )
    has_email = (
        select(ContactChannel.id)
        .join(Contact, Contact.id == ContactChannel.contact_id)
        .where(
            Contact.organization_id == Lead.organization_id,
            ContactChannel.channel_type == "email",
        )
        .exists()
    )
    return (
        select(
            func.count(),
            func.count().filter(has_email),
        )
        .select_from(Lead)
        .where(Lead.workspace_id == workspace_id, crawled)
    )


async def measure_extraction_rate(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> float | None:
    """Share of crawled leads that yielded an address, or None below the floor.

    None rather than a number, deliberately. A rate computed from nine crawls
    is noise, and planning research volume from noise is how a single unlucky
    afternoon turns into a week of over-ordering.
    """
    crawled, with_email = (
        await session.execute(_extraction_rate_query(workspace_id))
    ).one()
    if int(crawled) < MIN_EXTRACTION_SAMPLE:
        return None
    return int(with_email) / int(crawled)


async def read_fuel_state(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    daily_send_capacity: int,
    extraction_rate: float | None,
) -> FuelState:
    """Count the reserve. The rate is passed in, since it is measured elsewhere."""
    reachable = (
        await session.execute(_reachable_untouched_query(workspace_id))
    ).scalar_one()
    in_flight = (
        await session.execute(
            select(func.count())
            .select_from(Lead)
            .where(
                Lead.workspace_id == workspace_id,
                Lead.status == LeadStatus.RESEARCHING,
            )
        )
    ).scalar_one()
    return FuelState(
        reachable_untouched=int(reachable),
        daily_send_capacity=daily_send_capacity,
        extraction_rate=extraction_rate,
        in_flight=int(in_flight),
    )


__all__ = [
    "FALLBACK_EXTRACTION_RATE",
    "MIN_EXTRACTION_SAMPLE",
    "MIN_USABLE_EXTRACTION_RATE",
    "RESERVE_DAYS",
    "FuelBudget",
    "FuelState",
    "measure_extraction_rate",
    "read_fuel_state",
    "research_budget",
    "reserve_target",
    "usable_rate",
]
