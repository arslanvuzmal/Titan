"""Re-measure a claim before it ages out, rather than letting the draft rot.

The send gate refuses any message whose evidence is older than
:data:`coldops.policy.engine.MAX_EVIDENCE_AGE` -- thirty days -- and that rule is
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

from coldops.db.enums import DraftStatus, LeadStatus
from coldops.intelligence.call_list import MAX_EVIDENCE_AGE_DAYS as CALL_EVIDENCE_AGE_DAYS
from coldops.policy.engine import MAX_EVIDENCE_AGE

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
       -- Scoped here, not by the id alone. The id came from a workspace-scoped
       -- read, so today this predicate changes no row; the session adds no
       -- filter of its own to raw SQL, so the day the id list comes from
       -- anywhere else it is the only thing standing between this write and
       -- another workspace's rows.
       AND workspace_id = :ws
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
       -- Scoped here, not by the id alone. The id came from a workspace-scoped
       -- read, so today this predicate changes no row; the session adds no
       -- filter of its own to raw SQL, so the day the id list comes from
       -- anywhere else it is the only thing standing between this write and
       -- another workspace's rows.
       AND workspace_id = :ws
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


#: How old a *callable* lead's evidence may get before it goes back for a crawl.
#:
#: The draft sweep above protects the email gate, which allows thirty days. The
#: phone allows fourteen -- deliberately, because an email can be hedged and
#: re-read and a spoken claim cannot be withdrawn once the practice has checked
#: and found it fixed. Nothing was protecting that stricter gate, and the two
#: numbers left a seven-day hole: a lead passed fourteen days and became
#: uncallable, and nothing looked at it again until twenty-one.
#:
#: Measured on 27 September, with the calling start eight days away: 549 leads
#: were callable that day, 303 would still be callable on 6 October, and **none
#: at all on the 20th**. The whole phone pipeline expired mid-month with no
#: mechanism pointed at it.
#:
#: Three days inside the gate rather than the draft sweep's nine. A re-crawl and
#: re-score is the same hourly pipeline, but there is no draft to write and no
#: send window to wait for at the end of it -- the lead is callable the moment
#: the findings land.
CALL_STALE_AFTER = dt.timedelta(days=CALL_EVIDENCE_AGE_DAYS) - dt.timedelta(days=3)

#: Leads returned per pass, smaller than the draft sweep's twenty-five.
#:
#: The same bounded crawler serves discovery, the draft sweep and this. Fifteen
#: an hour is 360 a day against a callable pool in the hundreds, which refreshes
#: it faster than it can age without starving the other two.
CALL_MAX_PER_PASS = 15

#: Callable leads whose newest surviving finding is going stale.
#:
#: Scoped to leads that can actually be rung, so the crawl budget buys calling
#: capacity rather than being spent on rows the phone would never reach anyway:
#: an active campaign, a number on file, nobody who has replied, nothing already
#: suppressed for calls, and nothing already dialled.
#:
#: Keyed on the newest finding for the same reason the draft sweep is -- the map
#: is written in one pass from one crawl, so the newest finding is the age of
#: the crawl.
_CALL_STALE = text("""
    SELECT l.id AS lead_id, max(f.created_at) AS evidence_at
      FROM leads l
      JOIN organizations o ON o.id = l.organization_id
      JOIN campaigns c     ON c.id = l.campaign_id
      JOIN audit_findings f ON f.lead_id = l.id AND f.contradicted IS NOT TRUE
     WHERE l.workspace_id = :ws
       AND c.status = 'active'
       AND o.phone_e164 IS NOT NULL
       AND l.replied_at IS NULL
       AND l.status = ANY(:reopenable)
       AND NOT EXISTS (
             SELECT 1 FROM call_suppressions s
              WHERE s.workspace_id = l.workspace_id
                AND right(regexp_replace(s.phone_e164, '[^0-9]', '', 'g'), 9)
                  = right(regexp_replace(o.phone_e164, '[^0-9]', '', 'g'), 9))
       AND NOT EXISTS (
             SELECT 1 FROM call_outcomes co
              WHERE co.lead_id = l.id AND co.stage = 1)
     GROUP BY l.id
    HAVING max(f.created_at) < :cutoff
     ORDER BY max(f.created_at)
     LIMIT :limit
""")


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
        {
            "superseded": DraftStatus.SUPERSEDED.value,
            "now": now,
            "drafts": draft_ids,
            "ws": workspace_id,
        },
    )
    result = await session.execute(
        _REOPEN,
        {
            "qualified": LeadStatus.QUALIFIED.value,
            "reason": f"evidence older than {STALE_AFTER.days} days; re-measuring",
            "now": now,
            "leads": lead_ids,
            "ws": workspace_id,
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


@dataclass
class CallPoolReport:
    """What one pass did for the callable pool."""

    #: Callable leads whose evidence was going stale.
    ageing: int = 0
    #: Sent back for a fresh crawl.
    reopened: int = 0
    #: Age of the oldest one found, in days.
    oldest_days: int | None = None


async def sweep_ageing_call_evidence(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    now: dt.datetime,
    limit: int = CALL_MAX_PER_PASS,
    apply: bool = True,
) -> CallPoolReport:
    """Return callable leads for a fresh crawl before the phone gate refuses them.

    The companion to :func:`sweep_stale_evidence`, which protects the email gate
    at thirty days and leaves the phone's fourteen unguarded. Same remedy, same
    write path, different clock.

    Reopening is all this does. It does not start a workflow: the planner owns
    how much research runs at once, and a sweep that bypassed it could swamp the
    crawler that discovery and the draft sweep are also sharing.
    """
    cutoff = now - CALL_STALE_AFTER
    rows = (
        (
            await session.execute(
                _CALL_STALE,
                {
                    "ws": workspace_id,
                    "reopenable": list(_REOPENABLE),
                    "cutoff": cutoff,
                    "limit": limit,
                },
            )
        )
        .mappings()
        .all()
    )

    report = CallPoolReport(ageing=len(rows))
    if not rows:
        return report

    report.oldest_days = int((now - rows[0]["evidence_at"]).days)
    if not apply:
        return report

    leads = [r["lead_id"] for r in rows]
    reopened = (
        await session.execute(
            _REOPEN,
            {
                "ws": workspace_id,
                "leads": leads,
                "qualified": LeadStatus.QUALIFIED.value,
                "reopenable": list(_REOPENABLE),
                "reason": (
                    f"callable evidence older than {CALL_STALE_AFTER.days} days; "
                    "re-measuring before the phone gate refuses it"
                ),
                "now": now,
            },
        )
    ).rowcount or 0
    report.reopened = int(reopened)

    logger.info(
        "returned %s callable lead(s) for a fresh crawl; oldest evidence was %s days",
        report.reopened,
        report.oldest_days,
        extra={"ageing": report.ageing},
    )
    return report


__all__ = [
    "CALL_MAX_PER_PASS",
    "CALL_STALE_AFTER",
    "MAX_PER_PASS",
    "STALE_AFTER",
    "CallPoolReport",
    "StalenessReport",
    "sweep_ageing_call_evidence",
    "sweep_stale_evidence",
]
