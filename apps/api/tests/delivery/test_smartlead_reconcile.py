"""Giving Smartlead's sends a record in Titan's own model.

The CRM, the bounce escalation and every outcome query read ``messages``. For
real outreach that table was empty, because Smartlead delivers and Titan never
learned it happened -- so a complete, working CRM showed nothing and four real
bounces could not be counted.

The tests that matter are about honesty and idempotence: that a send is only
recorded when there is a real approved draft to attribute it to, and that
polling the same row repeatedly produces one message rather than many.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from titan.db.enums import DraftStatus, MessageState
from titan.db.models import Lead, Message, MessageDraft
from titan.delivery.smartlead_reconcile import dedupe_key, reconcile_send

from tests.delivery.conftest import build_sendable

pytestmark = pytest.mark.asyncio

SENT_AT = dt.datetime(2026, 8, 14, 9, 12, tzinfo=dt.UTC)


async def _fixture(session, workspace_id, *, suffix: str, approved: int = 1):
    """A lead with a campaign, a verified sender and `approved` approved drafts.

    Built on the delivery fixture so the lead is one that could genuinely have
    been sent -- a reconciled message that could never have existed would make
    the test vacuous.
    """
    built = await build_sendable(session, workspace_id, suffix=suffix)
    lead = await session.get(Lead, built.lead_id)
    lead.smartlead_normalized_email = built.to_email

    drafts = list(
        (
            await session.execute(
                select(MessageDraft).where(MessageDraft.lead_id == lead.id)
            )
        )
        .scalars()
        .all()
    )
    # build_sendable makes one approved draft; add any extra steps asked for.
    for index in range(1, max(approved, 1)):
        extra = MessageDraft(
            workspace_id=workspace_id,
            lead_id=lead.id,
            campaign_id=built.campaign_id,
            contact_channel_id=built.channel_id,
            idempotency_key=f"draft-{suffix}-step{index}",
            status=DraftStatus.APPROVED,
            subject=f"Following up on {suffix} step {index}",
            body_text=drafts[0].body_text,
            template_key=f"outreach_v2_followup{index}",
        )
        session.add(extra)
        drafts.append(extra)

    if approved == 0:
        # Rejected rather than deleted: the delivery fixture's own message
        # references the draft, so removing it would fail a foreign key and
        # test the fixture instead of the reconciler.
        for draft in drafts:
            draft.status = DraftStatus.REJECTED
        drafts = []
    await session.flush()
    return lead, drafts


async def _reconcile(session, workspace_id, lead, *, stats_id, subject=None, step=1):
    return await reconcile_send(
        session,
        workspace_id=workspace_id,
        lead=lead,
        stats_id=stats_id,
        to_email=lead.smartlead_normalized_email,
        subject=subject,
        sequence_number=step,
        sent_at=SENT_AT,
    )


# ------------------------------------------------------------------ the record


async def test_a_smartlead_send_becomes_a_titan_message(db_session, workspace) -> None:
    lead, drafts = await _fixture(db_session, workspace, suffix="one")

    outcome = await _reconcile(db_session, workspace, lead, stats_id="s-1")

    assert outcome.created is True
    message = await db_session.get(Message, outcome.message_id)
    assert message.state is MessageState.SENT
    assert message.sent_at == SENT_AT
    assert message.provider == "smartlead"
    assert message.provider_message_id == "s-1"
    assert message.draft_id == drafts[0].id


async def test_the_message_points_at_a_draft_a_human_approved(
    db_session, workspace
) -> None:
    """Not a fabrication.

    Every lead Smartlead holds was drafted and approved here before being handed
    over, which is the whole reason `draft_id`'s NOT NULL can be honoured rather
    than relaxed to let external sends in.
    """
    lead, _drafts = await _fixture(db_session, workspace, suffix="two")

    outcome = await _reconcile(db_session, workspace, lead, stats_id="s-2")

    message = await db_session.get(Message, outcome.message_id)
    draft = await db_session.get(MessageDraft, message.draft_id)
    assert draft.status is DraftStatus.APPROVED


# ------------------------------------------------------------------ idempotence


async def test_polling_the_same_row_twice_creates_one_message(
    db_session, workspace
) -> None:
    """The poller re-reads everything every hour by design.

    Keyed on `stats_id`, so a second read finds the message it already made.
    """
    lead, _ = await _fixture(db_session, workspace, suffix="three")

    first = await _reconcile(db_session, workspace, lead, stats_id="s-3")
    second = await _reconcile(db_session, workspace, lead, stats_id="s-3")

    assert first.created is True
    assert second.created is False
    assert second.message_id == first.message_id

    rows = (
        (
            await db_session.execute(
                select(Message).where(Message.dedupe_key == dedupe_key("s-3"))
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_each_sequence_step_is_its_own_message(db_session, workspace) -> None:
    """A lead gets several sends. One row per statistics row, not per lead."""
    lead, _ = await _fixture(db_session, workspace, suffix="four", approved=2)

    step_one = await _reconcile(db_session, workspace, lead, stats_id="s-4a", step=1)
    step_two = await _reconcile(db_session, workspace, lead, stats_id="s-4b", step=2)

    assert step_one.created and step_two.created
    assert step_one.message_id != step_two.message_id


# -------------------------------------------------------------------- refusals


async def test_a_send_with_no_draft_is_refused_rather_than_invented(
    db_session, workspace
) -> None:
    """Attributing to the wrong draft is worse than recording nothing.

    Every downstream read -- the CRM timeline, the variant A/B test -- would
    inherit the error with no way to notice it.
    """
    lead, _ = await _fixture(db_session, workspace, suffix="five", approved=0)

    outcome = await _reconcile(db_session, workspace, lead, stats_id="s-5")

    assert outcome.created is False
    assert outcome.skipped_reason == "no draft to attribute to"


async def test_the_subject_wins_over_the_step_number(db_session, workspace) -> None:
    """Smartlead reports the subject it actually used.

    The step number is Smartlead's own bookkeeping and is null on a fair share
    of rows, so the subject is the more reliable of the two.
    """
    lead, drafts = await _fixture(db_session, workspace, suffix="six", approved=2)

    outcome = await _reconcile(
        db_session,
        workspace,
        lead,
        stats_id="s-6",
        subject=drafts[1].subject,
        step=1,
    )

    message = await db_session.get(Message, outcome.message_id)
    assert message.draft_id == drafts[1].id, "the subject should have decided this"


async def test_an_unknown_sender_falls_back_rather_than_losing_the_send(
    db_session, workspace
) -> None:
    """Smartlead holds mailboxes Titan was never told about.

    Losing the whole send record over a missing sender row would trade a precise
    gap for a total one.
    """
    lead, _ = await _fixture(db_session, workspace, suffix="seven")

    outcome = await reconcile_send(
        db_session,
        workspace_id=workspace,
        lead=lead,
        stats_id="s-7",
        to_email=lead.smartlead_normalized_email,
        subject=None,
        sequence_number=1,
        sent_at=SENT_AT,
        from_email="a-mailbox-titan-never-heard-of@example.test",
    )

    assert outcome.created is True
    message = await db_session.get(Message, outcome.message_id)
    assert message.sender_identity_id is not None


async def test_a_reconciled_message_is_identifiable_as_one(db_session, workspace) -> None:
    """The dedupe key says where the row came from.

    It also cannot collide with a key the outbox worker generates, which matters
    because both write to the same table.
    """
    lead, _ = await _fixture(db_session, workspace, suffix="eight")

    outcome = await _reconcile(db_session, workspace, lead, stats_id="s-8")

    message = await db_session.get(Message, outcome.message_id)
    assert message.dedupe_key.startswith("smartlead:")
    assert uuid.UUID(str(message.id))
