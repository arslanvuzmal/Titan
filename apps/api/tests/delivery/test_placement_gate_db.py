"""The placement gate against a real database: the pool, the send boundary, the
readings query.

Each test starts from a message that would legitimately send and changes only
what its mailbox's probes said.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select, text
from titan.db.enums import OutboxStatus
from titan.db.models import OutboxMessage, SenderIdentity, Workspace
from titan.db.session import get_sessionmaker
from titan.delivery import placement_gate, sender_pool
from titan.delivery.outbox_worker import OutboxWorker
from titan.delivery.providers.mock import MockEmailProvider

from .conftest import NOW, build_sendable, sending_settings

pytestmark = pytest.mark.integration


async def _sender_email(session, sender_id: uuid.UUID) -> str:
    sender = await session.get(SenderIdentity, sender_id)
    assert sender is not None
    return sender.from_email


async def _probe(
    session,
    workspace_id: uuid.UUID,
    from_email: str,
    *,
    folder: str | None,
    hours_ago: float = 3,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO placement_checks
                (workspace_id, probe_token, seed_address, provider, from_email,
                 subject, had_attachment, sent_at, folder, checked_by)
            VALUES (:ws, :token, :seed, 'gmail', :from_email, 'probe', false,
                    :sent_at, :folder, 'imap')
            """
        ),
        {
            "ws": workspace_id,
            "token": f"tp-{uuid.uuid4().hex[:12]}",
            "seed": f"seed-{uuid.uuid4().hex[:6]}@gmail.com",
            "from_email": from_email,
            "sent_at": NOW - dt.timedelta(hours=hours_ago),
            "folder": folder,
        },
    )
    await session.commit()


def _gate_on(monkeypatch) -> None:
    monkeypatch.setattr(
        sender_pool, "get_settings", lambda: sending_settings(placement_gate_enabled=True)
    )


# ------------------------------------------------------------------ readings
@pytest.mark.asyncio
async def test_verdicts_read_only_this_workspaces_probes(db_session, workspace) -> None:
    fixture = await build_sendable(db_session, workspace, suffix="pg1")
    address = await _sender_email(db_session, fixture.sender_id)
    # Another workspace's probe from the same address must not vouch for it.
    other = Workspace(name="other", slug=f"other-{uuid.uuid4().hex[:12]}")
    db_session.add(other)
    await db_session.commit()
    await _probe(db_session, other.id, address, folder="inbox")

    verdicts = await placement_gate.verdicts_for(
        db_session, workspace_id=workspace, mailboxes=[address], now=NOW
    )
    assert verdicts[address.lower()].code == placement_gate.UNMEASURED


@pytest.mark.asyncio
async def test_unread_probes_are_not_readings(db_session, workspace) -> None:
    fixture = await build_sendable(db_session, workspace, suffix="pg2")
    address = await _sender_email(db_session, fixture.sender_id)
    await _probe(db_session, workspace, address, folder=None)

    verdicts = await placement_gate.verdicts_for(
        db_session, workspace_id=workspace, mailboxes=[address], now=NOW
    )
    assert verdicts[address.lower()].code == placement_gate.UNMEASURED


# ------------------------------------------------------------------ the pool
@pytest.mark.asyncio
async def test_the_pool_excludes_an_unmeasured_mailbox(
    db_session, workspace, monkeypatch
) -> None:
    _gate_on(monkeypatch)
    fixture = await build_sendable(db_session, workspace, suffix="pg3")

    slots = await sender_pool.load_slots(
        db_session, workspace, fixture.campaign_id, now=NOW
    )

    assert slots[0].available is False
    assert "no placement reading" in (slots[0].excluded_because or "")


@pytest.mark.asyncio
async def test_the_pool_keeps_a_mailbox_landing_in_the_inbox(
    db_session, workspace, monkeypatch
) -> None:
    _gate_on(monkeypatch)
    fixture = await build_sendable(db_session, workspace, suffix="pg4")
    address = await _sender_email(db_session, fixture.sender_id)
    for _ in range(3):
        await _probe(db_session, workspace, address, folder="inbox")

    slots = await sender_pool.load_slots(
        db_session, workspace, fixture.campaign_id, now=NOW
    )

    assert slots[0].available is True


@pytest.mark.asyncio
async def test_the_pool_ignores_placement_when_the_gate_is_off(
    db_session, workspace
) -> None:
    fixture = await build_sendable(db_session, workspace, suffix="pg5")

    slots = await sender_pool.load_slots(
        db_session, workspace, fixture.campaign_id, now=NOW
    )

    assert slots[0].available is True


# ------------------------------------------------------------- send boundary
async def _status(outbox_id: uuid.UUID) -> OutboxStatus:
    async with get_sessionmaker()() as s:
        row = (
            await s.execute(select(OutboxMessage).where(OutboxMessage.id == outbox_id))
        ).scalar_one()
        return row.status


@pytest.mark.asyncio
async def test_a_message_from_a_spam_folder_mailbox_is_deferred_not_sent(
    db_session, sendable
) -> None:
    address = await _sender_email(db_session, sendable.sender_id)
    for _ in range(3):
        await _probe(db_session, sendable.workspace_id, address, folder="spam")

    provider = MockEmailProvider()
    results = await OutboxWorker(
        provider,
        sending_settings(placement_gate_enabled=True),
        now_fn=lambda: NOW,
    ).run_once()

    assert provider.delivered_count == 0
    assert [r.outcome for r in results] == ["deferred"], results
    assert await _status(sendable.outbox_id) is OutboxStatus.DEFERRED


@pytest.mark.asyncio
async def test_a_message_from_an_inbox_mailbox_is_sent(db_session, sendable) -> None:
    address = await _sender_email(db_session, sendable.sender_id)
    for _ in range(3):
        await _probe(db_session, sendable.workspace_id, address, folder="inbox")

    provider = MockEmailProvider()
    results = await OutboxWorker(
        provider,
        sending_settings(placement_gate_enabled=True),
        now_fn=lambda: NOW,
    ).run_once()

    assert [r.outcome for r in results] == ["sent"], results
    assert provider.delivered_count == 1
