"""Delivery-layer proofs.

These are the acceptance criteria from mission section 29 ("Delivery") expressed
as executable tests, run against a real PostgreSQL and a mock provider that
records every send.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from sqlalchemy import select, update
from titan.db.enums import (
    CampaignStatus,
    ContactSource,
    LeadStatus,
    MessageState,
    OutboxStatus,
    SuppressionReason,
    VerificationStatus,
)
from titan.db.models import (
    Campaign,
    ContactChannel,
    Lead,
    Message,
    OutboxMessage,
    Workspace,
)
from titan.db.session import get_sessionmaker
from titan.delivery.outbox_worker import OutboxWorker
from titan.delivery.providers.base import SendErrorKind
from titan.delivery.providers.mock import MockEmailProvider
from titan.delivery.suppression import is_suppressed, suppress

from .conftest import NOW, build_sendable, sending_settings

pytestmark = pytest.mark.integration


def worker(provider: MockEmailProvider, **setting_overrides) -> OutboxWorker:
    return OutboxWorker(
        provider,
        sending_settings(**setting_overrides),
        now_fn=lambda: NOW,
    )


async def run_worker(provider: MockEmailProvider, **overrides):
    return await worker(provider, **overrides).run_once()


async def outbox_row(session, outbox_id: uuid.UUID) -> OutboxMessage:
    return (
        await session.execute(select(OutboxMessage).where(OutboxMessage.id == outbox_id))
    ).scalar_one()


# ==========================================================================
# The control case
# ==========================================================================
@pytest.mark.asyncio
async def test_authorized_message_is_delivered_once(db_session, sendable) -> None:
    provider = MockEmailProvider()
    results = await run_worker(provider)

    assert [r.outcome for r in results] == ["sent"], results
    assert provider.delivered_count == 1
    assert provider.recipients() == [sendable.to_email]

    async with get_sessionmaker()() as s:
        row = await outbox_row(s, sendable.outbox_id)
        assert row.status is OutboxStatus.SENT
        assert row.sent_at is not None

        message = await s.get(Message, sendable.message_id)
        assert message.state is MessageState.SENT
        assert message.provider_message_id

        lead = await s.get(Lead, sendable.lead_id)
        assert lead.status is LeadStatus.CONTACTED
        assert lead.last_contacted_at is not None


# ==========================================================================
# Exactly-once under retry and concurrency (invariants 11, 14)
# ==========================================================================
@pytest.mark.asyncio
async def test_repeated_worker_runs_do_not_resend(db_session, sendable) -> None:
    """A second poll cycle must not pick up an already-sent row."""
    provider = MockEmailProvider()
    await run_worker(provider)
    await run_worker(provider)
    await run_worker(provider)
    assert provider.delivered_count == 1


@pytest.mark.asyncio
async def test_concurrent_workers_deliver_exactly_once(db_session, workspace) -> None:
    """Eight workers racing on one queue must deliver each message once.

    This is the core exactly-once claim. FOR UPDATE SKIP LOCKED gives each row
    to exactly one worker; the others skip it rather than blocking.
    """
    fixtures = []
    async with get_sessionmaker()() as s:
        for i in range(12):
            fixtures.append(await build_sendable(s, workspace, suffix=f"conc{i}"))

    provider = MockEmailProvider()
    workers = [worker(provider, outbox_batch_size=4) for _ in range(8)]
    await asyncio.gather(*(w.run_once() for w in workers))

    assert provider.delivered_count == 12, f"delivered {provider.delivered_count}"
    assert len(set(provider.recipients())) == 12, "a recipient received two messages"

    async with get_sessionmaker()() as s:
        statuses = [(await outbox_row(s, f.outbox_id)).status for f in fixtures]
    assert all(st is OutboxStatus.SENT for st in statuses)


@pytest.mark.asyncio
async def test_transient_failure_retries_without_duplicating(
    db_session, sendable
) -> None:
    """A retry after a transient failure sends once, not twice.

    The provider idempotency key is stored on the row before the first attempt,
    so every attempt presents the same key.
    """
    provider = MockEmailProvider(fail_times=2, fail_kind=SendErrorKind.TRANSIENT)

    first = await run_worker(provider)
    assert [r.outcome for r in first] == ["retried"]
    assert provider.delivered_count == 0

    async with get_sessionmaker()() as s, s.begin():
        # Make the row due again, as the backoff timer would.
        await s.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == sendable.outbox_id)
            .values(next_attempt_at=NOW - dt.timedelta(seconds=1))
        )
    second = await run_worker(provider)
    assert [r.outcome for r in second] == ["retried"]

    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == sendable.outbox_id)
            .values(next_attempt_at=NOW - dt.timedelta(seconds=1))
        )
    third = await run_worker(provider)
    assert [r.outcome for r in third] == ["sent"]
    assert provider.delivered_count == 1, "retry produced a duplicate delivery"


@pytest.mark.asyncio
async def test_crash_after_provider_accept_does_not_duplicate(
    db_session, sendable
) -> None:
    """The unacknowledged-success case.

    The provider accepted, then the worker died before recording it. On retry
    the same idempotency key is presented, so the provider collapses it.
    """
    provider = MockEmailProvider()

    # First attempt: provider accepts, then the worker "crashes" before commit.
    async with get_sessionmaker()() as s:
        row = await s.get(OutboxMessage, sendable.outbox_id)
        email = worker(provider)._render(row, None)  # type: ignore[arg-type]
        accepted = await provider.send(email)
    assert accepted.accepted
    assert provider.delivered_count == 1

    # Row is still PENDING because nothing was committed. A worker retries.
    results = await run_worker(provider)
    assert [r.outcome for r in results] == ["sent"]
    assert provider.delivered_count == 1, "duplicate delivery after simulated crash"


@pytest.mark.asyncio
async def test_dedupe_key_is_unique_per_workspace(db_session, sendable) -> None:
    """A second outbox row with the same dedupe key must be impossible."""
    from sqlalchemy.exc import IntegrityError

    async with get_sessionmaker()() as s:
        original = await outbox_row(s, sendable.outbox_id)
        duplicate = OutboxMessage(
            workspace_id=original.workspace_id,
            message_id=original.message_id,
            draft_id=original.draft_id,
            campaign_id=original.campaign_id,
            lead_id=original.lead_id,
            sender_identity_id=original.sender_identity_id,
            dedupe_key=original.dedupe_key,
            provider_idempotency_key="different-key",
            to_email_normalized=original.to_email_normalized,
            to_domain=original.to_domain,
            next_attempt_at=NOW,
            payload=original.payload,
        )
        s.add(duplicate)
        with pytest.raises(IntegrityError):
            await s.commit()


# ==========================================================================
# Quota enforcement (invariant 14)
# ==========================================================================
@pytest.mark.asyncio
async def test_campaign_quota_caps_concurrent_delivery(db_session, workspace) -> None:
    """20 ready messages against a campaign limit of 5 must deliver 5."""
    async with get_sessionmaker()() as s:
        for i in range(20):
            await build_sendable(
                s,
                workspace,
                suffix=f"quota{i}",
                daily_send_limit=5,
                recipient_domain_daily_limit=50,
            )

    provider = MockEmailProvider()
    workers = [worker(provider, outbox_batch_size=10) for _ in range(6)]
    await asyncio.gather(*(w.run_once() for w in workers))

    assert provider.delivered_count == 5, (
        f"delivered {provider.delivered_count}, limit was 5"
    )


@pytest.mark.asyncio
async def test_recipient_domain_quota_is_enforced(db_session, workspace) -> None:
    """Ten messages to one domain with a per-domain cap of 2 deliver 2."""
    async with get_sessionmaker()() as s:
        for i in range(10):
            await build_sendable(
                s,
                workspace,
                suffix=f"dom{i}",
                to_email=f"person{i}@shared-domain.test",
                recipient_domain_daily_limit=2,
            )

    provider = MockEmailProvider()
    await run_worker(provider, outbox_batch_size=20)
    assert provider.delivered_count == 2


@pytest.mark.asyncio
async def test_quota_exhaustion_defers_rather_than_fails(db_session, workspace) -> None:
    """Mission 15.4: exhaustion must never mark a message permanently failed."""
    async with get_sessionmaker()() as s:
        fixtures = [
            await build_sendable(s, workspace, suffix=f"defer{i}", daily_send_limit=1)
            for i in range(4)
        ]

    provider = MockEmailProvider()
    results = await run_worker(provider, outbox_batch_size=10)
    outcomes = [r.outcome for r in results]
    assert outcomes.count("sent") == 1
    assert outcomes.count("deferred") == 3

    async with get_sessionmaker()() as s:
        deferred = [await outbox_row(s, f.outbox_id) for f in fixtures]
    statuses = {row.status for row in deferred}
    assert OutboxStatus.FAILED_PERMANENT not in statuses
    for row in deferred:
        if row.status is OutboxStatus.DEFERRED:
            # Rescheduled into the next window, spread rather than at midnight.
            assert row.next_attempt_at > NOW
            assert row.next_attempt_at.date() > NOW.date()


# ==========================================================================
# Suppression (invariants 5, 16)
# ==========================================================================
@pytest.mark.asyncio
async def test_suppressed_recipient_is_never_sent_to(db_session, sendable) -> None:
    async with get_sessionmaker()() as s, s.begin():
        await suppress(
            s,
            workspace_id=sendable.workspace_id,
            email_or_domain=sendable.to_email,
            reason=SuppressionReason.UNSUBSCRIBE,
            source="test",
            now=NOW,
        )

    provider = MockEmailProvider()
    results = await run_worker(provider)
    assert [r.outcome for r in results] == ["blocked"]
    assert provider.delivered_count == 0

    async with get_sessionmaker()() as s:
        row = await outbox_row(s, sendable.outbox_id)
    assert row.status is OutboxStatus.CANCELLED
    assert "suppress" in (row.blocked_reason or "").lower()


@pytest.mark.asyncio
async def test_suppression_added_after_queueing_still_blocks(
    db_session, sendable
) -> None:
    """The race that matters: unsubscribe between queueing and sending.

    The outbox worker re-reads suppression at the send boundary, so a row that
    was legitimately queued is still stopped.
    """
    async with get_sessionmaker()() as s, s.begin():
        await suppress(
            s,
            workspace_id=sendable.workspace_id,
            email_or_domain=sendable.to_email,
            reason=SuppressionReason.COMPLAINT,
            source="webhook",
            now=NOW,
        )
    provider = MockEmailProvider()
    await run_worker(provider)
    assert provider.delivered_count == 0


@pytest.mark.asyncio
async def test_domain_scope_suppression_blocks_every_address(
    db_session, workspace
) -> None:
    async with get_sessionmaker()() as s:
        await build_sendable(
            s, workspace, suffix="dom1", to_email="a@blocked-domain.test"
        )
        await build_sendable(
            s, workspace, suffix="dom2", to_email="b@blocked-domain.test"
        )
    async with get_sessionmaker()() as s, s.begin():
        await suppress(
            s,
            workspace_id=workspace,
            email_or_domain="blocked-domain.test",
            scope="domain",
            reason=SuppressionReason.LEGAL_REQUEST,
            source="test",
            now=NOW,
        )

    provider = MockEmailProvider()
    await run_worker(provider, outbox_batch_size=10)
    assert provider.delivered_count == 0


@pytest.mark.asyncio
async def test_plus_tag_suppression_matches_base_address(db_session, workspace) -> None:
    """Unsubscribing as a+news@x.test must also stop a@x.test."""
    async with get_sessionmaker()() as s:
        await build_sendable(s, workspace, suffix="plus", to_email="sam@plus-test.test")
    async with get_sessionmaker()() as s, s.begin():
        await suppress(
            s,
            workspace_id=workspace,
            email_or_domain="sam+newsletter@plus-test.test",
            reason=SuppressionReason.UNSUBSCRIBE,
            source="test",
            now=NOW,
        )
    async with get_sessionmaker()() as s:
        hit = await is_suppressed(
            s, workspace_id=workspace, email="sam@plus-test.test", now=NOW
        )
    assert hit is not None


@pytest.mark.asyncio
async def test_permanent_reason_cannot_be_given_an_expiry(db_session, workspace) -> None:
    async with get_sessionmaker()() as s, s.begin():
        with pytest.raises(ValueError, match="permanent"):
            await suppress(
                s,
                workspace_id=workspace,
                email_or_domain="x@y.test",
                reason=SuppressionReason.COMPLAINT,
                source="test",
                expires_at=NOW + dt.timedelta(days=1),
            )


@pytest.mark.asyncio
async def test_repeat_suppression_is_idempotent(db_session, workspace) -> None:
    """Duplicate webhooks are normal; suppressing twice must not error."""
    async with get_sessionmaker()() as s, s.begin():
        first = await suppress(
            s,
            workspace_id=workspace,
            email_or_domain="dup@x.test",
            reason=SuppressionReason.COMPLAINT,
            source="webhook",
            now=NOW,
        )
    async with get_sessionmaker()() as s, s.begin():
        second = await suppress(
            s,
            workspace_id=workspace,
            email_or_domain="dup@x.test",
            reason=SuppressionReason.COMPLAINT,
            source="webhook",
            now=NOW,
        )
    assert first.id == second.id


@pytest.mark.asyncio
async def test_permanent_provider_rejection_suppresses(db_session, sendable) -> None:
    provider = MockEmailProvider(permanent_failure_recipients={sendable.to_email.lower()})
    results = await run_worker(provider)
    assert [r.outcome for r in results] == ["failed_permanent"]

    async with get_sessionmaker()() as s:
        hit = await is_suppressed(
            s, workspace_id=sendable.workspace_id, email=sendable.to_email, now=NOW
        )
    assert hit is not None
    assert hit.reason is SuppressionReason.HARD_BOUNCE


@pytest.mark.asyncio
async def test_configuration_failure_does_not_suppress_the_recipient(
    db_session, sendable
) -> None:
    """An auth failure is our problem, not the recipient's."""
    provider = MockEmailProvider(fail_times=1, fail_kind=SendErrorKind.AUTH)
    results = await run_worker(provider)
    assert [r.outcome for r in results] == ["failed_permanent"]

    async with get_sessionmaker()() as s:
        hit = await is_suppressed(
            s, workspace_id=sendable.workspace_id, email=sendable.to_email, now=NOW
        )
    assert hit is None, "a configuration error suppressed an innocent recipient"


