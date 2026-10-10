"""Rendering each first email's personal PDF before the outbox worker gets to it.

A sweep, not a pipeline step, and that is deliberate. The research workflow
that writes drafts can sit for days waiting on an approval; adding an activity
to it would break every one of those workflows on replay. A sweep over the
outbox is new code on its own schedule, so it can be deployed, switched off or
changed without touching a workflow in flight.

**What it picks:** queued outbox rows whose lead has never been sent anything --
a first contact -- with no PDF on disk yet. Follow-ups refer back to the first
email's attachment instead of carrying their own.

**What it never does:** decide whether the email goes. A lead with nothing to
show (no findings that pass the evidence page's bar) simply gets no PDF, and
the outbox worker sends the email without one and without the sentence
announcing it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from temporalio import activity

from coldops.config import get_settings
from coldops.db.session import workspace_unit_of_work
from coldops.intelligence import audit_pdf, case_studies, evidence_page
from coldops.intelligence.composer import family_for
from coldops.providers.browser_client import BrowserWorkerClient
from coldops.workflows.types import RenderAuditPdfsInput, RenderAuditPdfsResult

logger = logging.getLogger(__name__)

#: Per run. A run every five minutes renders far more than a day's sends.
BATCH = 10


@dataclass(frozen=True)
class _Job:
    draft_id: uuid.UUID
    lead_id: uuid.UUID
    html: str


async def _candidates(workspace_id: uuid.UUID) -> list[tuple[uuid.UUID, uuid.UUID]]:
    async with workspace_unit_of_work(workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    -- Drafts still waiting on a person, as well as queued
                    -- mail. Queued alone was too late: a free mailbox sends
                    -- a queued email within seconds, long before this
                    -- five-minute sweep, so it left without its PDF. The
                    -- approval wait is the time there is to render in.
                    SELECT draft_id, lead_id FROM (
                        SELECT o.draft_id, o.lead_id, o.created_at
                          FROM outbox_messages o
                         WHERE o.workspace_id = :ws
                           AND o.status IN ('pending', 'deferred')
                           AND o.created_at > now() - interval '3 days'
                        UNION
                        SELECT d.id, d.lead_id, d.created_at
                          FROM message_drafts d
                         WHERE d.workspace_id = :ws
                           AND d.status IN ('awaiting_approval', 'approved')
                           AND d.validation_passed
                           AND d.created_at > now() - interval '3 days'
                    ) c
                     WHERE NOT EXISTS (
                           SELECT 1 FROM messages m
                            WHERE m.workspace_id = :ws
                              AND m.lead_id = c.lead_id
                              AND m.sent_at IS NOT NULL)
                     ORDER BY created_at
                     LIMIT 50
                    """
                ),
                {"ws": workspace_id},
            )
        ).all()
    return [(r.draft_id, r.lead_id) for r in rows]


async def _job(
    workspace_id: uuid.UUID, draft_id: uuid.UUID, lead_id: uuid.UUID
) -> _Job | None:
    settings = get_settings()
    async with workspace_unit_of_work(workspace_id) as session:
        page = await evidence_page.load_page(session, lead_id=lead_id)
        if page is None or page.workspace_id != workspace_id:
            return None
        cited = (
            (
                await session.execute(
                    text(
                        """
                    SELECT f.issue_type
                      FROM message_drafts d
                      CROSS JOIN LATERAL jsonb_array_elements(
                          COALESCE(d.claim_map, '[]'::jsonb)) AS c(claim)
                      JOIN audit_findings f
                        ON f.id::text = c.claim ->> 'finding_id' AND f.workspace_id = :ws
                     WHERE d.id = :draft AND d.workspace_id = :ws
                    """
                    ),
                    {"ws": workspace_id, "draft": draft_id},
                )
            )
            .scalars()
            .all()
        )
        industry = (
            await session.execute(
                text(
                    "SELECT o.industry FROM leads l JOIN organizations o "
                    "ON o.id = l.organization_id WHERE l.id = :lead AND l.workspace_id = :ws"
                ),
                {"ws": workspace_id, "lead": lead_id},
            )
        ).scalar_one_or_none()

    finding = audit_pdf.choose_finding(page, list(cited))
    if finding is None:
        return None
    study = case_studies.select(
        case_studies.registry(settings.case_studies_path),
        industry=str(industry) if industry else None,
        family=family_for(finding.issue_type),
        issue_type=finding.issue_type,
    )
    evidence_url = (
        evidence_page.evidence_url(
            settings.evidence_base_url, lead_id, settings.evidence_secret
        )
        if settings.evidence_base_url and settings.evidence_secret is not None
        else None
    )
    html = audit_pdf.build_html(
        page,
        finding=finding,
        case_study=study,
        evidence_url=evidence_url,
        sender_name=settings.owner_name,
        sender_site=str(settings.owner_portfolio_url).rstrip("/"),
    )
    return _Job(draft_id=draft_id, lead_id=lead_id, html=html)


async def render_audit_pdfs_now(request: RenderAuditPdfsInput) -> RenderAuditPdfsResult:
    settings = get_settings()
    if not settings.audit_pdf_enabled:
        return RenderAuditPdfsResult(unavailable="COLDOPS_AUDIT_PDF_ENABLED is off")
    if not settings.artifact_dir:
        return RenderAuditPdfsResult(unavailable="no artifact directory is configured")

    workspace_id = uuid.UUID(request.workspace_id)
    rendered = skipped = refused = 0
    detail: list[str] = []
    client = BrowserWorkerClient(settings)
    try:
        for draft_id, lead_id in await _candidates(workspace_id):
            if rendered + refused >= BATCH:
                break
            path = audit_pdf.pdf_path(settings.artifact_dir, draft_id)
            if path is not None and path.exists():
                continue
            job = await _job(workspace_id, draft_id, lead_id)
            if job is None:
                skipped += 1
                continue
            outcome = await client.render_pdf(
                draft_id=job.draft_id,
                html=job.html,
                max_bytes=audit_pdf.MAX_PDF_BYTES,
                max_pages=1,
            )
            if outcome.key:
                rendered += 1
            else:
                refused += 1
                detail.append(f"{draft_id}: {outcome.refused or outcome.error}")
    finally:
        await client.aclose()
    return RenderAuditPdfsResult(
        rendered=rendered, skipped=skipped, refused=refused, detail=tuple(detail[:20])
    )


@activity.defn(name="render_audit_pdfs")
async def render_audit_pdfs(request: RenderAuditPdfsInput) -> RenderAuditPdfsResult:
    return await render_audit_pdfs_now(request)


ALL_AUDIT_PDF_ACTIVITIES = [render_audit_pdfs]

__all__ = ["ALL_AUDIT_PDF_ACTIVITIES", "render_audit_pdfs", "render_audit_pdfs_now"]
