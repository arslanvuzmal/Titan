"""End-to-end pipeline tests against the real database.

These run the six activities in sequence with a stubbed browser worker, so the
whole chain -- evidence in, outbox row out -- is exercised for real: real
inserts, real constraints, real immutability triggers, real suppression checks.

The browser worker is stubbed rather than run because the crawl itself is
covered by the TypeScript suite; what these prove is that Titan correctly turns
a CrawlResult into findings, a score, a contact, a validated draft, and an
outbox row -- and refuses at each gate when it should.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from titan.config import OperatingMode
from titan.contracts.evidence import (
    CrawlResult,
    CtaObservation,
    FormObservation,
    PageEvidence,
)
from titan.db.enums import (
    CampaignStatus,
    Industry,
    LeadStatus,
    SuppressionReason,
)
from titan.db.models import (
    AuditFinding,
    Campaign,
    CampaignPolicy,
    FindingEvidence,
    Lead,
    LeadScore,
    OutboxMessage,
    Page,
    ResearchRun,
    SenderIdentity,
)
from titan.db.session import get_sessionmaker
from titan.delivery.suppression import suppress
from titan.workflows.types import (
    AnalyseActivityInput,
    ContactActivityInput,
    CrawlActivityInput,
    DraftActivityInput,
    QueueActivityInput,
    ResearchLeadInput,
    ScoreActivityInput,
)

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 8, 3, 12, 0, tzinfo=dt.UTC)
SITE = "https://harborline-legal-fixture.test"


def crawl_payload(*, broken_cta: bool = True, with_email: bool = True) -> CrawlResult:
    """A site with a deliberately broken primary CTA and a published address."""
    home = PageEvidence(
        url=f"{SITE}/",
        final_url=f"{SITE}/",
        depth=0,
        http_status=200,
        content_type="text/html",
        title="Harborline Legal",
        meta_description="A fictional firm.",
        has_viewport_meta=False,
        image_count=4,
        images_missing_alt=3,
        visible_phones=["+15550100"],
        visible_emails=(
            ["enquiries@harborline-legal-fixture.test"] if with_email else []
        ),
        ctas=[
            CtaObservation(
                selector="a[data-testid='consultation-cta']",
                text="Book a free consultation",
                href=f"{SITE}/blank",
                target_status=404 if broken_cta else 200,
                target_is_empty=None,
            )
        ],
        forms=[
            FormObservation(
                selector="form#contact",
                field_count=11,
                field_names=[f"f{i}" for i in range(11)],
                has_submit=True,
            )
        ],
        captured_at=NOW,
    )
    return CrawlResult(
        request_id="req-1",
        status="completed",
        seed_url=f"{SITE}/",
        final_url=f"{SITE}/",
        pages=[home],
        pages_fetched=1,
        worker_version="test",
    )


async def seed_lead(workspace_id: uuid.UUID, *, suffix: str) -> dict:
    """A campaign, organization and lead ready for research."""
    from titan.db.models import Organization

    async with get_sessionmaker()() as session, session.begin():
        sender = SenderIdentity(
            workspace_id=workspace_id,
            label="primary",
            from_email=f"arslan-{suffix}@mail.arslanvuzmallone.dev",
            from_name="Arslan Vuzmal Lone",
            reply_to_email=f"arslan-{suffix}@mail.arslanvuzmallone.dev",
            sending_domain="mail.arslanvuzmallone.dev",
            domain_verified=True,
            spf_ok=True,
            dkim_ok=True,
            dmarc_ok=True,
            mailing_address="12 Fictional Row, Testville",
            unsubscribe_mailto="mailto:unsub@mail.arslanvuzmallone.dev",
        )
        campaign = Campaign(
            workspace_id=workspace_id,
            name=f"Law firms {suffix}",
            slug=f"law-{suffix}",
            status=CampaignStatus.ACTIVE,
            industry=Industry.LAW_FIRM,
        )
        org = Organization(
            workspace_id=workspace_id,
            display_name="Harborline Legal",
            normalized_name="harborline legal",
            canonical_domain="harborline-legal-fixture.test",
            website_url=f"{SITE}/",
            industry=Industry.LAW_FIRM,
            rating=4.7,
            review_count=120,
            business_status="OPERATIONAL",
        )
        session.add_all([sender, campaign, org])
        await session.flush()
        campaign.sender_identity_id = sender.id

        session.add(
            CampaignPolicy(
                workspace_id=workspace_id,
                campaign_id=campaign.id,
                operating_mode=OperatingMode.CONTROLLED_AUTOPILOT,
                sending_authorized=True,
                min_lead_score=55,
                require_verified_email=True,
            )
        )
        lead = Lead(
            workspace_id=workspace_id,
            campaign_id=campaign.id,
            organization_id=org.id,
            status=LeadStatus.DISCOVERED,
        )
        session.add(lead)
        await session.flush()
        return {
            "campaign_id": str(campaign.id),
            "lead_id": str(lead.id),
            "org_id": str(org.id),
            "sender_id": str(sender.id),
        }


async def run_pipeline(
    workspace_id: uuid.UUID, ids: dict, *, payload: CrawlResult, run_key: str
) -> dict:
    """Drive the six activities directly, with the browser worker stubbed."""
    from unittest.mock import AsyncMock, patch

    from titan.activities import pipeline, research

    request = ResearchLeadInput(
        workspace_id=str(workspace_id),
        campaign_id=ids["campaign_id"],
        lead_id=ids["lead_id"],
        run_key=run_key,
        seed_url=f"{SITE}/",
    )

    with patch.object(pipeline, "activity") as fake_activity:
        fake_activity.info.return_value.workflow_id = f"wf-{run_key}"
        fake_activity.heartbeat = lambda *a, **k: None
        with patch.object(research, "activity") as ra:
            ra.info.return_value.workflow_id = f"wf-{run_key}"
            ra.info.return_value.activity_id = "act-1"
            ra.info.return_value.attempt = 1
            run_id = await research.open_research_run(request)

        with patch("titan.activities.pipeline.BrowserWorkerClient") as ClientCls:
            instance = ClientCls.return_value
            instance.research = AsyncMock(return_value=payload)
            instance.aclose = AsyncMock()
            crawl = await pipeline.crawl_lead_website(
                CrawlActivityInput(
                    workspace_id=str(workspace_id),
                    lead_id=ids["lead_id"],
                    research_run_id=run_id,
                    seed_url=f"{SITE}/",
                    idempotency_key=f"{run_key}:crawl",
                )
            )

        analysis = await pipeline.analyse_evidence(
            AnalyseActivityInput(
                workspace_id=str(workspace_id),
                lead_id=ids["lead_id"],
                research_run_id=run_id,
                crawl_run_id=crawl.crawl_run_id,
                idempotency_key=f"{run_key}:analyse",
            )
        )
        contact = await pipeline.resolve_contact(
            ContactActivityInput(
                workspace_id=str(workspace_id),
                lead_id=ids["lead_id"],
                campaign_id=ids["campaign_id"],
                research_run_id=run_id,
                idempotency_key=f"{run_key}:contact",
            )
        )
        score = await pipeline.score_lead(
            ScoreActivityInput(
                workspace_id=str(workspace_id),
                lead_id=ids["lead_id"],
                campaign_id=ids["campaign_id"],
                research_run_id=run_id,
                idempotency_key=f"{run_key}:score",
            )
        )
        draft = None
        queued = None
        if contact.eligible_channel_id:
            draft = await pipeline.generate_draft(
                DraftActivityInput(
                    workspace_id=str(workspace_id),
                    lead_id=ids["lead_id"],
                    campaign_id=ids["campaign_id"],
                    research_run_id=run_id,
                    contact_channel_id=contact.eligible_channel_id,
                    idempotency_key=f"{run_key}:draft",
                )
            )
            if draft.validation_passed:
                queued = await pipeline.queue_message(
                    QueueActivityInput(
                        workspace_id=str(workspace_id),
                        draft_id=draft.draft_id,
                        approval_id=None,
                        idempotency_key=f"{run_key}:queue",
                    )
                )

    return {
        "run_id": run_id,
        "crawl": crawl,
        "analysis": analysis,
        "contact": contact,
        "score": score,
        "draft": draft,
        "queued": queued,
    }


# ==========================================================================
# The full chain
# ==========================================================================
@pytest.mark.asyncio
async def test_evidence_becomes_a_queued_message(db_session, workspace) -> None:
    """Discovery -> evidence -> finding -> score -> contact -> draft -> outbox."""
    ids = await seed_lead(workspace, suffix="e2e")
    out = await run_pipeline(workspace, ids, payload=crawl_payload(), run_key="e2e-1")

    assert out["crawl"].pages_captured == 1
    assert out["analysis"].pitchable_findings >= 1
    assert out["contact"].eligible_channel_id is not None
    assert out["score"].total > 0
    assert out["draft"].validation_passed, out["draft"].violation_codes
    assert out["queued"].queued is True

    async with get_sessionmaker()() as s:
        outbox = (
            await s.execute(
                select(OutboxMessage).where(
                    OutboxMessage.id == uuid.UUID(out["queued"].outbox_id)
                )
            )
        ).scalar_one()
        assert outbox.provider_idempotency_key
        assert outbox.payload["subject"]
        # Nothing was sent: the row is pending for the outbox worker.
        assert outbox.status.value == "pending"
        assert outbox.sent_at is None


@pytest.mark.asyncio
async def test_the_message_cites_real_stored_evidence(db_session, workspace) -> None:
    """Invariant 7 through the whole chain, not just in the validator."""
    ids = await seed_lead(workspace, suffix="cite")
    out = await run_pipeline(workspace, ids, payload=crawl_payload(), run_key="cite-1")

    async with get_sessionmaker()() as s:
        draft_row = (
            await s.execute(
                select(OutboxMessage).where(
                    OutboxMessage.id == uuid.UUID(out["queued"].outbox_id)
                )
            )
        ).scalar_one()
        from titan.db.models import MessageDraft

        draft = await s.get(MessageDraft, draft_row.draft_id)
        assert draft.claim_map, "no claim map"
        claim = draft.claim_map[0]

        finding = await s.get(AuditFinding, uuid.UUID(claim["finding_id"]))
        assert finding is not None, "claim cites a finding that does not exist"

        evidence = (
            (
                await s.execute(
                    select(FindingEvidence).where(
                        FindingEvidence.finding_id == finding.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert evidence, "cited finding has no evidence rows"
        # The sentence in the body is the sentence in the claim map.
        assert claim["sentence"] in draft.body_text


@pytest.mark.asyncio
async def test_detectors_find_the_planted_defects(db_session, workspace) -> None:
    ids = await seed_lead(workspace, suffix="det")
    await run_pipeline(workspace, ids, payload=crawl_payload(), run_key="det-1")

    async with get_sessionmaker()() as s:
        types = {
            f.issue_type
            for f in (
                await s.execute(
                    select(AuditFinding).where(
                        AuditFinding.lead_id == uuid.UUID(ids["lead_id"])
                    )
                )
            ).scalars()
        }
    assert "broken_primary_cta" in types
    assert "missing_mobile_viewport" in types
    assert "high_friction_contact_form" in types


@pytest.mark.asyncio
async def test_score_is_persisted_with_its_explanation(db_session, workspace) -> None:
    ids = await seed_lead(workspace, suffix="score")
    await run_pipeline(workspace, ids, payload=crawl_payload(), run_key="score-1")

    async with get_sessionmaker()() as s:
        row = (
            await s.execute(
                select(LeadScore).where(LeadScore.lead_id == uuid.UUID(ids["lead_id"]))
            )
        ).scalar_one()
    assert 0 <= row.total <= 100
    assert row.components, "score has no component breakdown"
    assert row.policy_version
    assert row.reasons


# ==========================================================================
# Idempotency
# ==========================================================================
@pytest.mark.asyncio
async def test_rerunning_the_pipeline_creates_no_duplicates(
    db_session, workspace
) -> None:
    """A retried activity must find its own prior work."""
    ids = await seed_lead(workspace, suffix="idem")
    first = await run_pipeline(workspace, ids, payload=crawl_payload(), run_key="idem-1")
    second = await run_pipeline(workspace, ids, payload=crawl_payload(), run_key="idem-1")

    assert first["run_id"] == second["run_id"], "a second research run was created"
    assert first["crawl"].crawl_run_id == second["crawl"].crawl_run_id
    assert first["draft"].draft_id == second["draft"].draft_id
    assert first["queued"].outbox_id == second["queued"].outbox_id

    async with get_sessionmaker()() as s:
        outbox_count = len(
            (
                await s.execute(
                    select(OutboxMessage).where(
                        OutboxMessage.lead_id == uuid.UUID(ids["lead_id"])
                    )
                )
            )
            .scalars()
            .all()
        )
        page_count = len(
            (
                await s.execute(
                    select(Page).where(
                        Page.crawl_run_id == uuid.UUID(first["crawl"].crawl_run_id)
                    )
                )
            )
            .scalars()
            .all()
        )
    assert outbox_count == 1, "the pipeline queued a duplicate message"
    assert page_count == 1, "the pipeline stored duplicate evidence"


# ==========================================================================
# Refusals
# ==========================================================================
@pytest.mark.asyncio
async def test_no_published_address_means_no_draft(db_session, workspace) -> None:
    """Invariant 6: Titan does not invent an address when none is published."""
    ids = await seed_lead(workspace, suffix="noaddr")
    out = await run_pipeline(
        workspace,
        ids,
        payload=crawl_payload(with_email=False),
        run_key="noaddr-1",
    )
    assert out["contact"].eligible_channel_id is None
    assert out["contact"].rejected_reasons
    assert out["draft"] is None


@pytest.mark.asyncio
async def test_suppressed_recipient_is_never_queued(db_session, workspace) -> None:
    ids = await seed_lead(workspace, suffix="supp")
    async with get_sessionmaker()() as s, s.begin():
        await suppress(
            s,
            workspace_id=workspace,
            email_or_domain="enquiries@harborline-legal-fixture.test",
            reason=SuppressionReason.UNSUBSCRIBE,
            source="test",
            now=NOW,
        )
    out = await run_pipeline(workspace, ids, payload=crawl_payload(), run_key="supp-1")
    assert out["contact"].eligible_channel_id is None
    assert any("suppress" in r for r in out["contact"].rejected_reasons)


@pytest.mark.asyncio
async def test_a_clean_site_produces_no_pitchable_finding(db_session, workspace) -> None:
    """The false-positive control, end to end."""
    clean = PageEvidence(
        url=f"{SITE}/",
        final_url=f"{SITE}/",
        depth=0,
        http_status=200,
        title="Clean",
        meta_description="A well-built site.",
        has_viewport_meta=True,
        image_count=3,
        images_missing_alt=0,
        visible_phones=["+15550100"],
        visible_emails=["hello@harborline-legal-fixture.test"],
        forms=[FormObservation(selector="form", field_count=3, has_submit=True)],
        structured_data_types=["LegalService"],
        captured_at=NOW,
    )
    payload = CrawlResult(
        request_id="r",
        status="completed",
        seed_url=f"{SITE}/",
        final_url=f"{SITE}/",
        pages=[clean],
        pages_fetched=1,
        worker_version="t",
    )
    ids = await seed_lead(workspace, suffix="clean")
    out = await run_pipeline(workspace, ids, payload=payload, run_key="clean-1")

    assert out["analysis"].pitchable_findings == 0
    assert out["draft"] is None or not out["draft"].validation_passed


@pytest.mark.asyncio
async def test_research_run_records_the_policy_snapshot(db_session, workspace) -> None:
    """A later policy edit must not rewrite how a past run is read."""
    ids = await seed_lead(workspace, suffix="snap")
    out = await run_pipeline(workspace, ids, payload=crawl_payload(), run_key="snap-1")
    async with get_sessionmaker()() as s:
        run = await s.get(ResearchRun, uuid.UUID(out["run_id"]))
    assert run.playbook_snapshot.get("min_lead_score") == 55
    assert run.workflow_id
