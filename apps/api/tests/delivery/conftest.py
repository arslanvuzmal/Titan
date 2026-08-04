"""Fixtures that build a complete, sendable lead in the database.

Every delivery test starts from a state where a message *would* legitimately be
sent, then breaks exactly one thing. That way a passing test proves the specific
guard fired, not that some unrelated precondition happened to be missing.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from titan.config import OperatingMode, Settings
from titan.db.enums import (
    CampaignStatus,
    ContactSource,
    DraftStatus,
    LeadStatus,
    MessageState,
    OutboxStatus,
    VerificationStatus,
    WorkspaceRole,
)
from titan.db.models import (
    Campaign,
    CampaignPolicy,
    Contact,
    ContactChannel,
    Lead,
    Message,
    MessageApproval,
    MessageDraft,
    Organization,
    OrganizationLocation,
    OutboxMessage,
    SenderIdentity,
    User,
    Workspace,
    WorkspaceMember,
)

NOW = dt.datetime(2026, 8, 3, 12, 0, tzinfo=dt.UTC)


def sending_settings(**overrides) -> Settings:
    base = {
        "environment": "test",
        "production_sending_enabled": True,
        "email_provider": "resend",
        "resend_api_key": "re_test_not_a_real_key_000",
        "email_auth_preflight_acknowledged": True,
        "sender_mailing_address": "12 Fictional Row, Testville, TE1 1ST",
        "quiet_hours_enabled": False,
        "quota_min_spacing_seconds": 0,
        "outbox_batch_size": 25,
    }
    base.update(overrides)
    return Settings(**base)


@dataclass
class SendableFixture:
    workspace_id: uuid.UUID
    campaign_id: uuid.UUID
    lead_id: uuid.UUID
    draft_id: uuid.UUID
    message_id: uuid.UUID
    outbox_id: uuid.UUID
    sender_id: uuid.UUID
    channel_id: uuid.UUID
    to_email: str
    dedupe_key: str


@pytest_asyncio.fixture
async def sendable(db_session, workspace) -> SendableFixture:
    """A fully authorized, ready-to-send outbox row."""
    return await build_sendable(db_session, workspace)


async def build_sendable(
    session,
    workspace_id: uuid.UUID,
    *,
    to_email: str | None = None,
    suffix: str | None = None,
    daily_send_limit: int = 100,
    recipient_domain_daily_limit: int = 50,
) -> SendableFixture:
    tag = suffix or uuid.uuid4().hex[:8]
    address = to_email or f"hello-{tag}@fixture-business.test"

    # users is not workspace-scoped, so it does not cascade away with the
    # workspace. Always unique, or a rerun with a fixed suffix collides.
    user = User(
        email=f"operator-{tag}-{uuid.uuid4().hex[:8]}@titan.test", display_name="Operator"
    )
    session.add(user)
    await session.flush()
    session.add(
        WorkspaceMember(
            workspace_id=workspace_id, user_id=user.id, role=WorkspaceRole.OWNER
        )
    )

    ws = await session.get(Workspace, workspace_id)
    ws.operating_mode = OperatingMode.CONTROLLED_AUTOPILOT
    ws.sending_authorized = True
    ws.daily_send_limit = daily_send_limit

    sender = SenderIdentity(
        workspace_id=workspace_id,
        label="primary",
        from_email=f"arslan-{tag}@mail.arslanvuzmallone.dev",
        from_name="Arslan Vuzmal Lone",
        reply_to_email=f"arslan-{tag}@mail.arslanvuzmallone.dev",
        sending_domain="mail.arslanvuzmallone.dev",
        domain_verified=True,
        spf_ok=True,
        dkim_ok=True,
        dmarc_ok=True,
        mailing_address="12 Fictional Row, Testville, TE1 1ST",
        unsubscribe_mailto="mailto:unsub@mail.arslanvuzmallone.dev",
        supports_one_click_unsubscribe=True,
        daily_send_limit=daily_send_limit,
    )
    campaign = Campaign(
        workspace_id=workspace_id,
        name=f"Campaign {tag}",
        slug=f"campaign-{tag}",
        status=CampaignStatus.ACTIVE,
    )
    org = Organization(
        workspace_id=workspace_id,
        display_name=f"Fixture Business {tag}",
        normalized_name=f"fixture business {tag}",
        canonical_domain=f"fixture-{tag}.test",
    )
    session.add_all([sender, campaign, org])
    await session.flush()

    session.add(
        OrganizationLocation(
            workspace_id=workspace_id,
            organization_id=org.id,
            country_code="GB",
            timezone="Europe/London",
            is_primary=True,
        )
    )
    session.add(
        CampaignPolicy(
            workspace_id=workspace_id,
            campaign_id=campaign.id,
            operating_mode=OperatingMode.CONTROLLED_AUTOPILOT,
            sending_authorized=True,
            min_lead_score=70,
            daily_send_limit=daily_send_limit,
            recipient_domain_daily_limit=recipient_domain_daily_limit,
            min_spacing_seconds=0,
            respect_quiet_hours=False,
        )
    )
    campaign.sender_identity_id = sender.id

    contact = Contact(
        workspace_id=workspace_id, organization_id=org.id, full_name="Sam Fixture"
    )
    session.add(contact)
    await session.flush()

    channel = ContactChannel(
        workspace_id=workspace_id,
        contact_id=contact.id,
        channel_type="email",
        value=address,
        normalized_value=address,
        value_domain=address.split("@", 1)[1],
        source=ContactSource.FIRST_PARTY_WEBSITE,
        source_url=f"https://fixture-{tag}.test/contact",
        discovered_at=NOW,
        verification_status=VerificationStatus.PUBLISHED_FIRST_PARTY,
        confidence=0.9,
    )
    lead = Lead(
        workspace_id=workspace_id,
        campaign_id=campaign.id,
        organization_id=org.id,
        status=LeadStatus.QUALIFIED,
        latest_score=88,
    )
    session.add_all([channel, lead])
    await session.flush()
    lead.primary_contact_channel_id = channel.id

    draft = MessageDraft(
        workspace_id=workspace_id,
        lead_id=lead.id,
        campaign_id=campaign.id,
        contact_channel_id=channel.id,
        idempotency_key=f"draft-{tag}",
        status=DraftStatus.APPROVED,
        subject="A broken button on your booking page",
        # A realistic, deliverability-compliant body. The stub this replaced was
        # correctly refused by the deliverability gate: too short, no postal
        # address in the text. A fixture that could not legitimately be sent
        # would make every delivery test vacuous.
        body_text=(
            "Hi there,\n\n"
            "On fixture-business.test the booking button returns a 404, so anyone "
            "who clicks it cannot reach your booking form. That is the step most "
            "likely to be used by someone ready to act. I build booking and "
            "follow-up fixes for businesses of this size, and could outline what "
            "it would take in about ten minutes.\n\n"
            "Would a short call next week be useful?\n\n"
            "Arslan Vuzmal Lone\n"
            "https://arslanvuzmallone.dev\n"
            "12 Fictional Row, Testville, TE1 1ST\n"
            "Unsubscribe: https://arslanvuzmallone.dev/unsubscribe\n"
        ),
        claim_map=[
            {
                "sentence": "Your booking button returns a 404.",
                "claim": "broken CTA",
                "finding_id": f"finding-{tag}",
                "evidence_ids": [f"ev-{tag}"],
            }
        ],
        validation_passed=True,
        template_key="first_observation",
    )
    session.add(draft)
    await session.flush()

    session.add(
        MessageApproval(
            workspace_id=workspace_id,
            draft_id=draft.id,
            draft_version=draft.version,
            decision_seq=1,
            decision="approved",
            decided_by=user.id,
            decided_at=NOW,
            expires_at=NOW + dt.timedelta(days=7),
        )
    )

    dedupe_key = f"lead-{lead.id}-step-0"
    message = Message(
        workspace_id=workspace_id,
        draft_id=draft.id,
        lead_id=lead.id,
        campaign_id=campaign.id,
        sender_identity_id=sender.id,
        dedupe_key=dedupe_key,
        to_email=address,
        to_email_normalized=address,
        to_domain=address.split("@", 1)[1],
        from_email=sender.from_email,
        subject=draft.subject,
        state=MessageState.QUEUED,
        state_rank=0,
        provider="mock",
    )
    session.add(message)
    await session.flush()

    outbox = OutboxMessage(
        workspace_id=workspace_id,
        message_id=message.id,
        draft_id=draft.id,
        campaign_id=campaign.id,
        lead_id=lead.id,
        sender_identity_id=sender.id,
        dedupe_key=dedupe_key,
        provider_idempotency_key=f"idem-{dedupe_key}",
        status=OutboxStatus.PENDING,
        to_email_normalized=address,
        to_domain=address.split("@", 1)[1],
        next_attempt_at=NOW - dt.timedelta(minutes=1),
        payload={
            "to_email": address,
            "from_email": sender.from_email,
            "from_name": sender.from_name,
            "reply_to": sender.reply_to_email,
            "subject": draft.subject,
            "text_body": draft.body_text,
            # RFC 8058: an https target plus the -Post header is what renders
            # Gmail's one-click unsubscribe button.
            "list_unsubscribe": (
                "<https://arslanvuzmallone.dev/unsubscribe>, "
                f"<{sender.unsubscribe_mailto}>"
            ),
            "list_unsubscribe_post": "List-Unsubscribe=One-Click",
        },
    )
    session.add(outbox)
    await session.commit()

    return SendableFixture(
        workspace_id=workspace_id,
        campaign_id=campaign.id,
        lead_id=lead.id,
        draft_id=draft.id,
        message_id=message.id,
        outbox_id=outbox.id,
        sender_id=sender.id,
        channel_id=channel.id,
        to_email=address,
        dedupe_key=dedupe_key,
    )


@pytest.fixture
def frozen_now():
    return lambda: NOW