# ==========================================================================
# Policy re-evaluation at the send boundary
# ==========================================================================
@pytest.mark.asyncio
async def test_pausing_the_campaign_stops_already_queued_mail(
    db_session, sendable
) -> None:
    """Invariant 9. The most important operational control there is."""
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(Campaign)
            .where(Campaign.id == sendable.campaign_id)
            .values(status=CampaignStatus.PAUSED)
        )

    provider = MockEmailProvider()
    results = await run_worker(provider)
    assert [r.outcome for r in results] == ["blocked"]
    assert provider.delivered_count == 0


@pytest.mark.asyncio
async def test_revoking_workspace_authorization_stops_queued_mail(
    db_session, sendable
) -> None:
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(Workspace)
            .where(Workspace.id == sendable.workspace_id)
            .values(sending_authorized=False)
        )
    provider = MockEmailProvider()
    assert [r.outcome for r in await run_worker(provider)] == ["blocked"]
    assert provider.delivered_count == 0


@pytest.mark.asyncio
async def test_reply_between_queue_and_send_stops_delivery(db_session, sendable) -> None:
    """Invariant 15 at the send boundary."""
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(Lead)
            .where(Lead.id == sendable.lead_id)
            .values(replied_at=NOW - dt.timedelta(minutes=5), status=LeadStatus.REPLIED)
        )
    provider = MockEmailProvider()
    assert [r.outcome for r in await run_worker(provider)] == ["blocked"]
    assert provider.delivered_count == 0


