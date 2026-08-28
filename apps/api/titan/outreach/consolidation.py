"""Merging a market's worth of city campaigns into one business-type campaign.

Twenty-three campaigns divide twenty-five sends a day. Every one of them is
classified *learning* by the allocator, because none accumulates enough volume
to be judged on -- which is not the allocator failing to concentrate, it is the
allocator declining to invent a preference it has no evidence for.

Six campaigns dividing fifty sends is eight a day each, two hundred and fifty a
month. That is enough to measure a reply rate. It is the same mail either way;
what changes is whether the result means anything.

**Nothing is deleted.** One campaign per industry is promoted -- the one already
holding the most leads, so the fewest rows move -- and the rest are paused after
their leads, drafts and history are reassigned to it. A paused campaign with
nothing left in it is a record of what was tried, and reversing this is a matter
of moving rows back rather than reconstructing them.

**Everything, or nothing.** The move runs in one transaction per industry. A
lead whose drafts stayed behind is worse than either state on its own: the
draft's campaign would no longer be the lead's campaign, and every gate that
reads policy from the campaign would read the wrong one.

**Refuses only while a worker holds a message.** A leased outbox row is being
written to right now, and it is the one state where the campaign could change
underneath a send in progress. Queued, deferred and sent rows travel with their
lead in the same transaction, so nothing loses its thread.

The first version of that guard refused on anything not yet sent, and a live dry
run showed it blocking all six industries on forty-four rows waiting for a
sending window that will not open until a carrier plan is paid. A condition that
clears only when somebody buys something is not a reason to refuse.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.enums import CampaignStatus, Industry
from titan.db.models import Campaign

#: Tables whose rows belong to a campaign and must travel with its leads.
#:
#: Listed rather than discovered from the schema: a table added later should
#: fail loudly here by being absent from a review, not be swept along silently
#: by a reflection that nobody read.
MOVED_TABLES: tuple[str, ...] = (
    "leads",
    "message_drafts",
    "outbox_messages",
    "messages",
    "lead_sources",
    "research_runs",
    "model_runs",
    "workflow_runs",
    "autonomy_decisions",
    "usage_ledger",
    "smartlead_import_batches",
)

#: Tables that describe the campaign itself rather than its work. The survivor
#: keeps its own; the absorbed campaigns keep theirs and are paused with them,
#: so nothing has to be merged and no policy is silently overwritten.
KEPT_TABLES: tuple[str, ...] = (
    "campaign_policies",
    "campaign_senders",
    "email_sequences",
)


#: What a consolidated campaign is called. Plural, because it is a population
#: rather than a place, and explicit rather than title-cased -- "Hvac Home
#: Services" is what the mechanical version produces and it is not a name
#: anybody would write.
VERTICAL_NAMES: dict[str, str] = {
    "dentist": "Dentists",
    "med_spa": "Med spas",
    "law_firm": "Law firms",
    "hvac_home_services": "HVAC and home services",
    "real_estate": "Estate agents",
    "gym_fitness": "Gyms and fitness",
    "general": "General",
}


def vertical_name(industry: Industry) -> str:
    """The campaign name for a whole business type.

    Falls back to the enum's own words rather than raising: an industry added
    later should get a serviceable name, not stop a consolidation.
    """
    return VERTICAL_NAMES.get(
        industry.value, industry.value.replace("_", " ").capitalize()
    )


@dataclass(frozen=True, slots=True)
class Move:
    """One industry's consolidation, as it would happen."""

    industry: Industry
    survivor_id: uuid.UUID
    survivor_name: str
    absorbed: tuple[tuple[uuid.UUID, str], ...]
    leads: int
    drafts: int
    #: Populated when this industry is refused. Nothing moves for it.
    blockers: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.blockers


@dataclass
class Plan:
    moves: list[Move] = field(default_factory=list)

    @property
    def actionable(self) -> list[Move]:
        return [m for m in self.moves if m.ok and m.absorbed]

    def render(self) -> str:
        lines: list[str] = []
        for move in self.moves:
            head = f"{move.industry.value:<20} {len(move.absorbed) + 1} -> 1"
            if not move.ok:
                lines.append(f"  REFUSED  {head}")
                lines.extend(f"             {b}" for b in move.blockers)
                continue
            if not move.absorbed:
                lines.append(f"  no change {head[:20]} (already one campaign)")
                continue
            lines.append(
                f"  {head}   {move.leads} leads, {move.drafts} drafts "
                f"-> {move.survivor_name}"
            )
            lines.extend(f"             absorb  {name}" for _, name in move.absorbed)
        return "\n".join(lines)


async def _count(
    session: AsyncSession,
    sql: str,
    *,
    workspace_id: uuid.UUID,
    campaign_ids: list[uuid.UUID],
) -> int:
    """One scalar count, with the workspace stated rather than assumed.

    Workspace isolation in this codebase is an ORM guard. These are raw
    statements, so they carry their own ``workspace_id`` predicate -- see the
    same reasoning in :func:`apply_move`.
    """
    if not campaign_ids:
        return 0
    value = (
        await session.execute(text(sql), {"cids": campaign_ids, "ws": workspace_id})
    ).scalar_one()
    return int(value or 0)


