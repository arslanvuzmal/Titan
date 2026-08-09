"""API security tests.

These target the specific defects the audit found in the pre-0.2 API:

* cross-workspace access that depended on each handler remembering a WHERE
  clause (C-07);
* an approvals route that queried a nonexistent table, swallowed the ownership
  check, and signalled Temporal with a caller-supplied id (C-08);
* RBAC that was defined and applied to zero routes (H-11);
* a role trusted from a JWT claim rather than the database (H-12).

Requests go through the real ASGI app with a real database.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from titan.config import OperatingMode
from titan.db.enums import CampaignStatus, DraftStatus, Industry, WorkspaceRole
from titan.db.models import (
    AuditLog,
    Campaign,
    CampaignPolicy,
    Contact,
    ContactChannel,
    Lead,
    MessageDraft,
    Organization,
    User,
    Workspace,
    WorkspaceMember,
)
from titan.db.session import get_sessionmaker

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


async def make_member(
    workspace_id: uuid.UUID, role: WorkspaceRole, *, tag: str
) -> tuple[uuid.UUID, str]:
    """Create a user with a membership; return (user_id, email)."""
    email = f"{tag}-{uuid.uuid4().hex[:8]}@titan.test"
    async with get_sessionmaker()() as session, session.begin():
        user = User(email=email, display_name=tag)
        session.add(user)
        await session.flush()
        session.add(
            WorkspaceMember(workspace_id=workspace_id, user_id=user.id, role=role)
        )
        return user.id, email


async def token_for(client: httpx.AsyncClient, email: str, slug: str) -> str:
    response = await client.post(
        "/api/v1/auth/token", json={"email": email, "workspace_slug": slug}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def slug_of(workspace_id: uuid.UUID) -> str:
    async with get_sessionmaker()() as session:
        return (await session.get(Workspace, workspace_id)).slug


async def seed_campaign_and_draft(workspace_id: uuid.UUID, *, tag: str) -> dict:
    async with get_sessionmaker()() as session, session.begin():
        campaign = Campaign(
            workspace_id=workspace_id,
            name=f"C {tag}",
            slug=f"c-{tag}-{uuid.uuid4().hex[:6]}",
            status=CampaignStatus.ACTIVE,
            industry=Industry.LAW_FIRM,
        )
        org = Organization(
            workspace_id=workspace_id,
            display_name=f"Org {tag}",
            normalized_name=f"org {tag}",
        )
        session.add_all([campaign, org])
        await session.flush()
        session.add(
            CampaignPolicy(
                workspace_id=workspace_id,
                campaign_id=campaign.id,
                operating_mode=OperatingMode.RESEARCH_ONLY,
            )
        )
        contact = Contact(workspace_id=workspace_id, organization_id=org.id)
        lead = Lead(
            workspace_id=workspace_id,
            campaign_id=campaign.id,
            organization_id=org.id,
        )
        session.add_all([contact, lead])
        await session.flush()
        address = f"a-{uuid.uuid4().hex[:8]}@example-fixture.test"
        channel = ContactChannel(
            workspace_id=workspace_id,
            contact_id=contact.id,
            channel_type="email",
            value=address,
            normalized_value=address,
            value_domain="example-fixture.test",
            source="first_party_website",
            discovered_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        )
        session.add(channel)
        await session.flush()
        draft = MessageDraft(
            workspace_id=workspace_id,
            lead_id=lead.id,
            campaign_id=campaign.id,
            contact_channel_id=channel.id,
            idempotency_key=f"draft-{tag}-{uuid.uuid4().hex[:6]}",
            status=DraftStatus.AWAITING_APPROVAL,
            subject="Subject",
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
            "draft_id": draft.id,
            "draft_version": draft.version,
        }


# ==========================================================================
# Authentication
# ==========================================================================
@pytest.mark.asyncio
async def test_unauthenticated_requests_are_rejected(client) -> None:
    for path in ("/api/v1/me", "/api/v1/campaigns", "/api/v1/leads", "/api/v1/drafts"):
        response = await client.get(path)
        assert response.status_code == 401, path


@pytest.mark.asyncio
async def test_garbage_token_is_rejected(client) -> None:
    response = await client.get("/api/v1/me", headers=auth("not-a-jwt"))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_does_not_reveal_whether_an_account_exists(client) -> None:
    response = await client.post(
        "/api/v1/auth/token",
        json={"email": "nobody@nowhere.test", "workspace_slug": "no-such-workspace"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid credentials"


@pytest.mark.asyncio
async def test_revoking_membership_invalidates_an_unexpired_token(
    client, db_session, workspace
) -> None:
    """H-12: the role is read from the database, not trusted from the token."""
    user_id, email = await make_member(workspace, WorkspaceRole.ADMIN, tag="revoke")
    token = await token_for(client, email, await slug_of(workspace))
    assert (await client.get("/api/v1/me", headers=auth(token))).status_code == 200

    async with get_sessionmaker()() as session, session.begin():
        membership = (
            await session.execute(
                select(WorkspaceMember).where(WorkspaceMember.user_id == user_id)
            )
        ).scalar_one()
        await session.delete(membership)

    # Same token, now worthless.
    assert (await client.get("/api/v1/me", headers=auth(token))).status_code == 401


@pytest.mark.asyncio
async def test_deactivating_a_user_invalidates_their_token(
    client, db_session, workspace
) -> None:
    user_id, email = await make_member(workspace, WorkspaceRole.OWNER, tag="deact")
    token = await token_for(client, email, await slug_of(workspace))

    async with get_sessionmaker()() as session, session.begin():
        user = await session.get(User, user_id)
        user.is_active = False

    assert (await client.get("/api/v1/me", headers=auth(token))).status_code == 401


# ==========================================================================
# RBAC (H-11)
# ==========================================================================
@pytest.mark.asyncio
async def test_viewer_cannot_create_a_campaign(client, db_session, workspace) -> None:
    _, email = await make_member(workspace, WorkspaceRole.VIEWER, tag="viewer")
    token = await token_for(client, email, await slug_of(workspace))

    response = await client.post(
        "/api/v1/campaigns",
        headers=auth(token),
        json={"name": "Nope", "slug": f"nope-{uuid.uuid4().hex[:6]}"},
    )
    assert response.status_code == 403
    assert "viewer" in response.json()["detail"]


@pytest.mark.asyncio
async def test_only_owner_may_enable_sending(client, db_session, workspace) -> None:
    """sending:enable belongs to owner alone -- not even admin has it."""
    ids = await seed_campaign_and_draft(workspace, tag="send")

    for role, expected in (
        (WorkspaceRole.ADMIN, 403),
        (WorkspaceRole.REVIEWER, 403),
        (WorkspaceRole.OWNER, 200),
    ):
        _, email = await make_member(workspace, role, tag=f"snd-{role.value}")
        token = await token_for(client, email, await slug_of(workspace))
        response = await client.post(
            f"/api/v1/campaigns/{ids['campaign_id']}/sending-authorization",
            headers=auth(token),
            json={
                "authorized": True,
                "acknowledgement": "I authorize production sending for this campaign",
            },
        )
        assert response.status_code == expected, f"{role.value}: {response.text}"


@pytest.mark.asyncio
async def test_enabling_sending_requires_the_exact_acknowledgement(
    client, db_session, workspace
) -> None:
    """No casual boolean can activate live outreach."""
    ids = await seed_campaign_and_draft(workspace, tag="ack")
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="ack")
    token = await token_for(client, email, await slug_of(workspace))

    response = await client.post(
        f"/api/v1/campaigns/{ids['campaign_id']}/sending-authorization",
        headers=auth(token),
        json={"authorized": True, "acknowledgement": "yes"},
    )
    assert response.status_code == 400
    assert "acknowledgement must be exactly" in response.json()["detail"]

    async with get_sessionmaker()() as session:
        policy = (
            await session.execute(
                select(CampaignPolicy).where(
                    CampaignPolicy.campaign_id == ids["campaign_id"]
                )
            )
        ).scalar_one()
    assert policy.sending_authorized is False


@pytest.mark.asyncio
async def test_researcher_cannot_decide_an_approval(
    client, db_session, workspace
) -> None:
    ids = await seed_campaign_and_draft(workspace, tag="res")
    _, email = await make_member(workspace, WorkspaceRole.RESEARCHER, tag="res")
    token = await token_for(client, email, await slug_of(workspace))

    response = await client.post(
        f"/api/v1/drafts/{ids['draft_id']}/decision",
        headers=auth(token),
        json={"decision": "approved", "draft_version": ids["draft_version"]},
    )
    assert response.status_code == 403


# ==========================================================================
# Cross-workspace isolation (C-07)
# ==========================================================================
@pytest.mark.asyncio
async def test_foreign_resources_return_404_not_403(
    client, db_session, workspace, second_workspace
) -> None:
    """404, not 403: a 403 confirms the resource exists."""
    theirs = await seed_campaign_and_draft(second_workspace, tag="theirs")
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="mine")
    token = await token_for(client, email, await slug_of(workspace))

    for path in (
        f"/api/v1/campaigns/{theirs['campaign_id']}/policy",
        f"/api/v1/leads/{theirs['lead_id']}",
        f"/api/v1/leads/{theirs['lead_id']}/findings",
        f"/api/v1/drafts/{theirs['draft_id']}",
    ):
        response = await client.get(path, headers=auth(token))
        assert response.status_code == 404, f"{path} -> {response.status_code}"


@pytest.mark.asyncio
async def test_cannot_decide_a_foreign_draft(
    client, db_session, workspace, second_workspace
) -> None:
    """The specific pre-0.2 defect: the ownership check was swallowed."""
    theirs = await seed_campaign_and_draft(second_workspace, tag="fdraft")
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="attacker")
    token = await token_for(client, email, await slug_of(workspace))

    response = await client.post(
        f"/api/v1/drafts/{theirs['draft_id']}/decision",
        headers=auth(token),
        json={"decision": "approved", "draft_version": theirs["draft_version"]},
    )
    assert response.status_code == 404

    async with get_sessionmaker()() as session:
        draft = await session.get(MessageDraft, theirs["draft_id"])
    assert draft.status is DraftStatus.AWAITING_APPROVAL, "a foreign draft was decided"


@pytest.mark.asyncio
async def test_listing_never_includes_another_workspace(
    client, db_session, workspace, second_workspace
) -> None:
    await seed_campaign_and_draft(second_workspace, tag="hidden")
    mine = await seed_campaign_and_draft(workspace, tag="visible")
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="lister")
    token = await token_for(client, email, await slug_of(workspace))

    campaigns = (await client.get("/api/v1/campaigns", headers=auth(token))).json()
    ids = {c["id"] for c in campaigns["items"]}
    assert str(mine["campaign_id"]) in ids
    assert campaigns["total"] == len(ids)

    leads = (await client.get("/api/v1/leads", headers=auth(token))).json()
    assert all(item["campaign_id"] == str(mine["campaign_id"]) for item in leads["items"])


# ==========================================================================
# Approval integrity (C-08)
# ==========================================================================
@pytest.mark.asyncio
async def test_approving_a_stale_version_is_refused(
    client, db_session, workspace
) -> None:
    """approve -> edit -> send must not bypass review."""
    ids = await seed_campaign_and_draft(workspace, tag="stale")
    _, email = await make_member(workspace, WorkspaceRole.REVIEWER, tag="stale")
    token = await token_for(client, email, await slug_of(workspace))

    async with get_sessionmaker()() as session, session.begin():
        draft = await session.get(MessageDraft, ids["draft_id"])
        draft.body_text = "Edited after the reviewer looked at it"

    response = await client.post(
        f"/api/v1/drafts/{ids['draft_id']}/decision",
        headers=auth(token),
        json={"decision": "approved", "draft_version": ids["draft_version"]},
    )
    assert response.status_code == 409
    assert "has changed since you reviewed it" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_draft_that_failed_validation_cannot_be_approved(
    client, db_session, workspace
) -> None:
    ids = await seed_campaign_and_draft(workspace, tag="invalid")
    async with get_sessionmaker()() as session, session.begin():
        draft = await session.get(MessageDraft, ids["draft_id"])
        draft.validation_passed = False

    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="invalid")
    token = await token_for(client, email, await slug_of(workspace))

    async with get_sessionmaker()() as session:
        current = await session.get(MessageDraft, ids["draft_id"])
        version = current.version

    response = await client.post(
        f"/api/v1/drafts/{ids['draft_id']}/decision",
        headers=auth(token),
        json={"decision": "approved", "draft_version": version},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_a_valid_approval_is_recorded_and_audited(
    client, db_session, workspace
) -> None:
    ids = await seed_campaign_and_draft(workspace, tag="ok")
    _, email = await make_member(workspace, WorkspaceRole.REVIEWER, tag="ok")
    token = await token_for(client, email, await slug_of(workspace))

    response = await client.post(
        f"/api/v1/drafts/{ids['draft_id']}/decision",
        headers=auth(token),
        json={"decision": "approved", "draft_version": ids["draft_version"]},
    )
    assert response.status_code == 201, response.text
    assert response.json()["decision"] == "approved"

    async with get_sessionmaker()() as session:
        entries = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.workspace_id == workspace,
                        AuditLog.action == "draft.decision",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert entries, "an approval was not audited"
    assert entries[0].entry_hash


# ==========================================================================
# Policy cannot be widened past the workspace ceiling (invariant 18)
# ==========================================================================
@pytest.mark.asyncio
async def test_campaign_cannot_exceed_the_workspace_operating_mode(
    client, db_session, workspace
) -> None:
    ids = await seed_campaign_and_draft(workspace, tag="mode")
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="mode")
    token = await token_for(client, email, await slug_of(workspace))

    # The workspace defaults to research_only.
    response = await client.patch(
        f"/api/v1/campaigns/{ids['campaign_id']}/policy",
        headers=auth(token),
        json={"operating_mode": "controlled_autopilot"},
    )
    assert response.status_code == 409
    assert "exceeds the workspace ceiling" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_new_campaign_starts_closed(client, db_session, workspace) -> None:
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="new")
    token = await token_for(client, email, await slug_of(workspace))

    created = await client.post(
        "/api/v1/campaigns",
        headers=auth(token),
        json={"name": "Fresh", "slug": f"fresh-{uuid.uuid4().hex[:6]}"},
    )
    assert created.status_code == 201
    policy = (
        await client.get(
            f"/api/v1/campaigns/{created.json()['id']}/policy", headers=auth(token)
        )
    ).json()
    assert policy["operating_mode"] == "research_only"
    assert policy["sending_authorized"] is False


@pytest.mark.asyncio
async def test_pattern_guess_is_reported_as_never_eligible(
    client, db_session, workspace
) -> None:
    _, email = await make_member(workspace, WorkspaceRole.VIEWER, tag="src")
    token = await token_for(client, email, await slug_of(workspace))
    body = (await client.get("/api/v1/contact-sources", headers=auth(token))).json()
    assert "pattern_guess" in body["never_eligible"]
    assert "pattern_guess" not in body["eligible"]


# ==========================================================================
# Idempotency
# ==========================================================================
@pytest.mark.asyncio
async def test_repeating_a_create_with_the_same_key_is_safe(
    client, db_session, workspace
) -> None:
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="idem")
    token = await token_for(client, email, await slug_of(workspace))
    slug = f"idem-{uuid.uuid4().hex[:6]}"
    headers = {**auth(token), "Idempotency-Key": "key-1"}

    first = await client.post(
        "/api/v1/campaigns", headers=headers, json={"name": "Once", "slug": slug}
    )
    second = await client.post(
        "/api/v1/campaigns", headers=headers, json={"name": "Once", "slug": slug}
    )
    assert first.status_code == 201
    assert second.json()["id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_starting_research_twice_returns_the_same_run(
    client, db_session, workspace
) -> None:
    ids = await seed_campaign_and_draft(workspace, tag="rerun")
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="rerun")
    token = await token_for(client, email, await slug_of(workspace))

    first = await client.post(
        "/api/v1/research/runs",
        headers=auth(token),
        json={"lead_id": str(ids["lead_id"])},
    )
    second = await client.post(
        "/api/v1/research/runs",
        headers=auth(token),
        json={"lead_id": str(ids["lead_id"])},
    )
    assert first.status_code == 202
    assert second.json()["workflow_id"] == first.json()["workflow_id"]


# ==========================================================================
# Response hygiene (invariant 19)
# ==========================================================================
@pytest.mark.asyncio
async def test_no_response_leaks_a_credential_field(
    client, db_session, workspace
) -> None:
    ids = await seed_campaign_and_draft(workspace, tag="leak")
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="leak")
    token = await token_for(client, email, await slug_of(workspace))

    banned = ("encrypted_credential", "api_key", "secret", "password_hash")
    for path in (
        "/api/v1/workspace",
        "/api/v1/campaigns",
        f"/api/v1/campaigns/{ids['campaign_id']}/policy",
        "/api/v1/leads",
        "/api/v1/drafts",
        "/api/v1/suppressions",
        "/api/v1/usage",
    ):
        body = (await client.get(path, headers=auth(token))).text.lower()
        for field in banned:
            assert field not in body, f"{path} leaked {field}"


# ==========================================================================
# Passwordless local login must not survive into a deployed environment
# ==========================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["staging", "production"])
async def test_local_token_issuance_is_refused_wherever_deployed(
    client, monkeypatch, environment: str
) -> None:
    """/auth/token takes an email and a workspace slug and no password.

    Gating it on production alone left staging -- same data, same reachability
    -- issuing a token carrying that member's role to anyone who could guess an
    address.
    """
    from titan.api import routes
    from titan.config import Settings

    deployed = Settings(
        environment=environment,
        auth_mode="local",
        local_jwt_secret="deployed-secret-not-for-production",
    )
    monkeypatch.setattr(routes, "get_settings", lambda: deployed)

    response = await client.post(
        "/api/v1/auth/token",
        json={"email": "anyone@titan.test", "workspace_slug": "any-workspace"},
    )

    assert response.status_code == 501
    assert "access_token" not in response.text
