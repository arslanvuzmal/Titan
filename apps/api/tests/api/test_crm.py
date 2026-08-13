"""CRM read-surface tests.

A CRM that shows the wrong thing is worse than one that shows nothing, so
these check the properties an operator would actually rely on:

* a lead row identifies a *business*, not a UUID;
* counts are real counts, and change when the underlying rows change;
* a pattern-guessed address is visible but explicitly not contactable;
* filters and their totals agree;
* the whole surface is workspace-scoped and capability-gated.

Requests go through the real ASGI app against a real database.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid

import httpx
import pytest
import pytest_asyncio
from titan.config import OperatingMode
from titan.db.enums import (
    CampaignStatus,
    ContactSource,
    DraftStatus,
    Industry,
    LeadStatus,
    MessageState,
    Severity,
    VerificationMethod,
    WorkspaceRole,
)
from titan.db.models import (
    AuditFinding,
    Campaign,
    CampaignPolicy,
    Contact,
    ContactChannel,
    FindingEvidence,
    Lead,
    LeadScore,
    Message,
    MessageDraft,
    Organization,
    OrganizationDomain,
    OrganizationLocation,
    ResearchRun,
    SenderIdentity,
    Workspace,
)
from titan.db.session import get_sessionmaker

from tests.api.test_api_security import auth, make_member, slug_of, token_for

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client():
    import os

    os.environ.setdefault("TITAN_LOCAL_JWT_SECRET", "test-secret-not-for-production")
    from titan.config import get_settings

    get_settings.cache_clear()
    from titan.api.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as http_client:
        yield http_client


async def seed_crm_fixture(workspace_id: uuid.UUID, *, tag: str) -> dict:
    """A lead with everything a CRM row is supposed to display.

    Two contact channels on purpose: one first-party (contactable) and one
    pattern-guessed (never contactable), because the distinction between them
    is the thing most worth asserting.
    """
    now = dt.datetime.now(dt.UTC)
    async with get_sessionmaker()() as session, session.begin():
        campaign = Campaign(
            workspace_id=workspace_id,
            name=f"Campaign {tag}",
            slug=f"crm-{tag}-{uuid.uuid4().hex[:6]}",
            status=CampaignStatus.ACTIVE,
            industry=Industry.DENTIST,
        )
        org = Organization(
            workspace_id=workspace_id,
            display_name=f"{tag} Dental Practice",
            normalized_name=f"{tag} dental practice",
            canonical_domain=f"{tag}-dental.test",
            website_url=f"https://{tag}-dental.test/",
            industry=Industry.DENTIST,
            phone_e164="+441610000000",
            rating=4.7,
            review_count=214,
            business_status="OPERATIONAL",
            provenance=[{"source": "google_places", "fields": ["display_name"]}],
        )
        session.add_all([campaign, org])
        await session.flush()

        session.add_all(
            [
                CampaignPolicy(
                    workspace_id=workspace_id,
                    campaign_id=campaign.id,
                    operating_mode=OperatingMode.RESEARCH_ONLY,
                    require_verified_email=False,
                    allowed_contact_sources=[ContactSource.FIRST_PARTY_WEBSITE.value],
                ),
                OrganizationLocation(
                    workspace_id=workspace_id,
                    organization_id=org.id,
                    formatted_address="1 Test Street, Manchester",
                    locality="Manchester",
                    region="England",
                    country_code="GB",
                    timezone="Europe/London",
                    is_primary=True,
                ),
                OrganizationDomain(
                    workspace_id=workspace_id,
                    organization_id=org.id,
                    domain=f"{tag}-dental.test",
                    is_primary=True,
                ),
            ]
        )

        contact = Contact(
            workspace_id=workspace_id,
            organization_id=org.id,
            full_name="Practice Manager",
            role_title="Manager",
            is_decision_maker=True,
        )
        lead = Lead(
            workspace_id=workspace_id,
            campaign_id=campaign.id,
            organization_id=org.id,
            status=LeadStatus.QUALIFIED,
            latest_score=88,
        )
        session.add_all([contact, lead])
        await session.flush()

        real = f"hello-{uuid.uuid4().hex[:8]}@{tag}-dental.test"
        guessed = f"guess-{uuid.uuid4().hex[:8]}@{tag}-dental.test"
        channels = [
            ContactChannel(
                workspace_id=workspace_id,
                contact_id=contact.id,
                channel_type="email",
                value=real,
                normalized_value=real,
                value_domain=f"{tag}-dental.test",
                source=ContactSource.FIRST_PARTY_WEBSITE,
                source_url=f"https://{tag}-dental.test/contact",
                discovered_at=now,
                confidence=0.9,
            ),
            ContactChannel(
                workspace_id=workspace_id,
                contact_id=contact.id,
                channel_type="email",
                value=guessed,
                normalized_value=guessed,
                value_domain=f"{tag}-dental.test",
                source=ContactSource.PATTERN_GUESS,
                discovered_at=now,
                confidence=0.2,
            ),
        ]
        session.add_all(channels)

        # A finding must belong to a run: the schema refuses an orphan
        # observation, which is what keeps evidence traceable.
        run = ResearchRun(
            workspace_id=workspace_id,
            lead_id=lead.id,
            campaign_id=campaign.id,
            idempotency_key=f"run-{tag}-{uuid.uuid4().hex[:8]}",
            status="completed",
            started_at=now,
            finished_at=now,
            pages_crawled=3,
            findings_count=1,
        )
        session.add(run)
        await session.flush()

        finding = AuditFinding(
            workspace_id=workspace_id,
            research_run_id=run.id,
            lead_id=lead.id,
            category="conversion",
            issue_type="no_online_booking",
            title="No online booking on the homepage",
            page_url=f"https://{tag}-dental.test/",
            severity=Severity.HIGH,
            confidence=0.95,
            verification_method=VerificationMethod.DOM_ASSERTION,
            finding_fingerprint=f"no_online_booking:{tag}-dental.test:/",
        )
        session.add(finding)
        await session.flush()
        session.add(
            FindingEvidence(
                workspace_id=workspace_id,
                finding_id=finding.id,
                excerpt="no booking control found in the primary navigation",
                excerpt_fingerprint=hashlib.sha256(
                    b"no booking control found in the primary navigation"
                ).hexdigest(),
                source_url=f"https://{tag}-dental.test/",
                captured_at=now,
            )
        )
        session.add(
            LeadScore(
                workspace_id=workspace_id,
                lead_id=lead.id,
                total=88,
                band="high_priority",
                components={},
                reasons=["one pitchable finding"],
                policy_version="test",
                threshold_applied=70,
                passed_threshold=True,
            )
        )
        draft = MessageDraft(
            workspace_id=workspace_id,
            lead_id=lead.id,
            campaign_id=campaign.id,
            contact_channel_id=channels[0].id,
            idempotency_key=f"crm-{tag}-{uuid.uuid4().hex[:6]}",
            status=DraftStatus.AWAITING_APPROVAL,
            subject="One thing on your booking page",
            body_text="Body",
            claim_map=[],
            validation_passed=True,
            template_key="first_observation",
        )
        session.add(draft)
        await session.flush()

        return {
            "campaign_id": campaign.id,
            "lead_id": lead.id,
            "organization_id": org.id,
            "finding_id": finding.id,
            "real_email": real,
            "guessed_email": guessed,
            "draft_id": draft.id,
            "org_name": org.display_name,
        }


@pytest_asyncio.fixture
async def crm(client, workspace):
    """A seeded lead plus an analyst token for its workspace."""
    data = await seed_crm_fixture(workspace, tag="alpha")
    _, email = await make_member(workspace, WorkspaceRole.RESEARCHER, tag="crm")
    data["token"] = await token_for(client, email, await slug_of(workspace))
    return data


# ==========================================================================
# The lead list is about businesses, not identifiers
# ==========================================================================
@pytest.mark.asyncio
async def test_lead_list_identifies_the_business(client, crm) -> None:
    response = await client.get("/api/v1/leads", headers=auth(crm["token"]))
    assert response.status_code == 200, response.text
    row = next(r for r in response.json()["items"] if r["id"] == str(crm["lead_id"]))

    org = row["organization"]
    assert org is not None, "a lead row without its business is unusable"
    assert org["display_name"] == crm["org_name"]
    assert org["canonical_domain"] == "alpha-dental.test"
    assert org["rating"] == 4.7
    assert org["review_count"] == 214
    assert org["locality"] == "Manchester"
    assert org["country_code"] == "GB"
    assert row["campaign_name"].startswith("Campaign ")


@pytest.mark.asyncio
async def test_counts_are_real_counts(client, crm) -> None:
    response = await client.get(
        f"/api/v1/leads/{crm['lead_id']}", headers=auth(crm["token"])
    )
    body = response.json()
    assert body["finding_count"] == 1
    assert body["evidence_count"] == 1
    assert body["draft_count"] == 1
    assert body["message_count"] == 0
    assert body["has_eligible_contact"] is True


@pytest.mark.asyncio
async def test_a_count_changes_when_the_underlying_rows_change(
    client, crm, workspace
) -> None:
    """Guards against a hardcoded or cached number."""
    async with get_sessionmaker()() as session, session.begin():
        sender = SenderIdentity(
            workspace_id=workspace,
            label="fixture",
            from_email="sender@titan-fixture.test",
            from_name="Fixture Sender",
            reply_to_email="sender@titan-fixture.test",
            sending_domain="titan-fixture.test",
        )
        session.add(sender)
        await session.flush()
        session.add(
            Message(
                workspace_id=workspace,
                draft_id=crm["draft_id"],
                lead_id=crm["lead_id"],
                campaign_id=crm["campaign_id"],
                sender_identity_id=sender.id,
                dedupe_key=f"m-{uuid.uuid4().hex[:8]}",
                to_email=crm["real_email"],
                to_email_normalized=crm["real_email"],
                to_domain="alpha-dental.test",
                from_email="sender@titan-fixture.test",
                subject="s",
                state=MessageState.QUEUED,
                provider="mock",
            )
        )

    response = await client.get(
        f"/api/v1/leads/{crm['lead_id']}", headers=auth(crm["token"])
    )
    assert response.json()["message_count"] == 1


# ==========================================================================
# Contact eligibility is shown, never implied
# ==========================================================================
@pytest.mark.asyncio
async def test_pattern_guessed_address_is_visible_but_not_contactable(
    client, crm
) -> None:
    response = await client.get(
        f"/api/v1/leads/{crm['lead_id']}/contacts", headers=auth(crm["token"])
    )
    assert response.status_code == 200, response.text
    channels = [c for contact in response.json() for c in contact["channels"]]
    by_value = {c["normalized_value"]: c for c in channels}

    guessed = by_value[crm["guessed_email"]]
    assert guessed["eligible_for_outreach"] is False
    assert "pattern-guessed" in (guessed["ineligibility_reason"] or "")

    real = by_value[crm["real_email"]]
    assert real["eligible_for_outreach"] is True
    assert real["ineligibility_reason"] is None
    assert real["source"] == "first_party_website"
    assert real["source_url"].endswith("/contact")


@pytest.mark.asyncio
async def test_a_suppressed_address_is_reported_as_suppressed(
    client, crm, workspace
) -> None:
    from titan.db.enums import SuppressionReason
    from titan.delivery.suppression import suppress

    async with get_sessionmaker()() as session, session.begin():
        await suppress(
            session,
            workspace_id=workspace,
            email_or_domain=crm["real_email"],
            reason=SuppressionReason.UNSUBSCRIBE,
            source="test",
        )

    response = await client.get(
        f"/api/v1/leads/{crm['lead_id']}/contacts", headers=auth(crm["token"])
    )
    channels = [c for contact in response.json() for c in contact["channels"]]
    real = next(c for c in channels if c["normalized_value"] == crm["real_email"])
    assert real["suppressed"] is True
    assert real["eligible_for_outreach"] is False
    assert "unsubscribe" in real["ineligibility_reason"]


# ==========================================================================
# Filtering
# ==========================================================================
@pytest.mark.asyncio
async def test_search_matches_the_business_name(client, crm) -> None:
    hit = await client.get("/api/v1/leads?q=alpha", headers=auth(crm["token"]))
    assert any(r["id"] == str(crm["lead_id"]) for r in hit.json()["items"])

    miss = await client.get("/api/v1/leads?q=nosuchbusiness", headers=auth(crm["token"]))
    assert miss.json()["items"] == []
    assert miss.json()["total"] == 0


@pytest.mark.asyncio
async def test_total_agrees_with_the_filtered_rows(client, crm) -> None:
    """A total computed without the filters would over-report."""
    response = await client.get(
        "/api/v1/leads?q=alpha&limit=200", headers=auth(crm["token"])
    )
    body = response.json()
    assert body["total"] == len(body["items"])


@pytest.mark.asyncio
async def test_score_and_status_filters_apply(client, crm) -> None:
    included = await client.get(
        "/api/v1/leads?min_score=80&status=qualified", headers=auth(crm["token"])
    )
    assert any(r["id"] == str(crm["lead_id"]) for r in included.json()["items"])

    excluded = await client.get("/api/v1/leads?max_score=50", headers=auth(crm["token"]))
    assert all(r["id"] != str(crm["lead_id"]) for r in excluded.json()["items"])


@pytest.mark.asyncio
async def test_an_unknown_status_is_a_client_error_not_a_500(client, crm) -> None:
    response = await client.get(
        "/api/v1/leads?status=not-a-status", headers=auth(crm["token"])
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_sort_key_is_an_allowlist(client, crm) -> None:
    ok = await client.get("/api/v1/leads?sort=created", headers=auth(crm["token"]))
    assert ok.status_code == 200
    rejected = await client.get(
        "/api/v1/leads?sort=latest_score;drop", headers=auth(crm["token"])
    )
    assert rejected.status_code == 422


# ==========================================================================
# Organization detail
# ==========================================================================
@pytest.mark.asyncio
async def test_organization_detail_includes_provenance(client, crm) -> None:
    response = await client.get(
        f"/api/v1/organizations/{crm['organization_id']}", headers=auth(crm["token"])
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["display_name"] == crm["org_name"]
    assert body["provenance"] == [{"source": "google_places", "fields": ["display_name"]}]
    assert body["domains"] == ["alpha-dental.test"]
    assert body["locations"][0]["formatted_address"] == "1 Test Street, Manchester"
    assert body["locations"][0]["timezone"] == "Europe/London"


# ==========================================================================
# Timeline
# ==========================================================================
@pytest.mark.asyncio
async def test_timeline_reports_what_actually_happened(client, crm) -> None:
    response = await client.get(
        f"/api/v1/leads/{crm['lead_id']}/timeline", headers=auth(crm["token"])
    )
    assert response.status_code == 200, response.text
    events = response.json()
    kinds = {e["kind"] for e in events}
    assert {
        "lead.discovered",
        "research.run",
        "finding.detected",
        "lead.scored",
        "draft.generated",
    } <= kinds
    # No message was ever sent for this lead, so no send event may appear.
    assert not any(k.startswith("message.") for k in kinds)

    timestamps = [e["at"] for e in events]
    assert timestamps == sorted(timestamps, reverse=True), "newest first"


# ==========================================================================
# Overview counters
# ==========================================================================
@pytest.mark.asyncio
async def test_stats_counts_are_scoped_to_the_workspace(client, crm) -> None:
    response = await client.get("/api/v1/stats", headers=auth(crm["token"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["leads_total"] == 1
    assert body["organizations_total"] == 1
    assert body["findings_total"] == 1
    assert body["evidence_total"] == 1
    assert body["contacts_total"] == 1
    # One of the two channels is pattern-guessed and never counts as reachable.
    assert body["eligible_contacts"] == 1
    assert body["leads_by_status"] == {"qualified": 1}
    assert body["leads_by_band"] == {"high_priority": 1}
    assert body["drafts_by_status"] == {"awaiting_approval": 1}


@pytest.mark.asyncio
async def test_stats_reports_sending_as_off_when_the_process_switch_is_off(
    client, crm, workspace
) -> None:
    """Invariant 1: the workspace flag alone must not read as 'we can send'."""
    async with get_sessionmaker()() as session, session.begin():
        ws = await session.get(Workspace, workspace)
        ws.sending_authorized = True

    response = await client.get("/api/v1/stats", headers=auth(crm["token"]))
    assert response.json()["sending_authorized"] is False


# ==========================================================================
# Isolation and authorization
# ==========================================================================
@pytest.mark.asyncio
async def test_crm_routes_are_workspace_scoped(client, crm, second_workspace) -> None:
    """A token for another tenant sees 404, not the row."""
    _, email = await make_member(second_workspace, WorkspaceRole.ADMIN, tag="other")
    other = await token_for(client, email, await slug_of(second_workspace))

    for path in (
        f"/api/v1/leads/{crm['lead_id']}/contacts",
        f"/api/v1/leads/{crm['lead_id']}/timeline",
        f"/api/v1/leads/{crm['lead_id']}/drafts",
        f"/api/v1/leads/{crm['lead_id']}/messages",
        f"/api/v1/organizations/{crm['organization_id']}",
    ):
        response = await client.get(path, headers=auth(other))
        assert response.status_code == 404, f"{path} leaked to another workspace"

    listing = await client.get("/api/v1/leads", headers=auth(other))
    assert listing.json()["total"] == 0


@pytest.mark.asyncio
async def test_crm_routes_require_authentication(client, crm) -> None:
    for path in (
        "/api/v1/stats",
        f"/api/v1/leads/{crm['lead_id']}/contacts",
        f"/api/v1/leads/{crm['lead_id']}/timeline",
        f"/api/v1/organizations/{crm['organization_id']}",
    ):
        assert (await client.get(path)).status_code == 401, path


@pytest.mark.asyncio
async def test_a_viewer_can_read_the_crm_but_not_act_on_it(
    client, crm, workspace
) -> None:
    """Capability gating differs per route; the CRM must respect it."""
    _, email = await make_member(workspace, WorkspaceRole.VIEWER, tag="viewer")
    token = await token_for(client, email, await slug_of(workspace))

    for path in (
        "/api/v1/stats",
        f"/api/v1/leads/{crm['lead_id']}/timeline",
        f"/api/v1/leads/{crm['lead_id']}/contacts",
        f"/api/v1/leads/{crm['lead_id']}/drafts",
    ):
        assert (await client.get(path, headers=auth(token))).status_code == 200, path

    # Reading is not deciding. A viewer holds no approval capability.
    denied = await client.post(
        f"/api/v1/drafts/{uuid.uuid4()}/decision",
        headers=auth(token),
        json={"decision": "approved", "draft_version": 1},
    )
    assert denied.status_code == 403


# ==========================================================================
# Outcomes: opportunities and meetings
#
# Both tables were filled by the pipeline and had no reader. These check the
# two rules that make them safe to display: a gap is never priced, and a
# meeting never arrives with a time Titan invented.
# ==========================================================================
async def seed_outcomes(workspace_id: uuid.UUID, crm: dict) -> None:
    from titan.db.models import BusinessOpportunity, ResearchRun
    from titan.db.models.ops import Meeting

    async with get_sessionmaker()() as session, session.begin():
        run = ResearchRun(
            workspace_id=workspace_id,
            lead_id=crm["lead_id"],
            campaign_id=crm["campaign_id"],
            status="completed",
            idempotency_key=f"outcomes-{uuid.uuid4().hex[:8]}",
        )
        session.add(run)
        await session.flush()

        session.add_all(
            [
                BusinessOpportunity(
                    workspace_id=workspace_id,
                    lead_id=crm["lead_id"],
                    research_run_id=run.id,
                    offer_key="booking_improvement",
                    title="Booking improvement",
                    rationale="2 evidenced findings justify this offer.",
                    supporting_finding_ids=[str(crm["finding_id"])],
                    estimated_value_usd=2600.0,
                    priority=138,
                    deliverable=True,
                ),
                BusinessOpportunity(
                    workspace_id=workspace_id,
                    lead_id=crm["lead_id"],
                    research_run_id=run.id,
                    offer_key="unserved:serious_accessibility_violations",
                    title="Unserved: accessibility violations",
                    rationale="No offer covers this.",
                    supporting_finding_ids=[],
                    estimated_value_usd=None,
                    priority=32,
                    deliverable=False,
                ),
                Meeting(
                    workspace_id=workspace_id,
                    lead_id=crm["lead_id"],
                    status="proposed",
                    scheduled_at=None,
                    notes="They wrote: could we book a call next week?",
                ),
            ]
        )


async def test_opportunities_list_the_evidence_behind_each_one(client, crm, workspace):
    await seed_outcomes(workspace, crm)

    response = await client.get("/api/v1/opportunities", headers=auth(crm["token"]))

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 2
    # Highest priority first, so the sellable one leads.
    assert rows[0]["deliverable"] is True
    assert rows[0]["organization_name"]
    assert rows[0]["supporting_finding_count"] == 1


async def test_a_gap_is_returned_but_never_priced(client, crm, workspace):
    """Attaching a number to work nobody sells would put it in a forecast."""
    await seed_outcomes(workspace, crm)

    response = await client.get(
        "/api/v1/opportunities?deliverable=false", headers=auth(crm["token"])
    )

    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["deliverable"] is False
    assert rows[0]["estimated_value_usd"] is None
    assert rows[0]["offer_key"].startswith("unserved:")


async def test_deliverable_and_gaps_are_counted_apart(client, crm, workspace):
    """A total that mixes them reads as a pipeline, and is not one."""
    await seed_outcomes(workspace, crm)

    stats = (await client.get("/api/v1/stats", headers=auth(crm["token"]))).json()

    assert stats["opportunities_deliverable"] == 1
    assert stats["opportunities_unserved"] == 1


async def test_a_meeting_is_returned_without_an_invented_time(client, crm, workspace):
    await seed_outcomes(workspace, crm)

    response = await client.get("/api/v1/meetings", headers=auth(crm["token"]))

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["scheduled_at"] is None
    assert rows[0]["status"] == "proposed"
    # The request is quoted, so an operator can set the time from what was said.
    assert "book a call" in rows[0]["notes"]


async def test_unscheduled_meetings_can_be_isolated(client, crm, workspace):
    await seed_outcomes(workspace, crm)

    response = await client.get(
        "/api/v1/meetings?unscheduled_only=true", headers=auth(crm["token"])
    )

    assert len(response.json()) == 1
    stats = (await client.get("/api/v1/stats", headers=auth(crm["token"]))).json()
    assert stats["meetings_unscheduled"] == 1


async def test_outcomes_are_workspace_scoped(client, crm, workspace, db_session):
    """The isolation every read surface must hold to."""
    from titan.db.models import Workspace

    await seed_outcomes(workspace, crm)

    async with get_sessionmaker()() as session, session.begin():
        other = Workspace(
            name="Other WS",
            slug=f"other-{uuid.uuid4().hex[:8]}",
            operating_mode=OperatingMode.RESEARCH_ONLY,
        )
        session.add(other)
        await session.flush()
        other_id = other.id

    _, email = await make_member(other_id, WorkspaceRole.RESEARCHER, tag="other")
    other_token = await token_for(client, email, await slug_of(other_id))

    response = await client.get("/api/v1/opportunities", headers=auth(other_token))

    assert response.status_code == 200
    assert response.json() == []
