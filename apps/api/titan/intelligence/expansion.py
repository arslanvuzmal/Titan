"""Opening the next market when the current one is worked out.

The last piece of unattended lead supply, and the one that was missing. Every
other stage of discovery runs on a schedule; the decision *where to look next*
was a person editing ``provision_markets.py`` and running it by hand. So when
the 29 configured combinations were exhausted, discovery simply stopped --
**no lead was created between 8 and 16 September** -- and nothing said so
except a campaign filing "budget but no eligible leads" into a CRM nobody reads.

**Discovery was never exhausted. The query list was.** Measured on 16
September: Google Places answered a never-searched combination (``dentists in
Glasgow UK``) with a full page of results in under a second, on the same API
key, for $0.019 a lead. The catalogue holds **107 territories** and the estate
targets **13 business types** -- 1,391 possible combinations, of which 29 were
in use. Two per cent.

**What exhaustion actually looks like**, and why it is measurable rather than
guessed: ``lead_sources`` records what each search returned and how much of it
was already known. A worked-out combination keeps returning results and stops
returning *new* ones. ``dentists / Manchester UK``: 320 records returned, zero
new. That is the signal, and it is a fact about the ground rather than an
inference about the API.

**This never authorises a send.** A campaign created here is
``RESEARCH_ONLY`` with ``sending_authorized`` false, exactly as
``provision_markets`` creates them and for the reason its comment gives:
provisioning a market is not authorising a send to it. So the worst case of a
bug here is crawling a city nobody asked for -- never mail to one.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from titan.config import OperatingMode
from titan.db.enums import CampaignStatus
from titan.db.models import Campaign, CampaignPolicy, SenderIdentity
from titan.intelligence import territories
from titan.policy.schedule import default_window_for

logger = logging.getLogger(__name__)

#: How many searches a combination must have had before "no new results" means
#: anything. One search returning nothing new is a duplicate run; six is a
#: worked-out city.
MIN_SEARCHES_BEFORE_JUDGING = 3

#: Judge a combination on its most recent searches, not its whole history.
#:
#: The first version of this aggregated over all time and it was wrong in a way
#: worth keeping the note for: ``dental implant clinics / Manchester UK`` came
#: back as the *only* worked-out combination at 5.0% new across 118 searches,
#: while ``dentists / Manchester UK`` -- 320 records returned and **zero** new
#: in the last three weeks -- looked perfectly productive, because it had found
#: two thousand businesses in August and the lifetime average carried it.
#:
#: A city is worked out when it *has stopped* producing, not when its average
#: is low. Ten searches is long enough that one quiet afternoon does not
#: qualify and short enough that August cannot outvote September.
RECENT_SEARCHES_JUDGED = 10

#: The share of returned records that must be *new* for a combination to still
#: count as productive. Below this it is re-reading the same businesses.
#:
#: 5% rather than zero: a city never stops returning the occasional new
#: listing, and waiting for absolute zero would hold a campaign open forever on
#: one new dentist a fortnight.
PRODUCTIVE_NEW_SHARE = 0.05

#: How many new campaigns one pass may open.
#:
#: Two. Each one starts crawling immediately and the browser worker is bounded
#: at eight concurrent activities, so opening ten at once would swamp the
#: crawler and starve the campaigns already running. The pass runs hourly;
#: sustained expansion does not need to be fast.
MAX_NEW_PER_PASS = 2

#: Never open more than this many active campaigns in total.
#:
#: The daily send budget is divided across active campaigns, and dividing it
#: too far is a defect this estate has already had: 100 sends across 23
#: campaigns gave 2 or 3 each, which is below the rate at which anything
#: accumulates. Discovery outruns sending, so the ceiling is set by what can be
#: *sent*, not by what can be found.
MAX_ACTIVE_CAMPAIGNS = 60

DEFAULT_MIN_LEAD_SCORE = 55
DEFAULT_RESEARCH_BUDGET_USD = 5.0
DEFAULT_DAILY_SEND_LIMIT = 25


@dataclass(frozen=True, slots=True)
class Exhausted:
    """A combination that has stopped producing new businesses."""

    campaign_id: uuid.UUID
    business_type: str
    geography: str
    searches: int
    returned: int
    new_records: int

    @property
    def new_share(self) -> float:
        return self.new_records / self.returned if self.returned else 0.0

    def describe(self) -> str:
        return (
            f"{self.business_type} / {self.geography}: {self.returned} records "
            f"returned across {self.searches} searches, {self.new_records} of "
            f"them new ({self.new_share:.1%})"
        )


@dataclass
class ExpansionReport:
    exhausted: list[Exhausted] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def is_noop(self) -> bool:
        return not self.opened


#: The most recent searches per campaign, then how much of what they returned
#: was new. Windowed rather than aggregated over all time -- see
#: RECENT_SEARCHES_JUDGED for the case that got through when it was not.
_EXHAUSTION = text("""
    WITH ranked AS (
        SELECT ls.campaign_id,
               ls.records_returned,
               ls.records_deduplicated,
               row_number() OVER (
                   PARTITION BY ls.campaign_id ORDER BY ls.created_at DESC
               ) AS recency
          FROM lead_sources ls
         WHERE ls.workspace_id = :ws
    )
    SELECT c.id                                   AS campaign_id,
           c.target_business_type::text           AS business_type,
           c.target_geography                     AS geography,
           count(r.campaign_id)                   AS searches,
           coalesce(sum(r.records_returned), 0)   AS returned,
           coalesce(sum(r.records_returned - r.records_deduplicated), 0) AS new_records
      FROM campaigns c
      JOIN ranked r ON r.campaign_id = c.id AND r.recency <= :recent
     WHERE c.workspace_id = :ws
       AND c.status = 'active'
       AND c.target_business_type IS NOT NULL
       AND c.target_geography IS NOT NULL
     GROUP BY 1, 2, 3
    HAVING count(r.campaign_id) >= :min_searches
