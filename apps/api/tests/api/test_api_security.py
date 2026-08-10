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
from titan.api.passwords import hash_passcode
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


#: The passcode every test account shares, hashed once. argon2 is deliberately
#: expensive, so hashing per user would add real seconds to the suite for no
#: coverage -- the hashing itself is tested in tests/api/test_passwords.py.
TEST_PASSCODE = "correct-horse-battery"
TEST_PASSCODE_HASH = hash_passcode(TEST_PASSCODE)


async def make_member(
    workspace_id: uuid.UUID, role: WorkspaceRole, *, tag: str
) -> tuple[uuid.UUID, str]:
    """Create a user with a membership; return (user_id, email)."""
    email = f"{tag}-{uuid.uuid4().hex[:8]}@titan.test"
    async with get_sessionmaker()() as session, session.begin():
        user = User(
            email=email,
            display_name=tag,
            username=email.split("@")[0],
            password_hash=TEST_PASSCODE_HASH,
        )
        session.add(user)
        await session.flush()
        session.add(
            WorkspaceMember(workspace_id=workspace_id, user_id=user.id, role=role)
        )
        return user.id, email


async def token_for(client: httpx.AsyncClient, email: str, slug: str) -> str:
    """Sign in as the account `make_member` created for this email.

    Kept on (email, slug) so the ~40 call sites did not have to change when
    login moved to username and passcode. `make_member` derives the username
    from the address, and the slug is passed through for the accounts that
    belong to more than one workspace.
    """
    response = await client.post(
        "/api/v1/auth/token",
        json={
            "username": email.split("@")[0],
            "passcode": TEST_PASSCODE,
            "workspace": slug,
        },
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
async def test_login_does_not_reveal_whether_an_account_exists(client, workspace) -> None:
    """An unknown username and a wrong passcode are the same answer.

    A different status or a different message turns the login form into an
    account enumerator: an attacker learns which handles are real before
    spending a single guess on a passcode.
    """
    _user_id, email = await make_member(workspace, WorkspaceRole.OWNER, tag="enum")

    unknown = await client.post(
        "/api/v1/auth/token",
        json={"username": "no-such-operator", "passcode": "whatever-this-is"},
    )
    wrong = await client.post(
        "/api/v1/auth/token",
        json={"username": email.split("@")[0], "passcode": "not-the-passcode"},
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"] == "invalid credentials"


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
# Local login: a passcode, not an environment check
# ==========================================================================
@pytest.mark.asyncio
async def test_selecting_clerk_disables_local_token_issuance(client, monkeypatch) -> None:
    """Configuring Clerk closes this route, everywhere.

    The gate used to be the environment, because the route had no secret to
    check and could not be allowed to face the internet. Now that it verifies a
    passcode, the question it answers is "is this the configured identity
    provider" -- and a deployment that authenticates through Clerk must not
    also accept a second, locally-minted session.
    """
    from titan.api import routes
    from titan.config import Settings

    clerk = Settings(
        environment="production",
        auth_mode="clerk",
        clerk_issuer_url="https://example.clerk.accounts.dev",
    )
    monkeypatch.setattr(routes, "get_settings", lambda: clerk)

    response = await client.post(
        "/api/v1/auth/token",
        json={"username": "anyone", "passcode": "anything-at-all"},
    )

    assert response.status_code == 501
    assert "access_token" not in response.text


@pytest.mark.asyncio
async def test_an_account_with_no_passcode_cannot_sign_in(client, workspace) -> None:
    """Null password_hash means "cannot sign in", not "no passcode needed".

    Every user row created before passcodes existed has NULL here. If the
    verifier treated that as a pass, enabling local login would have handed a
    session to anyone who could guess a username.
    """
    email = f"nopass-{uuid.uuid4().hex[:8]}@titan.test"
    username = email.split("@")[0]
    async with get_sessionmaker()() as session, session.begin():
        user = User(email=email, display_name="no passcode", username=username)
        session.add(user)
        await session.flush()
        session.add(
            WorkspaceMember(
                workspace_id=workspace, user_id=user.id, role=WorkspaceRole.OWNER
            )
        )

    for attempt in ("", "anything", TEST_PASSCODE):
        response = await client.post(
            "/api/v1/auth/token", json={"username": username, "passcode": attempt}
        )
        assert response.status_code == 401, attempt
        assert "access_token" not in response.text


@pytest.mark.asyncio
async def test_repeated_wrong_passcodes_lock_the_account(
    client, workspace, monkeypatch
) -> None:
    """The lockout is what makes a short passcode survivable.

    Six digits is a million guesses, which is minutes at HTTP speed and
    centuries at three-attempts-then-wait. The counter must therefore survive
    the rollback of the request that increments it -- an earlier draft wrapped
    the handler in `session.begin()`, and the increment was discarded on every
    failed attempt, leaving a lockout that never fired.
    """
    from titan.api import routes
    from titan.config import Settings

    limited = Settings(
        auth_mode="local",
        local_jwt_secret="test-secret-not-for-production",
        login_max_attempts=3,
        login_lockout_seconds=900,
    )
    monkeypatch.setattr(routes, "get_settings", lambda: limited)

    _user_id, email = await make_member(workspace, WorkspaceRole.OWNER, tag="lockout")
    username = email.split("@")[0]

    for _ in range(3):
        response = await client.post(
            "/api/v1/auth/token", json={"username": username, "passcode": "wrong"}
        )
        assert response.status_code == 401

    # The correct passcode is now refused too: the lock is on the account, not
    # on the guess.
    locked = await client.post(
        "/api/v1/auth/token",
        json={"username": username, "passcode": TEST_PASSCODE},
    )
    assert locked.status_code == 429
    assert "access_token" not in locked.text
    assert int(locked.headers["retry-after"]) > 0


@pytest.mark.asyncio
async def test_a_successful_sign_in_clears_the_failure_counter(client, workspace) -> None:
    """Otherwise failures accumulate across days and lock a legitimate user."""
    _user_id, email = await make_member(workspace, WorkspaceRole.OWNER, tag="reset")
    username = email.split("@")[0]

    await client.post(
        "/api/v1/auth/token", json={"username": username, "passcode": "wrong"}
    )
    ok = await client.post(
        "/api/v1/auth/token",
        json={"username": username, "passcode": TEST_PASSCODE},
    )
    assert ok.status_code == 200, ok.text

    async with get_sessionmaker()() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one()
        assert user.failed_login_count == 0
        assert user.locked_until is None
        assert user.last_login_at is not None


@pytest.mark.asyncio
async def test_a_deactivated_account_cannot_sign_in(client, workspace) -> None:
    """is_active is checked at login, not only when a token is presented."""
    user_id, email = await make_member(workspace, WorkspaceRole.OWNER, tag="disabled")
    async with get_sessionmaker()() as session, session.begin():
        user = await session.get(User, user_id)
        assert user is not None
        user.is_active = False

    response = await client.post(
        "/api/v1/auth/token",
        json={"username": email.split("@")[0], "passcode": TEST_PASSCODE},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid credentials"