_LEADS = (
    "SELECT count(*) FROM leads WHERE campaign_id = ANY(:cids) AND workspace_id = :ws"
)
_DRAFTS = (
    "SELECT count(*) FROM message_drafts "
    "WHERE campaign_id = ANY(:cids) AND workspace_id = :ws"
)
#: Messages a sending worker is holding right now.
#:
#: The narrow guard, and it took a live run to find the right width. The first
#: version refused on anything not yet sent, which blocked all six industries on
#: 44 rows sitting ``deferred`` -- waiting for a sending window that will not
#: open until a carrier plan is paid. A condition that clears only when somebody
#: buys something is not a reason to refuse a reorganisation.
#:
#: ``pending`` and ``deferred`` rows travel with their lead: the outbox row is
#: reassigned in the same transaction and nothing has left the building.
#: ``sent`` rows travel too, and their message rows with them, so reply and
#: bounce attribution still resolves to the same lead.
#:
#: ``leased`` is the state that matters. A worker holds that row and will write
#: to it, and it is the only one where the campaign could change underneath a
#: send already in progress. The real enum, read rather than guessed: pending,
#: leased, deferred, sent, failed_permanent, cancelled.
_PENDING = (
    "SELECT count(*) FROM outbox_messages "
    "WHERE campaign_id = ANY(:cids) AND workspace_id = :ws "
    "AND status = 'leased'"
)


async def build_plan(session: AsyncSession, *, workspace_id: uuid.UUID) -> Plan:
    """What consolidating this workspace would do, without doing any of it."""
    campaigns = (
        (
            await session.execute(
                select(Campaign).where(
                    Campaign.status == CampaignStatus.ACTIVE,
                    Campaign.workspace_id == workspace_id,
                )
            )
        )
        .scalars()
        .all()
    )

    by_industry: dict[Industry, list[Campaign]] = {}
    for campaign in campaigns:
        by_industry.setdefault(campaign.industry, []).append(campaign)

    plan = Plan()
    for industry, group in sorted(by_industry.items(), key=lambda kv: kv[0].value):
        ids = [c.id for c in group]
        counts = {
            c.id: await _count(
                session, _LEADS, workspace_id=workspace_id, campaign_ids=[c.id]
            )
            for c in group
        }
        # The campaign already holding the most leads survives, so the fewest
        # rows move. Ties break on the older campaign, which has the longer
        # history for the allocator to read.
        survivor = max(group, key=lambda c: (counts[c.id], -c.created_at.timestamp()))
        absorbed = tuple((c.id, c.name) for c in group if c.id != survivor.id)

        blockers: list[str] = []
        pending = await _count(
            session, _PENDING, workspace_id=workspace_id, campaign_ids=ids
        )
        if pending:
            blockers.append(
                f"{pending} message(s) are leased by a sending worker right "
                "now; retry shortly, once the lease expires or the send finishes"
            )

        plan.moves.append(
            Move(
                industry=industry,
                survivor_id=survivor.id,
                survivor_name=survivor.name,
                absorbed=absorbed,
                leads=sum(counts.values()),
                drafts=await _count(
                    session, _DRAFTS, workspace_id=workspace_id, campaign_ids=ids
                ),
                blockers=tuple(blockers),
            )
        )
    return plan


async def apply_move(
    session: AsyncSession, move: Move, *, workspace_id: uuid.UUID
) -> dict[str, int]:
    """Reassign one industry's work to its survivor and pause the rest.

    The caller owns the transaction, and it must be one transaction: a lead
    whose drafts stayed behind has a draft whose campaign is not its own, and
    every gate that reads policy from the campaign would read the wrong one.

    ``workspace_id`` is written into every predicate rather than relied on from
    the session. Workspace isolation here is an ORM guard, and these are raw
    statements -- so the scoping is stated, not assumed.
    """
    if not move.ok:
        raise ValueError(f"{move.industry.value} is refused: {move.blockers}")

    absorbed_ids = [cid for cid, _ in move.absorbed]
    if not absorbed_ids:
        return {}

    moved: dict[str, int] = {}
    for table in MOVED_TABLES:
        result = await session.execute(
            text(
                f"UPDATE {table} SET campaign_id = :survivor "
                "WHERE campaign_id = ANY(:absorbed) AND workspace_id = :ws"
            ),
            {
                "survivor": move.survivor_id,
                "absorbed": absorbed_ids,
                "ws": workspace_id,
            },
        )
        # CursorResult carries rowcount; the base Result protocol does not.
        rows = int(getattr(result, "rowcount", 0) or 0)
        if rows:
            moved[table] = rows

    await session.execute(
        update(Campaign)
        .where(
            Campaign.id.in_(absorbed_ids),
            Campaign.workspace_id == workspace_id,
        )
        .values(status=CampaignStatus.PAUSED, paused_at=func.now())
    )
    # The survivor stops being a city and becomes a population. Renamed as well
    # as re-scoped: a campaign called "Dentists Leeds UK" that now works Sydney
    # is a name that will mislead somebody reading a report in three weeks.
    #
    # The slug is deliberately left alone. It is unique per workspace and other
    # records refer to it; a rename there buys nothing and can collide.
    await session.execute(
        update(Campaign)
        .where(
            Campaign.id == move.survivor_id,
            Campaign.workspace_id == workspace_id,
        )
        .values(spans_all_markets=True, name=vertical_name(move.industry))
    )
    return moved


__all__ = [
    "KEPT_TABLES",
    "MOVED_TABLES",
    "VERTICAL_NAMES",
    "Move",
    "Plan",
    "apply_move",
    "build_plan",
    "vertical_name",
]
