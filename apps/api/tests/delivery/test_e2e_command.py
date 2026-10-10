"""``coldops e2e start --apply`` builds a lead the pipeline will write to the operator.

Temporal is replaced by a recorder: what is under test is the rows the command
writes, that the research it would start is for the right lead, and that contact
resolution then picks the operator's inbox rather than anything the site lists.
"""

from __future__ import annotations

import argparse
import uuid

import pytest
from coldops import cli_e2e
from coldops.activities import pipeline
from coldops.db.enums import CampaignStatus, ContactSource
from coldops.db.models import Campaign, ContactChannel, Lead, Workspace
from coldops.db.session import get_sessionmaker
from coldops.workers import temporal_worker
from coldops.workflows.types import ContactActivityInput
from sqlalchemy import select, update

from .conftest import sending_settings

pytestmark = pytest.mark.integration

INBOX = "operator-own-inbox@example.com"


class _Recorder:
    def __init__(self) -> None:
        self.started: list[tuple[str, object, str]] = []

    async def start_workflow(self, name, arg, *, id, task_queue):
        self.started.append((name, arg, id))


def _args(slug: str, **extra) -> argparse.Namespace:
    base = {
        "workspace": slug,
        "site": "https://www.example-own-site.test/",
        "to": INBOX,
        "apply": True,
        "again": False,
    }
    base.update(extra)
    return argparse.Namespace(**base)


@pytest.fixture
def listed(monkeypatch):
    settings = sending_settings(test_recipients=INBOX)
    monkeypatch.setattr(cli_e2e, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    recorder = _Recorder()

    async def connect():
        return recorder

    monkeypatch.setattr(temporal_worker, "connect", connect)
    return recorder


async def _slug(workspace_id: uuid.UUID) -> str:
    async with get_sessionmaker()() as s:
        return (
            await s.execute(select(Workspace.slug).where(Workspace.id == workspace_id))
        ).scalar_one()


@pytest.mark.asyncio
async def test_start_builds_the_lead_and_starts_its_research(
    db_session, sendable, listed
) -> None:
    slug = await _slug(sendable.workspace_id)
    assert await cli_e2e._start(_args(slug)) == 0

    async with get_sessionmaker()() as s:
        campaign = (
            await s.execute(
                select(Campaign).where(
                    Campaign.workspace_id == sendable.workspace_id,
                    Campaign.slug == cli_e2e.CAMPAIGN_SLUG,
                )
            )
        ).scalar_one()
        lead = (
            await s.execute(select(Lead).where(Lead.campaign_id == campaign.id))
        ).scalar_one()
        channel = await s.get(ContactChannel, lead.primary_contact_channel_id)

    assert campaign.status is CampaignStatus.PAUSED
    assert channel is not None
    assert channel.normalized_value == INBOX
    assert channel.source is ContactSource.MANUAL_ENTRY

    [(name, arg, _id)] = listed.started
    assert name == "LeadResearchWorkflow"
    assert arg.lead_id == str(lead.id)
    assert arg.seed_url == "https://www.example-own-site.test/"

    # The step that would otherwise go looking on the site for an address.
    result = await pipeline.resolve_contact(
        ContactActivityInput(
            workspace_id=str(sendable.workspace_id),
            lead_id=str(lead.id),
            campaign_id=str(campaign.id),
            research_run_id=str(uuid.uuid4()),
            idempotency_key="e2e-test",
        )
    )
    assert result.eligible_channel_id == str(channel.id)

    # Running it twice reuses the same campaign and lead.
    assert await cli_e2e._start(_args(slug)) == 0
    async with get_sessionmaker()() as s:
        leads = (
            (await s.execute(select(Lead).where(Lead.campaign_id == campaign.id)))
            .scalars()
            .all()
        )
    assert len(leads) == 1

    # And status reads all of it without failing.
    assert await cli_e2e._status(argparse.Namespace(workspace=slug)) == 0


@pytest.mark.asyncio
async def test_an_unlisted_inbox_is_refused_before_anything_is_written(
    db_session, sendable, monkeypatch
) -> None:
    monkeypatch.setattr(cli_e2e, "get_settings", lambda: sending_settings())
    slug = await _slug(sendable.workspace_id)
    assert await cli_e2e._start(_args(slug)) == 1
    async with get_sessionmaker()() as s:
        found = (
            await s.execute(
                select(Campaign).where(Campaign.slug == cli_e2e.CAMPAIGN_SLUG)
            )
        ).first()
    assert found is None


@pytest.mark.asyncio
async def test_one_of_our_own_mailboxes_is_refused(
    db_session, sendable, listed, monkeypatch
) -> None:
    from coldops.db.models import SenderIdentity

    async with get_sessionmaker()() as s:
        own = (
            await s.execute(
                select(SenderIdentity.from_email).where(
                    SenderIdentity.id == sendable.sender_id
                )
            )
        ).scalar_one()
    settings = sending_settings(test_recipients=own)
    monkeypatch.setattr(cli_e2e, "get_settings", lambda: settings)
    slug = await _slug(sendable.workspace_id)
    assert await cli_e2e._start(_args(slug, to=own)) == 1


@pytest.mark.asyncio
async def test_a_resting_domain_still_gives_the_test_a_mailbox(
    db_session, sendable, monkeypatch
) -> None:
    """Queue time: a paused mailbox behind the gate still takes the test, and only it."""
    from coldops.db.models import Message, OutboxMessage, SenderIdentity
    from coldops.workflows.types import QueueActivityInput
    from sqlalchemy import delete, update

    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            delete(OutboxMessage).where(OutboxMessage.id == sendable.outbox_id)
        )
        await s.execute(delete(Message).where(Message.id == sendable.message_id))
        await s.execute(
            update(ContactChannel)
            .where(ContactChannel.id == sendable.channel_id)
            .values(source=ContactSource.MANUAL_ENTRY)
        )
        await s.execute(
            update(SenderIdentity)
            .where(SenderIdentity.id == sendable.sender_id)
            .values(is_active=False)
        )

    def queue() -> object:
        return pipeline.queue_message(
            QueueActivityInput(
                workspace_id=str(sendable.workspace_id),
                draft_id=str(sendable.draft_id),
                approval_id=None,
                idempotency_key="e2e-queue",
            )
        )

    # Gate on, no readings, inbox not listed: no mailbox may take it.
    gated = sending_settings(placement_gate_enabled=True)
    monkeypatch.setattr(pipeline, "get_settings", lambda: gated)
    import coldops.delivery.sender_pool as pool

    monkeypatch.setattr(pool, "get_settings", lambda: gated)
    refused = await queue()
    assert not refused.queued

    listed_settings = sending_settings(
        placement_gate_enabled=True, test_recipients=sendable.to_email
    )
    monkeypatch.setattr(pipeline, "get_settings", lambda: listed_settings)
    monkeypatch.setattr(pool, "get_settings", lambda: listed_settings)
    accepted = await queue()
    assert accepted.queued, accepted.refused_reasons


