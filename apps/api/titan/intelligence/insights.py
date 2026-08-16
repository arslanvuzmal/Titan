"""Connecting the judgements to the data they were written to judge.

Three modules in this codebase decide something worth knowing and are imported
by nothing: :mod:`titan.intelligence.timing` ranks the hours of a recipient's
week, :mod:`titan.autonomy.experiments` decides whether one phrasing actually
beat another, and :mod:`titan.intelligence.portfolio` reads six markets as one
object. All three are pure functions over dataclasses, all three are tested, and
none of them has ever been called outside a test.

That was defensible when they were written. ``timing`` says so in its own
docstring -- the column it reads was added in the same change, so there was no
history to read and would not be for weeks. **There is now**: reconciling
Smartlead's sends gave every real message a local weekday and hour, so the
precondition that justified the deferral no longer holds.

What was missing in each case is the same thing: something to turn rows into the
dataclasses the judgement takes. That is this module, and it deliberately does
not re-query. :func:`titan.intelligence.rollups.outcomes_by` already groups
delivery outcomes by local slot, by variant and by campaign, with one definition
of "sent" and one sample floor. A second set of queries here would be a second
definition, free to disagree with the first about what a bounce is -- and the
disagreement would surface as two screens showing different numbers for the same
week.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from titan.autonomy import experiments
from titan.db.enums import Region
from titan.db.session import WORKSPACE_KEY
from titan.intelligence import portfolio, timing
from titan.intelligence.rollups import (
    DEFAULT_WINDOW_DAYS,
    Dimension,
    outcomes_by,
)


async def timing_report(
    session: AsyncSession,
    *,
    now: dt.datetime,
    window_days: int = DEFAULT_WINDOW_DAYS,
    campaign_id: uuid.UUID | None = None,
) -> timing.TimingReport:
    """Which hours of the recipient's week are worth writing in.

    Slots come from the local-slot rollup, so "sent" here means exactly what it
    means on every other screen.

    Slots with no resolved clock are already excluded upstream, which matters:
    bucketing them into a null slot would let every message whose timezone could
    not be worked out vote on what time of day is best.
    """
    slices = await outcomes_by(
        session,
        Dimension.LOCAL_SLOT,
        now=now,
        window_days=window_days,
        campaign_id=campaign_id,
    )
    outcomes = []
    for each in slices:
        weekday, _, hour = each.key.partition("-")
        try:
            slot = timing.Slot(weekday=int(weekday), hour=int(hour))
        except ValueError:
            continue
        outcomes.append(
            timing.SlotOutcome(slot=slot, sent=each.sent, replied=each.replied)
        )
    return timing.learn(outcomes)


async def variant_comparison(
    session: AsyncSession,
    *,
    now: dt.datetime,
    window_days: int = DEFAULT_WINDOW_DAYS,
    campaign_id: uuid.UUID | None = None,
) -> experiments.Comparison | None:
    """Whether one phrasing actually beat another, or merely differed.

    ``positive_replies`` is what the arms are judged on, not ``replied``.
    Testing raw replies rewards whichever wording provokes the most answers of
    any kind, and the easiest way to provoke an answer is to annoy somebody.

    Returns None when there is nothing to compare -- one arm, or every arm below
    its floor. None is the honest answer to "which won"; a winner named out of
    two arms with nine sends each would be noise with a p-value attached.
    """
    slices = await outcomes_by(
        session,
        Dimension.VARIANT,
        now=now,
        window_days=window_days,
        campaign_id=campaign_id,
    )
    arms = [
        experiments.Arm(
            key=each.label,
            sent=each.sent,
            replied=each.replied,
            positive_replies=each.positive_replies,
        )
        for each in slices
    ]
    if len(arms) < 2:
        return None
    return experiments.best_against_control(arms)


#: The markets outreach is actually aimed at. ``OTHER`` and ``UNSPECIFIED`` are
#: excluded deliberately: one means "somewhere else", the other means "nobody
#: said", and neither is a market capacity could be moved into.
REAL_REGIONS: tuple[Region, ...] = (
    Region.USA,
    Region.CANADA,
    Region.UK,
    Region.EUROPE,
    Region.AUSTRALIA,
    Region.MIDDLE_EAST,
)


def unconfigured_markets(book: portfolio.Portfolio) -> tuple[Region, ...]:
    """Markets with no campaign at all, in declaration order.

    Reported beside the portfolio rather than folded into it as zero-filled
    slices. A market nobody has configured has no delivery record, and giving it
    a row of zeros would let it be sorted and compared against markets that have
    one -- "0% bounced" for a market that has never sent reads as the healthiest
    row in the table.

    This is what makes the view the *six* markets rather than only the occupied
    corners of it: capacity can only be reallocated toward somewhere you can see.
    """
    present = {slice_.region for slice_ in book.slices}
    return tuple(region for region in REAL_REGIONS if region not in present)


async def portfolio_view(
    session: AsyncSession,
    *,
    now: dt.datetime,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> portfolio.Portfolio:
    """The six markets as one object, busiest first.

    Grouped by the campaign's declared region rather than by the recipient's
    country. The region is what set the working hours and the sending calendar,
    so it is the unit the operator configured and the unit any reallocation
    would move capacity between. A campaign whose leads turn out to sit in a
    different country is a real finding -- ``portfolio.disagrees_with_country``
    exists for it -- but it is a separate question from how the week was spent.

    Campaign and lead counts are lifetime; the delivery counters are the window.
    A market with three hundred leads and no sends this month is the shape worth
    seeing, and folding both into one window would hide it.
    """
    since = now - dt.timedelta(days=window_days)
    rows = (
        await session.execute(
            text(
                """
                SELECT c.region                                        AS region,
                       count(DISTINCT c.id)                            AS campaigns,
                       count(DISTINCT c.id) FILTER (
                           WHERE c.status = 'active'
                       )                                               AS active,
                       count(DISTINCT l.id)                            AS leads,
                       count(DISTINCT m.lead_id)                       AS contacted,
                       -- DISTINCT on the message id, not just on the filter.
                       -- Joining campaigns to leads and to messages separately
                       -- crosses the two within each campaign, so a plain
                       -- count(m.id) multiplies every message by the number of
                       -- leads in its campaign. Observed: 40 real sends
                       -- reported as 1,415.
                       count(DISTINCT m.id) FILTER (
                           WHERE m.sent_at IS NOT NULL
                       )                                               AS sent,
                       count(DISTINCT m.id) FILTER (
                           WHERE m.bounced_at IS NOT NULL
                       )                                               AS bounced,
                       count(DISTINCT l.id) FILTER (
                           WHERE l.replied_at IS NOT NULL
                       )                                               AS replied
                  FROM campaigns c
                  LEFT JOIN leads l ON l.campaign_id = c.id
                  LEFT JOIN messages m
                         ON m.campaign_id = c.id
                        AND m.created_at >= :since
                        AND m.workspace_id = :workspace
                 WHERE c.workspace_id = :workspace
                 GROUP BY c.region
                """
            ),
            {"workspace": session.info.get(WORKSPACE_KEY), "since": since},
        )
    ).all()

    slices = []
    for row in rows:
        try:
            region = Region(row.region)
        except ValueError:
            continue
        slices.append(
            portfolio.RegionSlice(
                region=region,
                campaigns=int(row.campaigns),
                active_campaigns=int(row.active),
                leads=int(row.leads),
                contacted=int(row.contacted),
                sent=int(row.sent),
                bounced=int(row.bounced),
                replied=int(row.replied),
            )
        )
    return portfolio.summarise(slices)


__all__ = [
    "REAL_REGIONS",
    "portfolio_view",
    "timing_report",
    "unconfigured_markets",
    "variant_comparison",
]
