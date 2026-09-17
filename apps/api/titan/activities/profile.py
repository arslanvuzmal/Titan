"""Reading a business's Google listing, when there is no website to read.

The research pipeline's first step is a crawl, and everything after it assumes
the crawl produced pages. For a business whose only web presence is a Facebook
page that step has nothing to do: crawling the profile would audit Facebook's
markup, and every finding would be about somebody else's site.

This activity stands in for the crawl on those leads. It reads the Google
Business Profile through the Places API -- not by scraping, see
:mod:`titan.intelligence.profile_defects` for the robots boundary and why it
matters to a system that attaches a one-pager saying "robots.txt obeyed" to
every message -- and writes what the listing shows to be missing as findings,
in the same table and the same shape as a crawl's.

It is deliberately a separate activity rather than a branch inside
``crawl_lead_website``. The crawl activity holds the browser worker's lane; this
one makes a single HTTP request and needs neither the lane nor the timeout, and
a siteless lead queueing behind a crawler it will never use is the kind of thing
nobody notices until the queue is the constraint.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from temporalio import activity

from titan.config import get_settings
from titan.contracts.evidence import fingerprint
from titan.db.models import AuditFinding, FindingEvidence
from titan.db.session import workspace_unit_of_work
from titan.intelligence.profile_defects import (
    findings_from_profile,
    snapshot_from_places,
)
from titan.providers.places import GooglePlacesProvider

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True, slots=True)
class ReadProfileInput:
    workspace_id: str
    lead_id: str
    research_run_id: str
    idempotency_key: str


@dataclasses.dataclass(frozen=True, slots=True)
class ReadProfileResult:
    status: str
    findings_created: int
    listing_url: str | None
    reason: str | None = None


@activity.defn(name="read_business_profile")
async def read_business_profile(request: ReadProfileInput) -> ReadProfileResult:
    """Write what this business's Google listing does not contain.

    Returns ``no_evidence`` rather than failing when the listing cannot be read
    or says nothing measurable. A profile nobody could read is not a profile
    with nothing in it, and the difference decides whether the lead is written
    to or left alone.
    """
    workspace_id = uuid.UUID(request.workspace_id)
    lead_id = uuid.UUID(request.lead_id)

    async with workspace_unit_of_work(workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT o.google_place_id
                      FROM leads l
                      JOIN organizations o ON o.id = l.organization_id
                     WHERE l.id = :lead AND l.workspace_id = :ws
                    """
                ),
                {"lead": lead_id, "ws": workspace_id},
            )
        ).first()

    place_id = (row[0] if row else None) or ""
    if not place_id:
        # Nothing to ask Places about. Not a failure -- this lead came from
        # somewhere other than discovery.
        return ReadProfileResult("no_evidence", 0, None, "no google_place_id on file")

    settings = get_settings()
    provider = GooglePlacesProvider.from_settings(settings)
    try:
        payload = await provider.get_profile(place_id)
    except Exception as err:
        logger.warning(
            "could not read the business profile",
            extra={"lead_id": str(lead_id), "error": str(err)[:200]},
        )
        return ReadProfileResult("failed", 0, None, str(err)[:200])

    if not payload:
        return ReadProfileResult("no_evidence", 0, None, "places returned nothing")

    snapshot = snapshot_from_places(payload, place_id=place_id)
    findings = findings_from_profile(
        snapshot, pitchable=settings.absence_pitching_enabled
    )
    if not findings:
        # A listing with nothing missing is a real answer, and a good one.
        return ReadProfileResult("no_evidence", 0, snapshot.listing_url or None)

    created = 0
    async with workspace_unit_of_work(workspace_id) as session:
        for finding in findings:
            inserted = await session.execute(
                pg_insert(AuditFinding.__table__)  # type: ignore[arg-type]
                .values(
                    workspace_id=workspace_id,
                    research_run_id=uuid.UUID(request.research_run_id),
                    lead_id=lead_id,
                    # No page row: the listing is not a page Titan crawled, and
                    # inventing one would put Google's URL in the evidence
                    # table as though it were the recipient's.
                    page_id=None,
                    category=finding.category.value,
                    issue_type=finding.issue_type,
                    title=finding.title,
                    page_url=finding.page_url,
                    selector=finding.selector,
                    observed_value=finding.observed_value,
                    expected_behavior=finding.expected_behavior,
                    severity=finding.severity.value,
                    confidence=finding.confidence,
                    business_impact=finding.business_impact,
                    recommended_solution=finding.recommended_solution,
                    estimated_effort=finding.estimated_effort,
                    verification_method=finding.verification_method.value,
                    finding_fingerprint=finding.fingerprint,
                )
                .on_conflict_do_nothing(
                    index_elements=["research_run_id", "finding_fingerprint"]
                )
                .returning(AuditFinding.__table__.c.id)
            )
            finding_id = inserted.scalar_one_or_none()
            if finding_id is None:
                continue
            created += 1
            for excerpt, source_url in finding.evidence:
                await session.execute(
                    pg_insert(FindingEvidence.__table__)  # type: ignore[arg-type]
                    .values(
                        workspace_id=workspace_id,
                        finding_id=finding_id,
                        page_id=None,
                        excerpt=excerpt,
                        excerpt_fingerprint=fingerprint({"e": excerpt, "u": source_url}),
                        source_url=source_url,
                        captured_at=dt.datetime.now(dt.UTC),
                    )
                    .on_conflict_do_nothing()
                )

    logger.info(
        "read the business profile",
        extra={
            "lead_id": str(lead_id),
            "findings": created,
            "listing": snapshot.listing_url,
        },
    )
    return ReadProfileResult("completed", created, snapshot.listing_url or None)


#: Registered on the worker as a group, like every other activity module.
ALL_PROFILE_ACTIVITIES = [read_business_profile]

__all__ = [
    "ALL_PROFILE_ACTIVITIES",
    "ReadProfileInput",
    "ReadProfileResult",
    "read_business_profile",
]
