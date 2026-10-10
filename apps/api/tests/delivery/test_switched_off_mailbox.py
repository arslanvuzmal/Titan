"""Mail queued behind a switched-off mailbox moves, or waits; it is not lost.

Found 10 Oct 2026 retiring arslan@: all ten queued emails were pinned to it,
and the send gate refused a switched-off mailbox outright -- a block, final.
"""

from __future__ import annotations

import datetime as dt

import pytest
from coldops.db.models import (
    CampaignSender,
    OutboxMessage,
    SenderIdentity,
)
from coldops.db.session import get_sessionmaker
from coldops.delivery.providers.mock import MockEmailProvider
from sqlalchemy import update

from .conftest import NOW
from .test_outbox_delivery import run_worker

pytestmark = pytest.mark.integration


async def _switch_off(sendable) -> None:
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(SenderIdentity)
            .where(SenderIdentity.id == sendable.sender_id)
            .values(is_active=False)
        )


@pytest.mark.asyncio
async def test_with_no_mailbox_on_it_waits(db_session, sendable) -> None:
    await _switch_off(sendable)
    provider = MockEmailProvider()
    results = await run_worker(provider)
    assert [r.outcome for r in results] == ["deferred"]
    assert provider.delivered_count == 0
    async with get_sessionmaker()() as s:
        row = await s.get(OutboxMessage, sendable.outbox_id)
    assert row.status.value == "deferred"


@pytest.mark.asyncio
async def test_it_moves_to_a_mailbox_that_is_on(db_session, sendable) -> None:
    async with get_sessionmaker()() as s, s.begin():
        original = await s.get(SenderIdentity, sendable.sender_id)
        other = SenderIdentity(
            workspace_id=sendable.workspace_id,
            label="second",
            from_email="second@mail.arslanvuzmallone.dev",
            from_name=original.from_name,
            reply_to_email="second@mail.arslanvuzmallone.dev",
            sending_domain=original.sending_domain,
            domain_verified=True,
            spf_ok=True,
            dkim_ok=True,
            dmarc_ok=True,
            last_verified_at=dt.datetime.now(dt.UTC),
            mailing_address=original.mailing_address,
            unsubscribe_mailto="mailto:unsub@mail.arslanvuzmallone.dev",
            # The moved message takes this mailbox's unsubscribe headers, and
            # the gate wants one-click: an https target as well as the mailto.
            unsubscribe_url_template="https://arslanvuzmallone.com/unsubscribe",
            supports_one_click_unsubscribe=True,
            daily_send_limit=100,
        )
        s.add(other)
        await s.flush()
        for sid in (original.id, other.id):
            s.add(
                CampaignSender(
                    workspace_id=sendable.workspace_id,
                    campaign_id=sendable.campaign_id,
                    sender_identity_id=sid,
                )
            )
        other_id = other.id
    await _switch_off(sendable)

    provider = MockEmailProvider()
    secret = {"unsubscribe_secret": "test-unsubscribe-secret-0123456789abcdef"}
    first = await run_worker(provider, **secret)
    assert [r.outcome for r in first] == ["deferred"]
    assert "moved to second@" in (first[0].detail or "")
    async with get_sessionmaker()() as s:
        row = await s.get(OutboxMessage, sendable.outbox_id)
    assert row.sender_identity_id == other_id

    later = await run_worker(
        provider, now_fn=lambda: NOW + dt.timedelta(minutes=2), **secret
    )
    assert [r.outcome for r in later] == ["sent"], later
    assert provider.delivered_count == 1
