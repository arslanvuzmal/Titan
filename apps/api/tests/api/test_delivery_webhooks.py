"""The delivery-event webhook route.

This endpoint is reachable by anyone on the internet and, on the other side of
it, suppresses email addresses and marks messages permanently bounced. The
signature is the only thing standing between those two facts, so most of what
is checked here is the refusal path.

Requests go through the real ASGI app.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from titan.db.enums import MessageState
from titan.db.models import Message, ProviderEvent
from titan.db.session import get_sessionmaker

pytestmark = pytest.mark.asyncio

SECRET_BYTES = b"titan-webhook-test-secret-000000"
WEBHOOK_SECRET = "whsec_" + base64.b64encode(SECRET_BYTES).decode()

URL = "/api/v1/delivery/webhooks/resend"


@pytest_asyncio.fixture
async def client(monkeypatch):
    import os

    os.environ.setdefault("TITAN_LOCAL_JWT_SECRET", "test-secret-not-for-production")
    monkeypatch.setenv("TITAN_RESEND_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from titan.config import get_settings

    get_settings.cache_clear()
    from titan.api.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as http_client:
        yield http_client
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def unconfigured_client(monkeypatch):
    """The same app with no webhook secret. Must fail closed."""
    import os

    os.environ.setdefault("TITAN_LOCAL_JWT_SECRET", "test-secret-not-for-production")
    monkeypatch.delenv("TITAN_RESEND_WEBHOOK_SECRET", raising=False)
    from titan.config import get_settings

    get_settings.cache_clear()
    from titan.api.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as http_client:
        yield http_client
    get_settings.cache_clear()


def sign(body: bytes, *, msg_id: str = "msg_1", timestamp: int | None = None) -> dict:
    """Svix-style headers for a body, as Resend would send them."""
    ts = str(
        timestamp if timestamp is not None else int(dt.datetime.now(dt.UTC).timestamp())
    )
    signed = b".".join([msg_id.encode(), ts.encode(), body])
    digest = base64.b64encode(hmac.new(SECRET_BYTES, signed, hashlib.sha256).digest())
    return {
        "svix-id": msg_id,
        "svix-timestamp": ts,
        "svix-signature": f"v1,{digest.decode()}",
        "content-type": "application/json",
    }


def event(
    *,
    kind: str = "email.bounced",
    provider_message_id: str = "resend-msg-1",
    recipient: str = "someone@recipient-fixture.test",
    event_id: str = "evt-1",
) -> bytes:
    """A Resend webhook body. Serialised once so the bytes signed are the bytes sent."""
    return json.dumps(
        {
            "type": kind,
            "created_at": "2026-08-11T09:00:00.000Z",
            "data": {
                "email_id": provider_message_id,
                "to": [recipient],
                "subject": "A broken button on your booking page",
                "headers": [{"name": "X-Entity-Ref-ID", "value": event_id}],
            },
        }
    ).encode()


# ==========================================================================
# Refusals -- the whole point of the endpoint
# ==========================================================================
async def test_an_unsigned_request_is_rejected(client):
    """Without this, anyone could suppress any address by POSTing JSON."""
    response = await client.post(URL, content=event())

    assert response.status_code == 401


async def test_a_forged_signature_is_rejected(client):
    body = event()
    headers = sign(body)
    headers["svix-signature"] = "v1," + base64.b64encode(b"not-the-right-digest").decode()

    response = await client.post(URL, content=body, headers=headers)

    assert response.status_code == 401


async def test_a_signature_for_different_content_is_rejected(client):
    """The signature covers the body; swapping the body must invalidate it."""
    headers = sign(event(kind="email.delivered"))
    tampered = event(kind="email.complained")

    response = await client.post(URL, content=tampered, headers=headers)

    assert response.status_code == 401


async def test_a_replayed_old_signature_is_rejected(client):
    """A captured request must not stay valid indefinitely."""
    body = event()
    stale = int(dt.datetime.now(dt.UTC).timestamp()) - 3600

    response = await client.post(URL, content=body, headers=sign(body, timestamp=stale))

    assert response.status_code == 401


async def test_an_unconfigured_deployment_fails_closed(unconfigured_client):
    """No secret means nothing can be verified, so nothing may be trusted.

    Accepting here would let a forged 'delivered' mask a real bounce.
    """
    body = event()

    response = await unconfigured_client.post(URL, content=body, headers=sign(body))

    assert response.status_code == 503


async def test_an_unknown_provider_is_a_404(client):
    body = event()

    response = await client.post(
        "/api/v1/delivery/webhooks/sendgrid", content=body, headers=sign(body)
    )

    assert response.status_code == 404


async def test_a_signed_but_unparseable_body_is_a_400(client):
    """Signed, so it really came from the provider: a real integration fault."""
    body = b"{not json"

    response = await client.post(URL, content=body, headers=sign(body))

    assert response.status_code == 400


async def test_a_signed_non_object_body_is_a_400(client):
    body = b"[1, 2, 3]"

    response = await client.post(URL, content=body, headers=sign(body))

    assert response.status_code == 400


async def test_nothing_is_stored_for_a_rejected_request(db_session, client):
    """An unverified payload is attacker-controlled input, not a diagnostic."""
    before = len((await db_session.execute(select(ProviderEvent))).scalars().all())

    await client.post(URL, content=event())

    async with get_sessionmaker()() as s:
        after = len((await s.execute(select(ProviderEvent))).scalars().all())
    assert after == before


# ==========================================================================
# Acceptance
# ==========================================================================
async def test_an_event_for_an_unknown_message_is_accepted_not_retried(client):
    """A non-2xx makes the provider retry, and retrying will not help.

    Providers disable endpoints that keep failing, which would cost every
    later event too.
    """
    body = event(provider_message_id="resend-never-seen")

    response = await client.post(URL, content=body, headers=sign(body))

    assert response.status_code == 200
    assert response.json()["ignored_reason"] == "no_matching_message"


async def test_an_unrecognised_event_type_is_accepted(client):
    body = event(kind="email.opened.something.new")

    response = await client.post(URL, content=body, headers=sign(body))

    assert response.status_code == 200
    assert response.json()["ignored_reason"] == "unrecognised_event_type"


# ==========================================================================
# The loop closing -- what this endpoint exists for
# ==========================================================================
async def seed_sent_message(workspace_id: uuid.UUID, *, provider_message_id: str) -> str:
    """A message that has gone out, as the outbox worker would leave it."""
    from titan.db.enums import CampaignStatus, Industry, LeadStatus
    from titan.db.models import Campaign, Lead, Organization, SenderIdentity

    recipient = f"someone-{uuid.uuid4().hex[:6]}@recipient-fixture.test"
    async with get_sessionmaker()() as session, session.begin():
        sender = SenderIdentity(
            workspace_id=workspace_id,
            label="primary",
            from_email="sender@titan-fixture.test",
            from_name="Titan",
            reply_to_email="sender@titan-fixture.test",
            sending_domain="titan-fixture.test",
            mailing_address="12 Fictional Row",
            unsubscribe_mailto="mailto:unsub@titan-fixture.test",
        )
        campaign = Campaign(
            workspace_id=workspace_id,
            name="Webhook fixture",
            slug=f"wh-{uuid.uuid4().hex[:8]}",
            status=CampaignStatus.ACTIVE,
            industry=Industry.GENERAL,
        )
        org = Organization(
            workspace_id=workspace_id,
            display_name="Recipient Fixture Ltd",
            normalized_name="recipient fixture ltd",
            canonical_domain="recipient-fixture.test",
        )
        session.add_all([sender, campaign, org])
        await session.flush()

        lead = Lead(
            workspace_id=workspace_id,
            campaign_id=campaign.id,
            organization_id=org.id,
            status=LeadStatus.CONTACTED,
        )
        session.add(lead)
        await session.flush()

        session.add(
            Message(
                workspace_id=workspace_id,
                lead_id=lead.id,
                campaign_id=campaign.id,
                sender_identity_id=sender.id,
                dedupe_key=f"m-{uuid.uuid4().hex[:8]}",
                to_email=recipient,
                to_email_normalized=recipient,
                to_domain="recipient-fixture.test",
                from_email="sender@titan-fixture.test",
                subject="A broken button on your booking page",
                state=MessageState.SENT,
                state_rank=30,
                provider="resend",
                provider_message_id=provider_message_id,
                sent_at=dt.datetime.now(dt.UTC),
            )
        )
    return recipient


async def test_a_complaint_marks_the_message_and_suppresses_the_address(
    db_session, workspace, client
):
    """The whole reason this route exists.

    Until it did, MessageState.COMPLAINED was written by nothing in production,
    so the weekly report's complaint rate was 0.00% by construction -- against
    a Gmail ceiling of 0.30%.
    """
    from titan.db.models.compliance import SuppressionEntry

    recipient = await seed_sent_message(workspace, provider_message_id="resend-cx-1")
    body = event(
        kind="email.complained",
        provider_message_id="resend-cx-1",
        recipient=recipient,
        event_id="evt-complaint-1",
    )

    response = await client.post(URL, content=body, headers=sign(body))

    assert response.status_code == 200
    assert response.json()["state_changed"] is True

    async with get_sessionmaker()() as s:
        message = (
            (
                await s.execute(
                    select(Message).where(Message.provider_message_id == "resend-cx-1")
                )
            )
            .scalars()
            .one()
        )
        assert message.state is MessageState.COMPLAINED

        entry = (
            (
                await s.execute(
                    select(SuppressionEntry).where(
                        SuppressionEntry.normalized_value == recipient
                    )
                )
            )
            .scalars()
            .one()
        )
        assert entry.reason.value == "complaint"


async def test_a_retried_delivery_is_not_applied_twice(db_session, workspace, client):
    """Invariant 12. Providers retry; the address must not be suppressed twice."""
    await seed_sent_message(workspace, provider_message_id="resend-dupe-1")
    body = event(
        kind="email.bounced",
        provider_message_id="resend-dupe-1",
        event_id="evt-dupe-1",
    )
    headers = sign(body, msg_id="msg-dupe")

    first = await client.post(URL, content=body, headers=headers)
    second = await client.post(URL, content=body, headers=sign(body, msg_id="msg-dupe-2"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True

    async with get_sessionmaker()() as s:
        events = (
            (
                await s.execute(
                    select(ProviderEvent).where(
                        ProviderEvent.provider_message_id == "resend-dupe-1"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1


async def test_a_late_delivered_event_does_not_undo_a_bounce(
    db_session, workspace, client
):
    """Invariant 13. Providers deliver out of order; final state must hold."""
    await seed_sent_message(workspace, provider_message_id="resend-order-1")

    bounced = event(
        kind="email.bounced", provider_message_id="resend-order-1", event_id="evt-b"
    )
    await client.post(URL, content=bounced, headers=sign(bounced, msg_id="m-b"))

    delivered = event(
        kind="email.delivered", provider_message_id="resend-order-1", event_id="evt-d"
    )
    late = await client.post(
        URL, content=delivered, headers=sign(delivered, msg_id="m-d")
    )

    assert late.status_code == 200

    async with get_sessionmaker()() as s:
        message = (
            (
                await s.execute(
                    select(Message).where(Message.provider_message_id == "resend-order-1")
                )
            )
            .scalars()
            .one()
        )
    assert message.state is MessageState.BOUNCED
