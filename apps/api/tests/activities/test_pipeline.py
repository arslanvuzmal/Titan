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


@pytest.fixture(autouse=True)
def _mx_present(monkeypatch):
    """Stub DNS for the fixture domains.

    Every domain in this file is an RFC 2606 ``.test`` name, which is reserved
    precisely so that it never resolves. Against real DNS that is NXDOMAIN, and
    the contact activity correctly disqualifies the address as undeliverable --
    which is the right production behaviour and makes every pipeline assertion
    here fail for a reason that has nothing to do with the pipeline.

    MX has its own coverage in tests/intelligence/test_mx.py, and the
    disqualification path is asserted directly in
    ``test_a_domain_that_cannot_receive_mail_is_disqualified`` below. This
    fixture keeps the rest of the file testing what it is named for.
    """
    from titan.intelligence.mx import BulkMxResult, MxCheck, MxStatus

    def _present(domains, *, resolver=None):
        return BulkMxResult(
            checks={
                domain: MxCheck(MxStatus.PRESENT, domain, hosts=("mx.fixture.test",))
                for domain in {d.strip().lower() for d in domains if d}
            }
        )

    monkeypatch.setattr("titan.activities.pipeline.check_many", _present)


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
            # Recent on purpose: authorization_errors() expires a verification
            # after MAX_VERIFICATION_AGE, so a fixture with the flags set and
            # no timestamp is the unverified identity the gate now refuses.
            last_verified_at=dt.datetime.now(dt.UTC),
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
async def test_a_domain_that_cannot_receive_mail_is_disqualified(
    db_session, workspace, monkeypatch
) -> None:
    """The MX gate, asserted directly because the autouse fixture stubs it off.

    A domain publishing no route for inbound mail hard-bounces every address at
    it, and a bounce costs sender reputation on every attempt. This is the one
    direction MX is allowed to act in: a *positive* result never upgrades
    verification_status, because MX proves a domain accepts mail and says
    nothing about whether a mailbox exists.
    """
    from titan.intelligence.mx import BulkMxResult, MxCheck, MxStatus

    def _absent(domains, *, resolver=None):
        return BulkMxResult(
            checks={
                domain: MxCheck(MxStatus.NXDOMAIN, domain, detail="NXDOMAIN")
                for domain in {d.strip().lower() for d in domains if d}
            }
        )

    monkeypatch.setattr("titan.activities.pipeline.check_many", _absent)

    ids = await seed_lead(workspace, suffix="nomx")
    out = await run_pipeline(workspace, ids, payload=crawl_payload(), run_key="nomx-1")

    assert out["contact"].eligible_channel_id is None
    assert any(
        "cannot receive mail" in reason for reason in out["contact"].rejected_reasons
    )


@pytest.mark.asyncio
async def test_a_resolver_failure_is_not_a_disqualifier(
    db_session, workspace, monkeypatch
) -> None:
    """An unreachable resolver is our problem, not evidence about the domain.

    Treating a lookup failure as undeliverable would silently discard good leads
    whenever DNS had a bad minute, and the loss would look identical to a real
    answer.
    """

    def _explode(domains, *, resolver=None):
        raise OSError("resolver unreachable")

    monkeypatch.setattr("titan.activities.pipeline.check_many", _explode)

    ids = await seed_lead(workspace, suffix="dnsfail")
    out = await run_pipeline(workspace, ids, payload=crawl_payload(), run_key="dnsfail-1")

    assert out["contact"].eligible_channel_id is not None


def clean_payload() -> CrawlResult:
    """A well-built site: nothing for a detector to fire on."""
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
    return CrawlResult(
        request_id="r",
        status="completed",
        seed_url=f"{SITE}/",
        final_url=f"{SITE}/",
        pages=[clean],
        pages_fetched=1,
        worker_version="t",
    )


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


