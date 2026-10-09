"""Turning proposals into rows, and writing down what happened either way.

The only module in the package that touches a session. Everything upstream is
pure, so this is the whole surface between a decision and the database, and it
is deliberately small enough to read in one sitting.

**A decision row is written for every proposal**, including the ones the bounds
clamped and the ones that turned out to change nothing. The clamped ones are the
point: a proposal the boundary caught is the only direct evidence the boundary
works, and a table containing only successes would be a record of the manager
agreeing with itself.

**Except a no-op that repeats the last one.** A proposal to set something to the
value it already has, refused or not for the same reason as last cycle, says
nothing the previous row did not. Recorded every cycle it came to 282,944 of
306,516 rows by October -- one sentence ("no variant to promote") written every
few minutes for two months -- and it buried the 23,572 proposals that actually
reached for a change. The first no-op is kept, and so is any no-op whose reason
or refusal differs from the last recorded one: the trail still shows every
transition, just not every repetition of a state.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coldops.autonomy.actuator import Actuation, Bounds, Proposal, Verdict, evaluate
from coldops.autonomy.health import CampaignHealth
from coldops.db.models import AutonomyDecision, CampaignPolicy

logger = logging.getLogger(__name__)

#: Which policy column each actuation writes. The manager's columns, never the
#: human's -- see the migration for why the two are kept apart.
_COLUMN_FOR: dict[Actuation, str] = {
    Actuation.SET_DAILY_LIMIT: "managed_daily_send_limit",
    Actuation.SET_MIN_LEAD_SCORE: "managed_min_lead_score",
    Actuation.SET_PROMOTED_VARIANT: "managed_promoted_variant",
}


async def apply_all(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    campaign_id: uuid.UUID,
    health: CampaignHealth,
    proposals: list[Proposal],
    bounds: Bounds,
    now: dt.datetime,
) -> list[Verdict]:
    """Evaluate each proposal against the bounds, write it, and record it."""
    verdicts: list[Verdict] = []
    for proposal in proposals:
        verdict = evaluate(proposal, bounds)
        if verdict.changes_anything:
            await _write(session, campaign_id, verdict)
        verdicts.append(verdict)
        if await _repeats_last_no_op(session, campaign_id, verdict):
            continue
        session.add(
            _record(
                workspace_id=workspace_id,
                campaign_id=campaign_id,
                health=health,
                verdict=verdict,
                now=now,
            )
        )

    changed = [v for v in verdicts if v.changes_anything]
    if changed:
        logger.info(
            "campaign manager adjusted a campaign",
            extra={
                "campaign_id": str(campaign_id),
                "health": health.value,
                "changes": [
                    f"{v.proposal.actuation.value}={v.applied_value}" for v in changed
                ],
            },
        )
    return verdicts


async def _write(session: AsyncSession, campaign_id: uuid.UUID, verdict: Verdict) -> None:
    column = _COLUMN_FOR.get(verdict.proposal.actuation)
    if column is None:
        # Unreachable through evaluate(), which refuses an unimplemented
        # actuation before this point. Belt and braces: the failure mode of
        # getting this wrong is writing to a column nobody intended.
        raise ValueError(f"no column for {verdict.proposal.actuation.value}")
    await session.execute(
        update(CampaignPolicy)
        .where(CampaignPolicy.campaign_id == campaign_id)
        .values(**{column: verdict.applied_value})
    )


def is_no_op(verdict: Verdict) -> bool:
    """A proposal to keep things as they are, that kept them as they are."""
    return (
        not verdict.changes_anything
        and verdict.proposal.proposed == verdict.proposal.current
    )


async def _repeats_last_no_op(
    session: AsyncSession, campaign_id: uuid.UUID, verdict: Verdict
) -> bool:
    """Whether recording this would only repeat the last row for this actuation."""
    if not is_no_op(verdict):
        return False
    last = (
        await session.execute(
            select(
                AutonomyDecision.previous_value,
                AutonomyDecision.proposed_value,
                AutonomyDecision.applied,
                AutonomyDecision.reason,
                AutonomyDecision.refusal,
            )
            .where(
                AutonomyDecision.campaign_id == campaign_id,
                AutonomyDecision.actuation == verdict.proposal.actuation.value,
            )
            .order_by(AutonomyDecision.decided_at.desc())
            .limit(1)
        )
    ).first()
    if last is None or last.applied or last.proposed_value != last.previous_value:
        return False
    return (
        last.previous_value == verdict.proposal.current
        and last.reason == verdict.proposal.reason
        and last.refusal == verdict.refusal
    )


def _record(
    *,
    workspace_id: uuid.UUID,
    campaign_id: uuid.UUID,
    health: CampaignHealth,
    verdict: Verdict,
    now: dt.datetime,
) -> AutonomyDecision:
    proposal = verdict.proposal
    return AutonomyDecision(
        workspace_id=workspace_id,
        campaign_id=campaign_id,
        decided_at=now,
        actuation=proposal.actuation.value,
        health=health.value,
        previous_value=proposal.current,
        proposed_value=proposal.proposed,
        applied_value=verdict.applied_value,
        applied=verdict.changes_anything,
        refusal=verdict.refusal,
        reason=proposal.reason,
        evidence=dict(proposal.evidence),
        confidence=proposal.confidence,
    )


__all__ = ["apply_all"]