@pytest.mark.asyncio
async def test_again_after_a_send_is_a_first_email_to_a_new_lead(
    db_session, sendable, listed
) -> None:
    """The personal PDF only rides a first email, so a re-run needs a new lead."""
    import datetime as dt

    from coldops.db.models import Message

    slug = await _slug(sendable.workspace_id)
    assert await cli_e2e._start(_args(slug)) == 0
    first_campaign = await cli_e2e._test_campaign_id(sendable.workspace_id)

    async with get_sessionmaker()() as s, s.begin():
        first_lead = (
            await s.execute(select(Lead).where(Lead.campaign_id == first_campaign))
        ).scalar_one()
        # Stand in for the test email having gone out.
        await s.execute(
            update(Message)
            .where(Message.id == sendable.message_id)
            .values(lead_id=first_lead.id, sent_at=dt.datetime.now(dt.UTC))
        )

    # Without --again it refuses rather than writing to the same lead twice.
    assert await cli_e2e._start(_args(slug)) == 1

    assert await cli_e2e._start(_args(slug, again=True)) == 0
    second_campaign = await cli_e2e._test_campaign_id(sendable.workspace_id)
    assert second_campaign != first_campaign
    async with get_sessionmaker()() as s:
        campaign = await s.get(Campaign, second_campaign)
        second_lead = (
            await s.execute(select(Lead).where(Lead.campaign_id == second_campaign))
        ).scalar_one()
    assert campaign.slug == f"{cli_e2e.CAMPAIGN_SLUG}-2"
    assert campaign.status is CampaignStatus.PAUSED
    assert second_lead.id != first_lead.id
    # And the research it started is the new lead's.
    assert listed.started[-1][1].lead_id == str(second_lead.id)
