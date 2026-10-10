"""The operator's end-to-end test reaches the carrier; nothing else rides on it.

Each test pauses the campaign and switches the placement gate on with no
readings at all -- the state the live estate is in while a domain rests -- and
then varies exactly one of the two facts that make a message the test.
"""

from __future__ import annotations

import pytest
from coldops.db.enums import CampaignStatus, ContactSource
from coldops.db.models import Campaign, ContactChannel
from coldops.db.session import get_sessionmaker
from coldops.delivery.outbox_worker import OutboxWorker
from coldops.delivery.providers.mock import MockEmailProvider
from sqlalchemy import update

from .conftest import NOW, sending_settings

pytestmark = pytest.mark.integration


async def _held_back(sendable, *, source: ContactSource) -> None:
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(Campaign)
            .where(Campaign.id == sendable.campaign_id)
            .values(status=CampaignStatus.PAUSED)
        )
        await s.execute(
            update(ContactChannel)
            .where(ContactChannel.id == sendable.channel_id)
            .values(source=source)
        )


async def _run(provider: MockEmailProvider, recipients: str) -> list:
    worker = OutboxWorker(
        provider,
        sending_settings(placement_gate_enabled=True, test_recipients=recipients),
        now_fn=lambda: NOW,
    )
    return await worker.run_once()


@pytest.mark.asyncio
async def test_the_operators_own_inbox_is_sent_to(db_session, sendable) -> None:
    await _held_back(sendable, source=ContactSource.MANUAL_ENTRY)
    provider = MockEmailProvider()
    results = await _run(provider, sendable.to_email)
    assert [r.outcome for r in results] == ["sent"], results
    assert provider.delivered_count == 1


@pytest.mark.asyncio
async def test_a_published_address_is_cold_mail_even_when_listed(
    db_session, sendable
) -> None:
    await _held_back(sendable, source=ContactSource.FIRST_PARTY_WEBSITE)
    provider = MockEmailProvider()
    results = await _run(provider, sendable.to_email)
    assert provider.delivered_count == 0
    assert [r.outcome for r in results] == ["blocked"]


@pytest.mark.asyncio
async def test_a_hand_entered_address_nobody_listed_is_held(db_session, sendable) -> None:
    await _held_back(sendable, source=ContactSource.MANUAL_ENTRY)
    provider = MockEmailProvider()
    results = await _run(provider, "somebody-else@example.com")
    assert provider.delivered_count == 0
    assert [r.outcome for r in results] == ["blocked"]


@pytest.mark.asyncio
async def test_the_gate_is_really_closed_for_ordinary_mail(db_session, sendable) -> None:
    """The control: same setup, campaign active, an ordinary published address.

    Without this the first test could pass because the gate let everything
    through, rather than because the test was excused from it.
    """
    provider = MockEmailProvider()
    results = await _run(provider, "")
    assert provider.delivered_count == 0
    assert [r.outcome for r in results] == ["deferred"]
