"""Re-checking the claims that are about to be asserted.

The last guard between a measurement and a message. Everything upstream makes
the claim *provable when it was taken*; this asks whether it is still true
before Titan says it to a stranger.

**Why it is an activity and not part of the send gate.** It makes a network
request, and mission section 25 forbids I/O inside the unit of work the outbox
worker holds while it sends. Running it on the hourly pass instead means a
claim is checked while the draft waits rather than at the instant it goes,
which is the right trade: the draft sits in the queue for hours anyway, and a
send path that reaches out to the recipient's web server before every message
is a send path that stops when their web server does.

**Only the claims a single request can settle.** ``broken_internal_link`` and
``broken_primary_cta`` assert that an address does not work, which is answered
by asking the address. That is also what Titan overwhelmingly leads with -- 305
of the first 413 messages opened with a broken link on a booking or contact
path -- so the cheap half of the problem is most of the problem.

**A contradiction is expensive to get wrong in one direction only**, so the
asymmetry from :mod:`titan.intelligence.claim_recheck` is preserved here: only
a conclusive "it works now" withdraws a message. Anything inconclusive leaves
the draft exactly as it was.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from temporalio import activity

from titan.db.enums import DraftStatus, LeadStatus
from titan.db.session import workspace_session, workspace_unit_of_work
from titan.intelligence.claim_recheck import URL_STATUS_CLAIMS, Verdict, judge
from titan.providers.browser_client import BrowserWorkerClient
from titan.workflows.types import RecheckClaimsInput, RecheckClaimsResult

logger = logging.getLogger(__name__)

#: Claims re-checked per pass.
#:
#: Each is one browser context on the same worker the crawler uses, bounded at
#: four lanes. Twelve an hour is enough to cover a day's sending several times
#: over without competing with discovery for the browser.
MAX_PER_PASS = 12

#: Queued drafts whose leading claim is one a single request can settle.
#:
#: Ordered oldest-evidence-first: the older the measurement, the more likely
#: the business has fixed it, so the budget goes where the risk is.
_CANDIDATES = text("""
    SELECT d.id        AS draft_id,
           d.lead_id   AS lead_id,
           f.id        AS finding_id,
           f.issue_type::text AS issue_type,
           f.page_url  AS page_url,
           f.created_at AS measured_at
      FROM message_drafts d
      JOIN LATERAL jsonb_array_elements(d.claim_map) cm ON true
      JOIN audit_findings f ON f.id::text = cm.value->>'finding_id'
     WHERE d.workspace_id = :ws
       AND d.status = :queued
       AND jsonb_typeof(d.claim_map) = 'array'
       AND f.issue_type::text = ANY(:types)
       AND f.page_url IS NOT NULL
       AND f.contradicted IS NOT TRUE
     ORDER BY f.created_at
     LIMIT :limit
""")

#: The column that existed for this and was never written by anything.
_MARK_CONTRADICTED = text("""
    UPDATE audit_findings
       SET contradicted = true,
           contradiction_reason = :reason,
           updated_at = :now
     WHERE id = :finding_id
""")

_SUPERSEDE = text("""
    UPDATE message_drafts
       SET status = :superseded, updated_at = :now
     WHERE id = :draft_id
""")

#: Back to a researchable status so the site is re-crawled and a true message
#: written. Never for a lead somebody has decided about.
_REOPEN = text("""
    UPDATE leads
       SET status = :qualified, status_reason = :reason, updated_at = :now
     WHERE id = :lead_id
       AND replied_at IS NULL
       AND status = ANY(:reopenable)
""")

_REOPENABLE = (
    LeadStatus.DRAFTED.value,
    LeadStatus.AWAITING_APPROVAL.value,
    LeadStatus.QUEUED.value,
    LeadStatus.QUALIFIED.value,
    LeadStatus.RESEARCHED.value,
)


@dataclass
class _Report:
    checked: int = 0
    confirmed: int = 0
    contradicted: int = 0
    inconclusive: int = 0
    withdrawn: list[str] = field(default_factory=list)


@activity.defn(name="recheck_pending_claims")
async def recheck_pending_claims(request: RecheckClaimsInput) -> RecheckClaimsResult:
    """Test queued claims against the live site; withdraw the ones now false."""
    workspace_id = uuid.UUID(request.workspace_id)
    now = dt.datetime.now(dt.UTC)
    report = _Report()

    async with workspace_session(workspace_id) as session:
        rows = (
            await session.execute(
                _CANDIDATES,
                {
                    "ws": workspace_id,
                    "queued": DraftStatus.QUEUED.value,
                    "types": sorted(URL_STATUS_CLAIMS),
                    "limit": MAX_PER_PASS,
                },
            )
        ).all()

    if not rows:
        return RecheckClaimsResult()

    client = BrowserWorkerClient()
    try:
        for row in rows:
            # Outside a Temporal activity -- a CLI run, a test -- there is no
            # context to heartbeat into, and `activity.heartbeat` raises rather
            # than no-ops. The heartbeat exists so a slow batch is not killed
            # as stuck; it is not worth making the function impossible to call
            # by hand.
            try:
                activity.heartbeat(f"re-checking {row.page_url}")
            except RuntimeError:
                pass
            observed = await client.recheck(row.page_url)
            check = judge(issue_type=row.issue_type, observed=observed)
            report.checked += 1

            if check.verdict is Verdict.CONFIRMED:
                report.confirmed += 1
                continue
            if check.verdict is Verdict.INCONCLUSIVE:
                report.inconclusive += 1
                continue

            # Conclusively fixed. Withdraw the message rather than send a claim
            # the recipient can disprove in one click -- which is what 156
            # messages did before the detectors were corrected, and what the
            # staleness window bounds but does not prevent.
            report.contradicted += 1
            report.withdrawn.append(f"{row.issue_type} @ {row.page_url}")
            async with workspace_unit_of_work(workspace_id) as session:
                await session.execute(
                    _MARK_CONTRADICTED,
                    {
                        "reason": check.detail[:500],
                        "now": now,
                        "finding_id": row.finding_id,
                    },
                )
                await session.execute(
                    _SUPERSEDE,
                    {
                        "superseded": DraftStatus.SUPERSEDED.value,
                        "now": now,
                        "draft_id": row.draft_id,
                    },
                )
                await session.execute(
                    _REOPEN,
                    {
                        "qualified": LeadStatus.QUALIFIED.value,
                        "reason": "claim no longer true; re-measuring",
                        "now": now,
                        "lead_id": row.lead_id,
                        "reopenable": list(_REOPENABLE),
                    },
                )
            logger.info(
                "withdrew a message whose claim is no longer true",
                extra={"detail": check.detail, "lead_id": str(row.lead_id)},
            )
    finally:
        await client.aclose()

    return RecheckClaimsResult(
        checked=report.checked,
        confirmed=report.confirmed,
        contradicted=report.contradicted,
        inconclusive=report.inconclusive,
        withdrawn=tuple(report.withdrawn[:5]),
    )


ALL_CLAIM_VERIFICATION_ACTIVITIES = [recheck_pending_claims]

__all__ = [
    "ALL_CLAIM_VERIFICATION_ACTIVITIES",
    "MAX_PER_PASS",
    "recheck_pending_claims",
]