@pytest.mark.asyncio
async def test_global_kill_switch_stops_everything(db_session, sendable) -> None:
    """Invariant 8. One environment variable halts all delivery."""
    provider = MockEmailProvider()
    results = await run_worker(provider, production_sending_enabled=False)
    assert [r.outcome for r in results] == ["blocked"]
    assert provider.delivered_count == 0


@pytest.mark.asyncio
async def test_downgrading_contact_verification_stops_delivery(
    db_session, sendable
) -> None:
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(ContactChannel)
            .where(ContactChannel.id == sendable.channel_id)
            .values(verification_status=VerificationStatus.RISKY)
        )
    provider = MockEmailProvider()
    assert [r.outcome for r in await run_worker(provider)] == ["blocked"]
    assert provider.delivered_count == 0


@pytest.mark.asyncio
async def test_guessed_contact_source_stops_delivery(db_session, sendable) -> None:
    """Invariant 6, enforced at the last possible moment."""
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(ContactChannel)
            .where(ContactChannel.id == sendable.channel_id)
            .values(source=ContactSource.PATTERN_GUESS)
        )
    provider = MockEmailProvider()
    results = await run_worker(provider)
    assert [r.outcome for r in results] == ["blocked"]
    assert provider.delivered_count == 0
    async with get_sessionmaker()() as s:
        row = await outbox_row(s, sendable.outbox_id)
    assert "guess" in (row.blocked_reason or "").lower()


@pytest.mark.asyncio
async def test_blocked_row_is_not_retried(db_session, sendable) -> None:
    """A policy refusal is terminal; retrying would be a bug, not resilience."""
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(Campaign)
            .where(Campaign.id == sendable.campaign_id)
            .values(status=CampaignStatus.PAUSED)
        )
    provider = MockEmailProvider()
    await run_worker(provider)
    second = await run_worker(provider)
    assert second == []
    assert provider.delivered_count == 0
