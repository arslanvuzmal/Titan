"""Recording replies, and the ordering that makes re-reading a mailbox safe.

Runs against a real PostgreSQL. The guarantees under test are the unique
constraint on ``(workspace_id, provider_inbound_id)`` and the transaction
boundary around suppression -- neither of which an in-memory database would
reproduce faithfully.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from titan.db.enums import LeadStatus, ReplyClass, SuppressionReason
from titan.db.models import Lead, Message
from titan.db.models.compliance import SuppressionEntry
from titan.db.models.messaging import InboundMessage as InboundMessageRow
from titan.db.models.messaging import ReplyClassification as ReplyClassificationRow
from titan.db.session import workspace_unit_of_work
from titan.delivery.inbound import ingest_inbound, synthetic_inbound_id
from titan.delivery.mailbox import RawMessage
from titan.delivery.reply_collector import ReplyCollector
from titan.intelligence.replies import InboundMessage, ReplyKind

from tests.delivery.conftest import build_sendable

pytestmark = pytest.mark.asyncio

REPLIED_AT = dt.datetime(2026, 8, 10, 9, 14, 22, tzinfo=dt.UTC)


def human_reply(
    from_email: str, body: str = "Yes please, send pricing."
) -> InboundMessage:
    return InboundMessage(
        from_email=from_email,
        subject="Re: A broken button on your booking page",
        body_text=body,
        headers={"From": from_email, "Message-ID": "<r1@theirs.test>"},
    )


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


async def test_a_reply_is_recorded_not_just_acted_on(db_session, workspace):
    """The gap this closes.

    Both tables existed from the first migration with no writer anywhere, so
    acting on a reply left a suppressed address with nothing saying who asked
    for it and a lead marked ``replied`` with no reply attached. An audit could
    not answer "why did we stop writing to this person?".
    """
    fixture = await build_sendable(db_session, workspace)

    async with workspace_unit_of_work(workspace) as session:
        result = await ingest_inbound(
            session,
            workspace_id=workspace,
            message=human_reply(fixture.to_email),
            lead_id=fixture.lead_id,
            provider_inbound_id="reply-001@theirs.test",
            received_at=REPLIED_AT,
        )

    assert result.duplicate is False
    assert result.kind is ReplyKind.HUMAN
    assert result.sequence_stopped is True

    async with workspace_unit_of_work(workspace) as session:
        row = (
            await session.execute(
                select(InboundMessageRow).where(
                    InboundMessageRow.provider_inbound_id == "reply-001@theirs.test"
                )
            )
        ).scalar_one()
        assert row.from_email_normalized == fixture.to_email
        assert row.lead_id == fixture.lead_id
        assert row.received_at == REPLIED_AT

        classification = (
            await session.execute(
                select(ReplyClassificationRow).where(
                    ReplyClassificationRow.inbound_message_id == row.id
                )
            )
        ).scalar_one()
        assert classification.decided_by == "rules"


async def test_a_human_reply_is_recorded_as_unknown_not_as_interested(
    db_session, workspace
):
    """A reply carrying no intent signal must not be assigned one.

    The rules prove a person wrote; where nothing indicates what they want,
    inventing a class would put a fabricated intent into the client history
    where a reader takes it as fact. The authoritative signal that a human
    replied is the lead status, which is set on the same path.
    """
    fixture = await build_sendable(db_session, workspace)

    async with workspace_unit_of_work(workspace) as session:
        await ingest_inbound(
            session,
            workspace_id=workspace,
            message=human_reply(fixture.to_email, body="Got it, cheers."),
            lead_id=fixture.lead_id,
            provider_inbound_id="reply-002@theirs.test",
            received_at=REPLIED_AT,
        )

    async with workspace_unit_of_work(workspace) as session:
        classification = (
            (await session.execute(select(ReplyClassificationRow))).scalars().one()
        )
        assert classification.reply_class is ReplyClass.UNKNOWN
        # Confidence records how much was actually established: the human verdict
        # is reached by the absence of automation markers, which is weak.
        assert classification.confidence < 0.75

        lead = await session.get(Lead, fixture.lead_id)
        assert lead.status is LeadStatus.REPLIED
        assert lead.replied_at == REPLIED_AT


async def test_replied_at_is_when_they_wrote_not_when_we_read_it(db_session, workspace):
    """A poller that was down for a day must not restamp every reply.

    replied_at drives follow-up timing and the client history an operator reads
    before a call. Recording the moment the poller happened to restart would
    make every reply look simultaneous.
    """
    fixture = await build_sendable(db_session, workspace)
    much_later = dt.datetime(2026, 8, 11, 18, 0, tzinfo=dt.UTC)

    async with workspace_unit_of_work(workspace) as session:
        await ingest_inbound(
            session,
            workspace_id=workspace,
            message=human_reply(fixture.to_email),
            lead_id=fixture.lead_id,
            provider_inbound_id="reply-003@theirs.test",
            received_at=REPLIED_AT,
            now=much_later,
        )

    async with workspace_unit_of_work(workspace) as session:
        lead = await session.get(Lead, fixture.lead_id)
        assert lead.replied_at == REPLIED_AT


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_the_same_message_twice_applies_its_effects_once(db_session, workspace):
    """Re-reading a folder is normal, not exceptional.

    A second ingest must not move replied_at forward to the moment of the
    re-read, which would quietly rewrite when the person answered.
    """
    fixture = await build_sendable(db_session, workspace)
    message = human_reply(fixture.to_email)

    async with workspace_unit_of_work(workspace) as session:
        first = await ingest_inbound(
            session,
            workspace_id=workspace,
            message=message,
            lead_id=fixture.lead_id,
            provider_inbound_id="reply-dup@theirs.test",
            received_at=REPLIED_AT,
        )
    async with workspace_unit_of_work(workspace) as session:
        second = await ingest_inbound(
            session,
            workspace_id=workspace,
            message=message,
            lead_id=fixture.lead_id,
            provider_inbound_id="reply-dup@theirs.test",
            received_at=REPLIED_AT + dt.timedelta(days=1),
        )

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.inbound_message_id == first.inbound_message_id
    assert second.sequence_stopped is False

    async with workspace_unit_of_work(workspace) as session:
        rows = (await session.execute(select(InboundMessageRow))).scalars().all()
        assert len(rows) == 1
        lead = await session.get(Lead, fixture.lead_id)
        assert lead.replied_at == REPLIED_AT


async def test_a_message_with_no_id_falls_back_to_a_content_hash(db_session, workspace):
    """No intake path can skip deduplication by omitting an id.

    A message violating RFC 5322 by carrying no Message-ID would otherwise be
    re-ingested on every poll, re-suppressing and re-stamping forever.
    """
    fixture = await build_sendable(db_session, workspace)
    message = human_reply(fixture.to_email)

    for _ in range(2):
        async with workspace_unit_of_work(workspace) as session:
            result = await ingest_inbound(
                session,
                workspace_id=workspace,
                message=message,
                lead_id=fixture.lead_id,
                received_at=REPLIED_AT,
            )

    assert result.duplicate is True
    async with workspace_unit_of_work(workspace) as session:
        row = (await session.execute(select(InboundMessageRow))).scalars().one()
        assert row.provider_inbound_id == synthetic_inbound_id(message)


# ---------------------------------------------------------------------------
# Bounces
# ---------------------------------------------------------------------------


async def test_a_bounce_suppresses_the_failed_recipient_not_the_daemon(
    db_session, workspace
):
    """The address that rejected the mail is not the address that reported it.

    Suppressing MAILER-DAEMON@ leaves the real mailbox in rotation to bounce
    again on every send -- each one costing sender reputation -- while blocking
    a postmaster address nobody was writing to.
    """
    fixture = await build_sendable(db_session, workspace)
    daemon = "mailer-daemon@mx.fixture-business.test"

    bounce = InboundMessage(
        from_email=daemon,
        subject="Undeliverable: A broken button on your booking page",
        body_text="Reason: 5.1.1 user unknown",
        headers={"From": daemon},
        content_type='multipart/report; report-type="delivery-status"',
    )

    async with workspace_unit_of_work(workspace) as session:
        result = await ingest_inbound(
            session,
            workspace_id=workspace,
            message=bounce,
            lead_id=fixture.lead_id,
            provider_inbound_id="dsn-77@mx.fixture-business.test",
            suppression_target=fixture.to_email,
            received_at=REPLIED_AT,
        )

    assert result.kind is ReplyKind.BOUNCE
    assert result.suppressed is True

    async with workspace_unit_of_work(workspace) as session:
        entries = (await session.execute(select(SuppressionEntry))).scalars().all()
        suppressed = {e.normalized_value for e in entries}
        assert fixture.to_email in suppressed
        assert daemon not in suppressed
        assert all(e.reason is SuppressionReason.HARD_BOUNCE for e in entries)


async def test_a_bounce_does_not_stop_the_sequence_as_if_someone_replied(
    db_session, workspace
):
    """A machine reporting a failure is not a person answering.

    The address is suppressed, so nothing more is sent regardless -- but marking
    the lead as REPLIED would put a bounce into the funnel as engagement and
    make reply-rate meaningless.
    """
    fixture = await build_sendable(db_session, workspace)

    async with workspace_unit_of_work(workspace) as session:
        result = await ingest_inbound(
            session,
            workspace_id=workspace,
            message=InboundMessage(
                from_email="mailer-daemon@mx.fixture-business.test",
                subject="Undeliverable",
                body_text="5.1.1 user unknown",
                content_type='multipart/report; report-type="delivery-status"',
            ),
            lead_id=fixture.lead_id,
            provider_inbound_id="dsn-78@mx.fixture-business.test",
            suppression_target=fixture.to_email,
            received_at=REPLIED_AT,
        )

    assert result.sequence_stopped is False
    async with workspace_unit_of_work(workspace) as session:
        lead = await session.get(Lead, fixture.lead_id)
        assert lead.status is not LeadStatus.REPLIED


async def test_a_soft_bounce_suppresses_nothing(db_session, workspace):
    """4.x.x is temporary. The mailbox is fine and will accept mail later."""
    fixture = await build_sendable(db_session, workspace)

    async with workspace_unit_of_work(workspace) as session:
        result = await ingest_inbound(
            session,
            workspace_id=workspace,
            message=InboundMessage(
                from_email="mailer-daemon@mx.fixture-business.test",
                subject="Delivery Status Notification (Delay)",
                body_text="4.2.2 mailbox full; will retry",
                content_type='multipart/report; report-type="delivery-status"',
            ),
            lead_id=fixture.lead_id,
            provider_inbound_id="dsn-soft@mx.fixture-business.test",
            suppression_target=fixture.to_email,
            received_at=REPLIED_AT,
        )

    assert result.kind is ReplyKind.BOUNCE
    assert result.suppressed is False
    async with workspace_unit_of_work(workspace) as session:
        assert (await session.execute(select(SuppressionEntry))).scalars().all() == []


async def test_an_unsubscribe_suppresses_and_stops(db_session, workspace):
    fixture = await build_sendable(db_session, workspace)

    async with workspace_unit_of_work(workspace) as session:
        result = await ingest_inbound(
            session,
            workspace_id=workspace,
            message=human_reply(
                fixture.to_email, body="Please remove me from your list."
            ),
            lead_id=fixture.lead_id,
            provider_inbound_id="unsub-1@theirs.test",
            received_at=REPLIED_AT,
        )

    assert result.kind is ReplyKind.UNSUBSCRIBE
    assert result.suppressed is True
    assert result.sequence_stopped is True

    async with workspace_unit_of_work(workspace) as session:
        entry = (await session.execute(select(SuppressionEntry))).scalars().one()
        assert entry.normalized_value == fixture.to_email
        assert entry.reason is SuppressionReason.UNSUBSCRIBE


# ---------------------------------------------------------------------------
# The collector
# ---------------------------------------------------------------------------


class FakeMailbox:
    """An in-memory mailbox. Records what was marked read and when."""

    def __init__(self, messages: list[RawMessage], *, fail_mark: bool = False) -> None:
        self.messages = messages
        self.marked: list[str] = []
        self.fail_mark = fail_mark

    async def fetch_unread(self, limit: int) -> list[RawMessage]:
        return self.messages[:limit]

    async def mark_read(self, uids: list[str]) -> None:
        if self.fail_mark:
            raise RuntimeError("IMAP STORE failed")
        self.marked.extend(uids)


def raw_reply(*, from_email: str, in_reply_to: str, message_id: str) -> bytes:
    return (
        f"From: Sam <{from_email}>\r\n"
        f"To: outreach@arslanvuzmallone.com\r\n"
        f"Subject: Re: A broken button on your booking page\r\n"
        f"Message-ID: <{message_id}>\r\n"
        f"In-Reply-To: <{in_reply_to}>\r\n"
        f"Date: Mon, 10 Aug 2026 09:14:22 +0000\r\n"
        f'Content-Type: text/plain; charset="utf-8"\r\n'
        f"\r\n"
        f"That is useful, thank you. What would it cost?\r\n"
    ).encode()


async def test_collector_matches_a_reply_by_its_threading_header(db_session, workspace):
    """The primary attribution path.

    Matching on the Message-ID Titan recorded when it sent is exact. The sender
    fallback is not: a person may answer from a different address than the one
    written to, and a shared mailbox may answer for several leads.
    """
    fixture = await build_sendable(db_session, workspace)
    sent_id = "abc123def456@arslanvuzmallone.com"

    async with workspace_unit_of_work(workspace) as session:
        message = await session.get(Message, fixture.message_id)
        message.provider_message_id = sent_id

    mailbox = FakeMailbox(
        [
            RawMessage(
                uid="7",
                raw=raw_reply(
                    from_email=fixture.to_email,
                    in_reply_to=sent_id,
                    message_id="their-reply-1@theirs.test",
                ),
            )
        ]
    )
    collector = ReplyCollector(mailbox, mailbox_address="outreach@arslanvuzmallone.com")

    result = await collector.run_once()

    assert result.fetched == 1
    assert result.ingested == 1
    assert result.unmatched == 0
    assert result.stopped_sequences == 1
    assert mailbox.marked == ["7"]

    async with workspace_unit_of_work(workspace) as session:
        row = (await session.execute(select(InboundMessageRow))).scalars().one()
        assert row.lead_id == fixture.lead_id
        assert row.in_reply_to_message_id == fixture.message_id


async def test_collector_deduplicates_a_folder_it_has_already_read(db_session, workspace):
    """Marking read can fail, or a deploy can land between commit and STORE.

    Either way the next cycle sees the same messages. That has to be free.
    """
    fixture = await build_sendable(db_session, workspace)
    sent_id = "dedupe-me@arslanvuzmallone.com"

    async with workspace_unit_of_work(workspace) as session:
        message = await session.get(Message, fixture.message_id)
        message.provider_message_id = sent_id

    raw = RawMessage(
        uid="9",
        raw=raw_reply(
            from_email=fixture.to_email,
            in_reply_to=sent_id,
            message_id="their-reply-2@theirs.test",
        ),
    )
    collector = ReplyCollector(
        FakeMailbox([raw]), mailbox_address="outreach@arslanvuzmallone.com"
    )

    first = await collector.run_once()
    second = await collector.run_once()

    assert first.ingested == 1
    assert second.ingested == 0
    assert second.duplicates == 1

    async with workspace_unit_of_work(workspace) as session:
        assert (
            len((await session.execute(select(InboundMessageRow))).scalars().all()) == 1
        )


async def test_collector_skips_titans_own_messages(db_session, workspace):
    """A copy of our own outreach sitting in the polled folder.

    Ingesting it would classify our own message as a reply and stop the very
    sequence that sent it.
    """
    await build_sendable(db_session, workspace)
    own = "outreach@arslanvuzmallone.com"

    mailbox = FakeMailbox(
        [
            RawMessage(
                uid="3",
                raw=raw_reply(
                    from_email=own,
                    in_reply_to="whatever@arslanvuzmallone.com",
                    message_id="our-own@arslanvuzmallone.com",
                ),
            )
        ]
    )
    collector = ReplyCollector(mailbox, mailbox_address=own)

    result = await collector.run_once()

    assert result.skipped_own == 1
    assert result.ingested == 0
    assert mailbox.marked == ["3"]

    async with workspace_unit_of_work(workspace) as session:
        assert (await session.execute(select(InboundMessageRow))).scalars().all() == []


async def test_collector_leaves_an_unassignable_message_unread(db_session, workspace):
    """No match and no default workspace means nowhere to put it.

    Left unread deliberately: this is a configuration gap, and the reply should
    still be in the mailbox once it is fixed rather than silently consumed.
    """
    mailbox = FakeMailbox(
        [
            RawMessage(
                uid="11",
                raw=raw_reply(
                    from_email="stranger@nowhere.test",
                    in_reply_to="unknown@arslanvuzmallone.com",
                    message_id="stranger-1@nowhere.test",
                ),
            )
        ]
    )
    collector = ReplyCollector(
        mailbox,
        mailbox_address="outreach@arslanvuzmallone.com",
        default_workspace_id=None,
    )

    result = await collector.run_once()

    assert result.failed == 1
    assert result.ingested == 0
    assert mailbox.marked == []


async def test_collector_records_an_unmatched_reply_in_the_default_workspace(
    db_session, workspace
):
    """Somebody writing in cold still belongs in the client history."""
    mailbox = FakeMailbox(
        [
            RawMessage(
                uid="12",
                raw=raw_reply(
                    from_email="stranger@nowhere.test",
                    in_reply_to="unknown@arslanvuzmallone.com",
                    message_id="stranger-2@nowhere.test",
                ),
            )
        ]
    )
    collector = ReplyCollector(
        mailbox,
        mailbox_address="outreach@arslanvuzmallone.com",
        default_workspace_id=workspace,
    )

    result = await collector.run_once()

    assert result.ingested == 1
    assert result.unmatched == 1
    assert mailbox.marked == ["12"]

    async with workspace_unit_of_work(workspace) as session:
        row = (await session.execute(select(InboundMessageRow))).scalars().one()
        assert row.lead_id is None


async def test_a_failure_to_mark_read_does_not_lose_the_ingest(db_session, workspace):
    """The ordering that makes the whole loop safe.

    The commit happens first. If the IMAP flag were set first and the write then
    failed, somebody's unsubscribe would be marked handled and never seen again.
    In this order the cost of a failure is one re-read, which deduplicates.
    """
    fixture = await build_sendable(db_session, workspace)
    sent_id = "mark-fails@arslanvuzmallone.com"

    async with workspace_unit_of_work(workspace) as session:
        message = await session.get(Message, fixture.message_id)
        message.provider_message_id = sent_id

    mailbox = FakeMailbox(
        [
            RawMessage(
                uid="13",
                raw=raw_reply(
                    from_email=fixture.to_email,
                    in_reply_to=sent_id,
                    message_id="their-reply-3@theirs.test",
                ),
            )
        ],
        fail_mark=True,
    )
    collector = ReplyCollector(mailbox, mailbox_address="outreach@arslanvuzmallone.com")

    result = await collector.run_once()

    assert result.ingested == 1
    assert mailbox.marked == []
    async with workspace_unit_of_work(workspace) as session:
        assert (
            len((await session.execute(select(InboundMessageRow))).scalars().all()) == 1
        )


async def test_one_broken_message_does_not_stop_the_batch(db_session, workspace):
    """The messages behind a poison one include the opt-outs."""
    fixture = await build_sendable(db_session, workspace)
    sent_id = "batch-survivor@arslanvuzmallone.com"

    async with workspace_unit_of_work(workspace) as session:
        message = await session.get(Message, fixture.message_id)
        message.provider_message_id = sent_id

    mailbox = FakeMailbox(
        [
            RawMessage(uid="20", raw=b"\xff\xfe not a message at all"),
            RawMessage(
                uid="21",
                raw=raw_reply(
                    from_email=fixture.to_email,
                    in_reply_to=sent_id,
                    message_id="their-reply-4@theirs.test",
                ),
            ),
        ]
    )
    collector = ReplyCollector(
        mailbox,
        mailbox_address="outreach@arslanvuzmallone.com",
        default_workspace_id=workspace,
    )

    result = await collector.run_once()

    assert result.fetched == 2
    assert result.ingested >= 1
    assert "21" in mailbox.marked

    async with workspace_unit_of_work(workspace) as session:
        rows = (await session.execute(select(InboundMessageRow))).scalars().all()
        assert any(r.lead_id == fixture.lead_id for r in rows)


async def test_workspace_isolation_holds_for_recorded_replies(db_session, workspace):
    """A reply is only ever visible in the workspace that sent to it."""
    fixture = await build_sendable(db_session, workspace)
    other = uuid.uuid4()

    async with workspace_unit_of_work(workspace) as session:
        await ingest_inbound(
            session,
            workspace_id=workspace,
            message=human_reply(fixture.to_email),
            lead_id=fixture.lead_id,
            provider_inbound_id="isolation-1@theirs.test",
            received_at=REPLIED_AT,
        )

    async with workspace_unit_of_work(other) as session:
        rows = (await session.execute(select(InboundMessageRow))).scalars().all()
        assert rows == []
