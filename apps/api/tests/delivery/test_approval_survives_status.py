"""A draft approved by a person is sent, though approving it moved its version.

Reproduces the live failure of 10 Oct 2026 against the real outbox: the
approval is recorded, the draft's status then changes through the ORM (which
is what moves ``version``), and the message must still go -- while an approval
recorded the old way, with no fingerprint, is still refused as before.
"""

from __future__ import annotations

import datetime as dt

import pytest
from coldops.db.enums import DraftStatus
from coldops.db.models import MessageApproval, MessageDraft
from coldops.db.session import get_sessionmaker
from coldops.delivery.providers.mock import MockEmailProvider
from coldops.policy import approval_content

from .conftest import NOW
from .test_outbox_delivery import run_worker

pytestmark = pytest.mark.integration


async def _approve_then_move_status(sendable, *, fingerprint: bool) -> int:
    async with get_sessionmaker()() as s, s.begin():
        draft = await s.get(MessageDraft, sendable.draft_id)
        assert draft is not None
        s.add(
            MessageApproval(
                workspace_id=sendable.workspace_id,
                draft_id=draft.id,
                draft_version=draft.version,
                decision_seq=2,
                decision="approved",
                decided_at=NOW + dt.timedelta(seconds=1),
                expires_at=NOW + dt.timedelta(days=7),
                policy_snapshot=(
                    {approval_content.CONTENT_KEY: approval_content.of_draft(draft)}
                    if fingerprint
                    else {}
                ),
            )
        )
        await s.flush()
        # What the approval route and queue_message do next.
        draft.status = DraftStatus.QUEUED
        await s.flush()
        return draft.version


@pytest.mark.asyncio
async def test_an_approved_draft_is_sent_after_its_status_moves(
    db_session, sendable
) -> None:
    version = await _approve_then_move_status(sendable, fingerprint=True)
    assert version > 1, "the status change should have moved the counter"
    provider = MockEmailProvider()
    results = await run_worker(provider)
    assert [r.outcome for r in results] == ["sent"], results
    assert provider.delivered_count == 1


@pytest.mark.asyncio
async def test_an_old_style_approval_is_still_refused_the_old_way(
    db_session, sendable
) -> None:
    await _approve_then_move_status(sendable, fingerprint=False)
    provider = MockEmailProvider()
    results = await run_worker(provider)
    assert provider.delivered_count == 0
    assert [r.outcome for r in results] == ["blocked"]
