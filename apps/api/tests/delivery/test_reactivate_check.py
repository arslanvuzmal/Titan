"""``coldops reactivate`` reports, and changes nothing."""

from __future__ import annotations

import argparse

import pytest
from coldops import cli_reactivate
from coldops.db.models import OutboxMessage, SenderIdentity, Workspace
from coldops.db.session import get_sessionmaker
from sqlalchemy import select

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_the_checklist_is_read_only(db_session, sendable, capsys) -> None:
    async with get_sessionmaker()() as s:
        slug = (
            await s.execute(
                select(Workspace.slug).where(Workspace.id == sendable.workspace_id)
            )
        ).scalar_one()
        before = (
            (await s.get(SenderIdentity, sendable.sender_id)).is_active,
            (await s.get(OutboxMessage, sendable.outbox_id)).status,
        )

    assert await cli_reactivate._run(argparse.Namespace(workspace=slug)) == 0
    out = capsys.readouterr().out
    assert "not yet:" in out  # no placement readings, so nothing is ready
    assert "0 of 1 mailboxes meet the bar. Nothing was changed." in out
    assert "queued emails" in out

    async with get_sessionmaker()() as s:
        after = (
            (await s.get(SenderIdentity, sendable.sender_id)).is_active,
            (await s.get(OutboxMessage, sendable.outbox_id)).status,
        )
    assert after == before
