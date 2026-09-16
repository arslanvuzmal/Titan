"""Re-measure a claim before it ages out, rather than letting the draft rot.

The send gate refuses any message whose evidence is older than
:data:`titan.policy.engine.MAX_EVIDENCE_AGE` -- thirty days -- and that rule is
right: telling a business its booking page is broken on the strength of a crawl
from last month is how this estate once sent 156 messages making a claim the
recipient could disprove in one click.

But refusing is all it did. Nothing acted *before* a draft reached that age, so
a draft written on day one simply sat in the queue until day thirty and then
became permanently unsendable. Measured on the live estate the day it moved to
its own server: **167 queued drafts already rest on evidence over thirty days
old** and can never send, and another 255 rest on evidence between fourteen and
thirty days -- each one a claim about a defect that may have been fixed a
fortnight ago.

**The answer is to re-measure, not to relax.** A lead whose draft is going stale
is returned to the research pipeline, which re-crawls the site, re-runs the
detectors and writes a new draft from what is true today. The old draft is
superseded rather than deleted: its claim map is the record of what was asserted
and why, and that record is the thing that makes a false claim auditable.

**It acts early on purpose.** The threshold is below the gate's, so a lead has
time to be re-crawled, re-scored and re-drafted before the message it is waiting
on would have been refused. Acting at the gate would be too late: by then the
work is wasted and the lead has been silent for a month.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.enums import DraftStatus, LeadStatus
from titan.policy.engine import MAX_EVIDENCE_AGE

logger = logging.getLogger(__name__)

#: How old a draft's evidence may get before the lead is sent back for a fresh
#: crawl.
#:
#: Nine days inside the gate's thirty. A re-crawl, a re-score and a re-draft is
#: an hourly pipeline rather than an instant one, and the lead then has to wait
#: for a send window and its share of the daily budget. Nine days is comfortable
#: for that without being so early that healthy drafts are churned: a draft
#: written today is untouched for three weeks.
STALE_AFTER = MAX_EVIDENCE_AGE - dt.timedelta(days=9)

#: Leads returned to research per pass.
#:
#: Each one costs a browser crawl, and the crawler is bounded at eight
#: concurrent activities. This runs hourly, so twenty-five an hour drains the
#: 167 already past the gate inside a day without competing with the discovery
#: the same crawler is doing.
MAX_PER_PASS = 25

#: Draft states worth rescuing. A rejected or superseded draft is already
#: decided; re-researching its lead would reopen a question somebody closed.
_LIVE_DRAFTS = (
    DraftStatus.QUEUED.value,
    DraftStatus.APPROVED.value,
    DraftStatus.AWAITING_APPROVAL.value,
    DraftStatus.GENERATED.value,
)

#: Drafts whose newest cited finding is older than the threshold.
#:
#: Keyed on the *newest* finding rather than the oldest: a message asserts every
#: claim in its map, but the map is written in one pass from one crawl, so the
#: newest is the age of the crawl. Taking the oldest would age a draft by
#: whichever detector happened to have run first.
_STALE = text("""
    SELECT d.id AS draft_id,
           d.lead_id,
           max(f.created_at) AS evidence_at
      FROM message_drafts d
      JOIN LATERAL jsonb_array_elements(d.claim_map) cm ON true
      JOIN audit_findings f ON f.id::text = cm.value->>'finding_id'
     WHERE d.workspace_id = :ws
       AND d.status = ANY(:live)
       AND jsonb_typeof(d.claim_map) = 'array'
     GROUP BY d.id, d.lead_id
    HAVING max(f.created_at) < :cutoff
     ORDER BY max(f.created_at)
     LIMIT :limit
""")

#: Superseded, never deleted. The claim map is the record of what was asserted
#: and on what evidence, and destroying it would remove the only proof that a
#: message was justified when it was written.
_SUPERSEDE = text("""
    UPDATE message_drafts
       SET status = :superseded, updated_at = :now
     WHERE id = ANY(:drafts)
""")

#: Back to QUALIFIED, which is a RESEARCHABLE_STATUS, so the campaign
#: orchestrator picks the lead up on its next cycle and the research pipeline
#: re-crawls it. Nothing here starts a workflow directly: the planner owns how
#: much research runs at once, and a sweep that bypassed it could swamp the
#: crawler.
_REOPEN = text("""
    UPDATE leads
       SET status = :qualified,
           status_reason = :reason,
           updated_at = :now
     WHERE id = ANY(:leads)
       AND status = ANY(:reopenable)
       AND replied_at IS NULL
""")

#: Never reopen a lead a person or a reply has decided. A suppressed lead asked
#: not to hear from us; a replied lead is in a conversation; a rejected one was
#: judged. Re-crawling any of them would be work, but re-drafting them would be
#: writing to somebody who is not waiting for it.
_REOPENABLE = (
    LeadStatus.DRAFTED.value,
    LeadStatus.AWAITING_APPROVAL.value,
    LeadStatus.QUEUED.value,
    LeadStatus.QUALIFIED.value,
    LeadStatus.RESEARCHED.value,
)


@dataclass
class StalenessReport:
    #: Drafts found resting on evidence past the threshold.
    stale: int = 0
    #: Drafts superseded, and leads sent back for a fresh crawl.
    reopened: int = 0
    #: Already past the gate's own limit when found -- these could never have
    #: sent, and are the backlog rather than the flow.
    already_unsendable: int = 0
    oldest_days: int | None = None
    examples: list[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return self.stale == 0


async def sweep_stale_evidence(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    now: dt.datetime,
    limit: int = MAX_PER_PASS,
    apply: bool = True,
) -> StalenessReport:
    """Return leads with ageing claims to the research pipeline.

    Does not commit -- the caller owns the transaction, as everywhere else in
    this package.
    """
    report = StalenessReport()
    cutoff = now - STALE_AFTER
    gate = now - MAX_EVIDENCE_AGE

    rows = (
        await session.execute(
            _STALE,
            {
                "ws": workspace_id,
                "live": list(_LIVE_DRAFTS),
                "cutoff": cutoff,
                "limit": limit,
            },
        )
    ).all()
    if not rows:
        return report

    report.stale = len(rows)
    report.already_unsendable = sum(1 for r in rows if r.evidence_at < gate)
    oldest = min(r.evidence_at for r in rows)
    report.oldest_days = int((now - oldest).days)
    report.examples = [
        f"draft {str(r.draft_id)[:8]} on evidence {int((now - r.evidence_at).days)}d old"
        for r in rows[:3]
    ]

    if not apply:
        return report

    draft_ids = [r.draft_id for r in rows]
    lead_ids = list({r.lead_id for r in rows})

    await session.execute(
        _SUPERSEDE,
        {"superseded": DraftStatus.SUPERSEDED.value, "now": now, "drafts": draft_ids},
    )
    result = await session.execute(
        _REOPEN,
        {
            "qualified": LeadStatus.QUALIFIED.value,
            "reason": f"evidence older than {STALE_AFTER.days} days; re-measuring",
            "now": now,
            "leads": lead_ids,
            "reopenable": list(_REOPENABLE),
        },
    )
    report.reopened = result.rowcount or 0

    logger.info(
        "returned %s lead(s) for a fresh crawl; oldest evidence was %s days",
        report.reopened,
        report.oldest_days,
        extra={"stale_drafts": report.stale, "past_gate": report.already_unsendable},
    )
    return report


__all__ = [
    "MAX_PER_PASS",
    "STALE_AFTER",
    "StalenessReport",
    "sweep_stale_evidence",
]