# ==========================================================================
# Opportunities -- the commercial roll-up of the findings
# ==========================================================================
@pytest.mark.asyncio
async def test_findings_become_persisted_opportunities(db_session, workspace) -> None:
    """``business_opportunities`` had no writer until this stage existed."""
    from titan.db.models import BusinessOpportunity, SolutionRecommendation

    ids = await seed_lead(workspace, suffix="opp")
    out = await run_pipeline(workspace, ids, payload=crawl_payload(), run_key="opp-1")

    assert out["analysis"].deliverable_opportunities >= 1
    assert out["analysis"].top_offer_key is not None

    async with get_sessionmaker()() as s:
        rows = (
            (
                await s.execute(
                    select(BusinessOpportunity).where(
                        BusinessOpportunity.research_run_id == uuid.UUID(out["run_id"])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows, "the analyse stage persisted no opportunities"

        sellable = [r for r in rows if r.deliverable]
        assert sellable
        top = max(sellable, key=lambda r: r.priority)
        # Every opportunity names the findings that justify it, by id.
        assert top.supporting_finding_ids
        assert top.estimated_value_usd is not None

        outlines = (
            (
                await s.execute(
                    select(SolutionRecommendation).where(
                        SolutionRecommendation.opportunity_id == top.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(outlines) == 1
        assert outlines[0].implementation_outline


@pytest.mark.asyncio
async def test_rerunning_analysis_replaces_rather_than_accumulates(
    db_session, workspace
) -> None:
    """An opportunity that outlives its evidence is an unfounded claim.

    Re-derivation is cheap and the previous set holds nothing the new one lacks,
    so the run's opportunities are replaced wholesale.
    """
    from titan.db.models import BusinessOpportunity

    ids = await seed_lead(workspace, suffix="oppidem")
    first = await run_pipeline(
        workspace, ids, payload=crawl_payload(), run_key="oppidem-1"
    )
    second = await run_pipeline(
        workspace, ids, payload=crawl_payload(), run_key="oppidem-1"
    )

    assert first["analysis"].opportunities_created == (
        second["analysis"].opportunities_created
    )

    async with get_sessionmaker()() as s:
        rows = (
            (
                await s.execute(
                    select(BusinessOpportunity).where(
                        BusinessOpportunity.research_run_id == uuid.UUID(first["run_id"])
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == second["analysis"].opportunities_created


@pytest.mark.asyncio
async def test_a_clean_site_produces_no_opportunity(db_session, workspace) -> None:
    """Nothing evidenced means nothing to sell, which is the correct outcome."""
    from titan.db.models import BusinessOpportunity

    ids = await seed_lead(workspace, suffix="oppclean")
    out = await run_pipeline(
        workspace, ids, payload=clean_payload(), run_key="oppclean-1"
    )

    assert out["analysis"].deliverable_opportunities == 0

    async with get_sessionmaker()() as s:
        rows = (
            (
                await s.execute(
                    select(BusinessOpportunity).where(
                        BusinessOpportunity.research_run_id == uuid.UUID(out["run_id"])
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [r for r in rows if r.deliverable] == []


# ==========================================================================
# One recipient, one pending message
# ==========================================================================
@pytest.mark.asyncio
async def test_a_second_message_to_a_waiting_recipient_is_refused(
    db_session, workspace
) -> None:
    """Found live: 17 recipients each holding two pending messages.

    The per-draft dedupe key collapses a retry of the queue activity and
    nothing else, so a re-run of research produced a second draft and a second
    outbox row to the same person in the same campaign.
    """
    ids = await seed_lead(workspace, suffix="dupe-recip")
    first = await run_pipeline(
        workspace, ids, payload=crawl_payload(), run_key="dupe-recip-1"
    )
    assert first["queued"] is not None
    assert first["queued"].queued is True

    # A different run key produces a genuinely different draft, exactly as a
    # second pass of the research pipeline would.
    second = await run_pipeline(
        workspace, ids, payload=crawl_payload(), run_key="dupe-recip-2"
    )

    assert second["queued"] is not None
    assert second["queued"].queued is False
    assert any("already queued" in reason for reason in second["queued"].refused_reasons)

    async with get_sessionmaker()() as s:
        rows = (
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
    assert len(rows) == 1, "a second message was queued to a waiting recipient"


@pytest.mark.asyncio
async def test_an_unverified_sending_domain_cannot_be_used(db_session, workspace) -> None:
    """The third delivery gate, made to mean something.

    Twenty identities were found in production claiming SPF, DKIM and DMARC on
    a domain with no DNS. Expiring the claim is what stops a flag somebody
    typed from authorising mail forever.
    """
    from titan.db.models import SenderIdentity

    ids = await seed_lead(workspace, suffix="stale-sender")

    async with get_sessionmaker()() as s, s.begin():
        sender = await s.get(SenderIdentity, uuid.UUID(ids["sender_id"]))
        # Exactly the shape of the production rows: flags true, never verified.
        sender.last_verified_at = None

    async with get_sessionmaker()() as s:
        sender = await s.get(SenderIdentity, uuid.UUID(ids["sender_id"]))
        errors = sender.authorization_errors()

    assert any("never been verified" in e for e in errors)


@pytest.mark.asyncio
async def test_the_queued_message_comes_from_the_campaigns_pool(
    db_session, workspace
) -> None:
    """The wiring proof for the sender pool.

    Selection and capacity are covered as units in
    tests/delivery/test_sender_pool.py. What that cannot show is whether
    queue_message actually asks. So the pool here holds exactly one mailbox and
    it is deliberately *not* the one on ``campaigns.sender_identity_id`` -- if
    the pool were being ignored the message would go out from the campaign's own
    sender, and the assertion below would name it.
    """
    from titan.db.models import CampaignSender, Message, SenderIdentity

    ids = await seed_lead(workspace, suffix="pool-wired")
    legacy = uuid.UUID(ids["sender_id"])

    async with get_sessionmaker()() as s, s.begin():
        source = await s.get(SenderIdentity, legacy)
        second = SenderIdentity(
            workspace_id=workspace,
            label="second",
            from_email=f"second-{uuid.uuid4().hex[:8]}@{source.sending_domain}",
            from_name=source.from_name,
            reply_to_email=source.reply_to_email,
            sending_domain=source.sending_domain,
            domain_verified=True,
            spf_ok=True,
            dkim_ok=True,
            dmarc_ok=True,
            last_verified_at=source.last_verified_at,
            daily_send_limit=source.daily_send_limit,
            mailing_address=source.mailing_address,
        )
        s.add(second)
        await s.flush()
        pool_only = second.id
        s.add(
            CampaignSender(
                workspace_id=workspace,
                campaign_id=uuid.UUID(ids["campaign_id"]),
                sender_identity_id=pool_only,
            )
        )

    result = await run_pipeline(
        workspace, ids, payload=crawl_payload(), run_key="pool-wired-run"
    )

    assert result["queued"] is not None and result["queued"].queued, (
        result["queued"].refused_reasons if result["queued"] else None
    )
    async with get_sessionmaker()() as s:
        message = (
            await s.execute(
                select(Message).where(
                    Message.campaign_id == uuid.UUID(ids["campaign_id"])
                )
            )
        ).scalar_one()

    assert message.sender_identity_id == pool_only
    assert message.sender_identity_id != legacy, "the pool was ignored"


@pytest.mark.asyncio
async def test_a_campaign_whose_whole_pool_is_full_queues_nothing(
    db_session, workspace
) -> None:
    """The refusal names the mailboxes rather than saying "no capacity", because
    the fix differs per mailbox: one waits for tomorrow, another for a DNS
    record."""
    from titan.db.models import SenderIdentity

    ids = await seed_lead(workspace, suffix="pool-full")

    async with get_sessionmaker()() as s, s.begin():
        sender = await s.get(SenderIdentity, uuid.UUID(ids["sender_id"]))
        sender.daily_send_limit = 0

    result = await run_pipeline(
        workspace, ids, payload=crawl_payload(), run_key="pool-full-run"
    )

    queued = result["queued"]
    assert queued is not None and queued.queued is False
    assert any("no mailbox" in r for r in queued.refused_reasons), queued.refused_reasons
    assert any("no daily send limit" in r for r in queued.refused_reasons)


# ==========================================================================
# An offer has to match the evidence, or there is no message
# ==========================================================================
@pytest.mark.asyncio
async def test_evidence_with_no_matching_offer_produces_no_draft(
    db_session, workspace, monkeypatch
) -> None:
    """The fallback this replaces was a capability claim nobody checked.

    When a lead's evidence matched nothing in its industry's playbook, the
    message still went out saying "I build enquiry capture and follow-up
    automation" -- directly after showing the recipient a broken booking
    button. Two sentences about unrelated things, which is the specific kind of
    small wrong that reads as a template rather than a person.

    ``select_offers`` already documents an empty result as "there is nothing
    truthful to offer". Refusing is agreeing with it.
    """
    monkeypatch.setattr("titan.activities.pipeline.select_offers", lambda *a, **k: [])

    ids = await seed_lead(workspace, suffix="nooffer")
    out = await run_pipeline(workspace, ids, payload=crawl_payload(), run_key="nooffer-1")

    draft = out["draft"]
    assert draft is not None, "the run stopped before drafting; the test proves nothing"
    assert draft.validation_passed is False
    assert draft.draft_id == ""
    assert "no_offer_matching_the_evidence" in draft.violation_codes
    assert out["queued"] is None, "a refused draft must never reach the outbox"


@pytest.mark.asyncio
async def test_a_matching_offer_still_drafts(db_session, workspace) -> None:
    """The other half. A refusal that fired on everything would be a pause
    wearing a validator's name, so the contrast is asserted rather than
    assumed."""
    ids = await seed_lead(workspace, suffix="hasoffer")
    out = await run_pipeline(
        workspace, ids, payload=crawl_payload(), run_key="hasoffer-1"
    )

    draft = out["draft"]
    assert draft is not None
    assert "no_offer_matching_the_evidence" not in (draft.violation_codes or ())


# ==========================================================================
# Follow-ups: the same pipeline, one step further, and never the same evidence
# ==========================================================================
@pytest.mark.asyncio
async def test_each_follow_up_leads_with_evidence_no_earlier_step_used(
    db_session, workspace
) -> None:
    """Mission section 13, enforced where the message is actually made.

    The cheapest way to violate "each step contributes new evidence" is to
    compose step 2 from the same headline finding as step 1 and change only the
    opener -- the first message again in different words.

    Composing follow-ups until they run out proves both halves at once: every
    draft cites a finding none before it did, and the moment there is nothing
    new to say the pipeline refuses instead of repeating itself.
    """
    from titan.activities import pipeline

    ids = await seed_lead(workspace, suffix="followup")
    first = await run_pipeline(
        workspace, ids, payload=crawl_payload(), run_key="followup-1"
    )
    assert first["draft"] is not None
    assert first["draft"].draft_id, "the opener did not draft; the test proves nothing"

    cited: list[set[str]] = []
    refusal = None
    for step in range(1, 8):
        result = await pipeline.generate_draft(
            DraftActivityInput(
                workspace_id=str(workspace),
                lead_id=ids["lead_id"],
                campaign_id=ids["campaign_id"],
                research_run_id=first["run_id"],
                contact_channel_id=first["contact"].eligible_channel_id,
                idempotency_key=f"followup-step{step}",
                template_key="outreach_v2_followup1",
                step_number=step,
            )
        )
        if not result.draft_id:
            refusal = result
            break
        from titan.db.models import MessageDraft

        async with get_sessionmaker()() as session:
            draft = await session.get(MessageDraft, uuid.UUID(result.draft_id))
            cited.append(
                {str(c["finding_id"]) for c in draft.claim_map if c.get("finding_id")}
            )

    assert refusal is not None, "follow-ups never ran out; the refusal is untested"
    assert "no_unused_evidence_for_a_follow_up" in refusal.violation_codes

    # No finding appears in two steps. Overlap anywhere is the rule failing.
    seen: set[str] = set()
    for step_findings in cited:
        assert not (seen & step_findings), "a follow-up repeated earlier evidence"
        seen |= step_findings


@pytest.mark.asyncio
async def test_the_opener_is_not_subject_to_the_new_evidence_rule(
    db_session, workspace
) -> None:
    """Step 0 has nothing prior to differ from.

    Applying the rule to the first message would make every lead undraftable,
    so the contrast is asserted rather than assumed.
    """
    ids = await seed_lead(workspace, suffix="opener")
    out = await run_pipeline(workspace, ids, payload=crawl_payload(), run_key="opener-1")

    draft = out["draft"]
    assert draft is not None
    assert "no_unused_evidence_for_a_follow_up" not in (draft.violation_codes or ())
