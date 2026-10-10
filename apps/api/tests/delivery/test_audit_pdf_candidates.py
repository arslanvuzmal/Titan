"""The personal PDF is rendered while a draft waits for approval, not after.

A queued email to a free mailbox leaves within seconds; the PDF sweep runs
every five minutes. Rendering only for queued mail meant the first email
left without its PDF -- which the end-to-end test of 10 Oct 2026 would have.
"""

from __future__ import annotations

import pytest
from coldops.activities.audit_pdf import _candidates
from coldops.db.enums import DraftStatus
from coldops.db.models import Message, MessageDraft, OutboxMessage
from coldops.db.session import get_sessionmaker
from sqlalchemy import delete, update

pytestmark = pytest.mark.integration


async def _only_the_draft(sendable, status: DraftStatus) -> None:
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            delete(OutboxMessage).where(OutboxMessage.id == sendable.outbox_id)
        )
        await s.execute(delete(Message).where(Message.id == sendable.message_id))
        await s.execute(
            update(MessageDraft)
            .where(MessageDraft.id == sendable.draft_id)
            .values(status=status)
        )


@pytest.mark.asyncio
async def test_a_draft_awaiting_approval_is_rendered_ahead(db_session, sendable) -> None:
    await _only_the_draft(sendable, DraftStatus.AWAITING_APPROVAL)
    assert (sendable.draft_id, sendable.lead_id) in await _candidates(
        sendable.workspace_id
    )


@pytest.mark.asyncio
async def test_queued_mail_is_still_rendered(db_session, sendable) -> None:
    assert (sendable.draft_id, sendable.lead_id) in await _candidates(
        sendable.workspace_id
    )


@pytest.mark.asyncio
async def test_a_rejected_draft_is_not(db_session, sendable) -> None:
    await _only_the_draft(sendable, DraftStatus.REJECTED)
    assert await _candidates(sendable.workspace_id) == []