""")


async def find_exhausted(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> list[Exhausted]:
    """Combinations that keep returning results and stop returning new ones."""
    rows = (
        await session.execute(
            _EXHAUSTION,
            {
                "ws": workspace_id,
                "min_searches": MIN_SEARCHES_BEFORE_JUDGING,
                "recent": RECENT_SEARCHES_JUDGED,
            },
        )
    ).all()

    worked_out = []
    for row in rows:
        found = Exhausted(
            campaign_id=row.campaign_id,
            business_type=row.business_type,
            geography=row.geography,
            searches=int(row.searches),
            returned=int(row.returned),
            new_records=int(row.new_records),
        )
        if found.returned and found.new_share < PRODUCTIVE_NEW_SHARE:
            worked_out.append(found)
    return worked_out


async def _taken(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> set[tuple[str, str]]:
    """Every (business type, geography) the workspace already has a campaign for.

    Every status, not only active. A paused or finished campaign has already
    worked that ground, and re-opening it would spend the discovery budget
    re-reading businesses the estate knows.
    """
    rows = (
        await session.execute(
            select(Campaign.target_business_type, Campaign.target_geography).where(
                Campaign.workspace_id == workspace_id
            )
        )
    ).all()
    return {(str(bt), str(geo)) for bt, geo in rows if bt is not None and geo is not None}


def _next_territory(
    business_type: str, taken: set[tuple[str, str]]
) -> territories.Territory | None:
    """The densest unworked metro for this business type.

    ``TERRITORIES`` is ordered densest-first within each region, and that order
    is the whole ranking: the first entry this business type has not been run
    against is the best remaining place to look.
    """
    for territory in territories.TERRITORIES:
        if (business_type, territory.query_name) not in taken:
            return territory
    return None


async def expand(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    limit: int = MAX_NEW_PER_PASS,
    apply: bool = False,
) -> ExpansionReport:
    """Open the next market for each worked-out combination.

    Does not commit -- the caller owns the transaction, as everywhere else in
    this package.
    """
    report = ExpansionReport()
    report.exhausted = await find_exhausted(session, workspace_id=workspace_id)
    if not report.exhausted:
        report.reason = "no combination is worked out yet"
        return report

    active_count = (
        await session.scalar(
            select(func.count())
            .select_from(Campaign)
            .where(
                Campaign.workspace_id == workspace_id,
                Campaign.status == CampaignStatus.ACTIVE,
            )
        )
        or 0
    )

    if active_count >= MAX_ACTIVE_CAMPAIGNS:
        report.reason = (
            f"{active_count} active campaigns already, ceiling is "
            f"{MAX_ACTIVE_CAMPAIGNS}; the send budget divides too far beyond it"
        )
        return report

    taken = await _taken(session, workspace_id=workspace_id)

    # One sender for every campaign this pass opens, chosen the way
    # provision_markets chooses it. None is acceptable: a research-only campaign
    # never sends, and the send path resolves its own mailbox anyway.
    sender = (
        await session.execute(
            select(SenderIdentity)
            .where(
                SenderIdentity.workspace_id == workspace_id,
                SenderIdentity.is_active.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    for worked_out in report.exhausted:
        if len(report.opened) >= limit:
            break
        if active_count + len(report.opened) >= MAX_ACTIVE_CAMPAIGNS:
            break

        territory = _next_territory(worked_out.business_type, taken)
        if territory is None:
            logger.info(
                "no unworked territory left for this business type",
                extra={"business_type": worked_out.business_type},
            )
            continue

        label = f"{worked_out.business_type} / {territory.query_name}"
        report.opened.append(label)
        taken.add((worked_out.business_type, territory.query_name))

        if not apply:
            continue

        source = await session.get(Campaign, worked_out.campaign_id)
        window = default_window_for(territory.country_code)
        slug = _slug(worked_out.business_type, territory)

        campaign = Campaign(
            workspace_id=workspace_id,
            name=f"{worked_out.business_type.title()}, {territory.city}",
            slug=slug,
            status=CampaignStatus.ACTIVE,
            # Carried from the campaign that ran dry: the industry vocabulary
            # belongs to the business type, not to the city.
            industry=source.industry if source is not None else None,
            target_business_type=worked_out.business_type,
            target_geography=territory.query_name,
            target_country_code=territory.country_code,
            region=territory.region,
            sub_region=territory.sub_region,
            sender_identity_id=sender.id if sender is not None else None,
        )
        session.add(campaign)
        await session.flush()

        session.add(
            CampaignPolicy(
                workspace_id=workspace_id,
                campaign_id=campaign.id,
                # RESEARCH_ONLY and not authorised, exactly as
                # provision_markets creates them. Opening a market is not
                # authorising a send to it, and that is what makes it safe for
                # this to happen without a human in the loop.
                operating_mode=OperatingMode.RESEARCH_ONLY,
                sending_authorized=False,
                min_lead_score=DEFAULT_MIN_LEAD_SCORE,
                research_budget_usd=DEFAULT_RESEARCH_BUDGET_USD,
                daily_send_limit=DEFAULT_DAILY_SEND_LIMIT,
                send_window_start_hour=window.start_hour,
                send_window_end_hour=window.end_hour,
                send_days=list(window.days),
            )
        )
        logger.info(
            "opened a new market",
            extra={
                "business_type": worked_out.business_type,
                "geography": territory.query_name,
                "because": worked_out.describe(),
            },
        )

    if not report.opened:
        report.reason = "every territory in the catalogue is already worked"
    return report


def _slug(business_type: str, territory: territories.Territory) -> str:
    """Stable, readable, and unique per combination."""
    trade = business_type.lower().replace(" ", "-")
    city = territory.city.lower().replace(" ", "-").replace(",", "")
    return f"{territory.country_code.lower()}-{trade}-{city}"[:100]


__all__ = [
    "MAX_ACTIVE_CAMPAIGNS",
    "MAX_NEW_PER_PASS",
    "MIN_SEARCHES_BEFORE_JUDGING",
    "PRODUCTIVE_NEW_SHARE",
    "RECENT_SEARCHES_JUDGED",
    "Exhausted",
    "ExpansionReport",
    "expand",
    "find_exhausted",
]
