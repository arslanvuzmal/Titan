"""A message is not married to the mailbox that was healthy when it was queued.

Both defects proved here were found the same way: the estate had fifteen unused
sending slots on 1 September and a queue of eighty-seven messages that could not
reach any of them. Neither showed up as a failure. Every message involved was
sitting in ``deferred`` with an honest reason attached, which is exactly why it
went unnoticed -- the system was not broken in any way it could report.

* Seventy-six of the eighty-seven were pinned to two mailboxes that between them
  could send nothing more that day: one on a bounce block, one capped at five.
  The pool had picked them days earlier, when they were the roomiest mailboxes
  in the estate, and nothing ever revisited that choice.
* Twelve more were parked until the next UTC midnight for being inside their
  recipient's quiet hours -- and UTC midnight is 01:00 in Dublin, still inside
  quiet hours, so they would have been parked again, every night, forever. The
  oldest had been going round that loop for nine days.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text, update
from titan.db.enums import OutboxStatus
from titan.db.models import (
    CampaignPolicy,
    CampaignSender,
    Message,
    OrganizationLocation,
    OutboxMessage,
    SenderIdentity,
)
from titan.db.session import get_sessionmaker
from titan.delivery import quotas
from titan.delivery.providers.mock import MockEmailProvider

from .conftest import NOW, build_sendable, sending_settings
from .test_outbox_delivery import outbox_row, worker

pytestmark = pytest.mark.integration


async def _second_mailbox(
    session, workspace_id: uuid.UUID, campaign_id: uuid.UUID, source_id: uuid.UUID
) -> SenderIdentity:
    """Another fully-authorized mailbox, in the same campaign's pool."""
    source = await session.get(SenderIdentity, source_id)
    spare = SenderIdentity(
        workspace_id=workspace_id,
        label="spare",
        from_email=f"spare-{uuid.uuid4().hex[:8]}@{source.sending_domain}",
        from_name=source.from_name,
        reply_to_email=source.reply_to_email,
        sending_domain=source.sending_domain,
        domain_verified=True,
        spf_ok=True,
        dkim_ok=True,
        dmarc_ok=True,
        # Real clock, exactly as the fixture's own mailbox does it: staleness
        # is judged against wall time, so a fixture-dated verification is a
        # month old by the time this runs and the mailbox is excluded.
        last_verified_at=dt.datetime.now(dt.UTC),
        daily_send_limit=source.daily_send_limit,
        mailing_address=source.mailing_address,
        unsubscribe_mailto=source.unsubscribe_mailto,
        supports_one_click_unsubscribe=True,
    )
    session.add(spare)
    await session.flush()
    session.add_all(
        [
            CampaignSender(
                workspace_id=workspace_id,
                campaign_id=campaign_id,
                sender_identity_id=sid,
            )
            for sid in (source_id, spare.id)
        ]
    )
    await session.commit()
    return spare


async def _saturate(
    session, workspace_id: uuid.UUID, scope, key: str, *, day: dt.date = NOW.date()
) -> None:
    """Spend one quota scope's whole allowance for today.

    Not by zeroing ``daily_send_limit``: a mailbox with no limit configured is
    refused by the authorization gate as misconfigured, and a block is the one
    outcome this file is not about.
    """
    await session.execute(
        text(
            """
            INSERT INTO quota_counters
                (id, workspace_id, scope_type, scope_key, window_date,
                 used, limit_value, created_at, updated_at)
            VALUES (gen_random_uuid(), :ws, :scope, :key, :day, 1, 1, now(), now())
            ON CONFLICT (workspace_id, scope_type, scope_key, window_date)
            DO UPDATE SET used = 1, limit_value = 1
            """
        ),
        {"ws": workspace_id, "scope": scope.value, "key": key, "day": day},
    )
    await session.commit()


async def _spend_sender_quota(
    session, workspace_id, sender_id: uuid.UUID, *, day: dt.date = NOW.date()
) -> None:
    await _saturate(
        session, workspace_id, quotas.QuotaScope.SENDER, str(sender_id), day=day
    )


# ==========================================================================
# Moving between mailboxes
# ==========================================================================
@pytest.mark.asyncio
async def test_a_message_whose_mailbox_is_spent_moves_to_one_that_is_not(
    db_session, workspace
) -> None:
    """The whole point. A mailbox with nothing left today used to take its
    backlog down with it until midnight; now the backlog goes round it."""
    fixture = await build_sendable(db_session, workspace, suffix="rp1")
    spare = await _second_mailbox(
        db_session, workspace, fixture.campaign_id, fixture.sender_id
    )
    await _spend_sender_quota(db_session, workspace, fixture.sender_id)

    await worker(MockEmailProvider()).run_once()

    async with get_sessionmaker()() as s:
        row = await outbox_row(s, fixture.outbox_id)
        assert row.status is OutboxStatus.DEFERRED
        assert "moved to" in (row.blocked_reason or ""), row.blocked_reason
        assert row.sender_identity_id == spare.id
        assert "moved to" in (row.blocked_reason or ""), row.blocked_reason


@pytest.mark.asyncio
async def test_the_move_rewrites_the_address_the_message_is_sent_from(
    db_session, workspace
) -> None:
    """The SMTP pool routes on the payload's ``from_email``. A row that moved
    its foreign key and not its payload would go out over one mailbox's
    connection wearing another's address -- an SPF failure by construction."""
    fixture = await build_sendable(db_session, workspace, suffix="rp2")
    spare = await _second_mailbox(
        db_session, workspace, fixture.campaign_id, fixture.sender_id
    )
    await _spend_sender_quota(db_session, workspace, fixture.sender_id)

    await worker(MockEmailProvider()).run_once()

    async with get_sessionmaker()() as s:
        row = await outbox_row(s, fixture.outbox_id)
        assert row.payload["from_email"] == spare.from_email
        assert row.payload["reply_to"] == spare.reply_to_email


@pytest.mark.asyncio
async def test_the_move_carries_the_unsubscribe_headers_with_it(
    db_session, workspace
) -> None:
    """List-Unsubscribe is a send gate, not a courtesy. A moved message that
    lost it would be refused; one that kept a stale mailto would point an
    opt-out at the wrong mailbox."""
    fixture = await build_sendable(db_session, workspace, suffix="rp3")
    await _second_mailbox(db_session, workspace, fixture.campaign_id, fixture.sender_id)
    await _spend_sender_quota(db_session, workspace, fixture.sender_id)

    await worker(MockEmailProvider()).run_once()

    async with get_sessionmaker()() as s:
        row = await outbox_row(s, fixture.outbox_id)
        assert row.payload["list_unsubscribe"]


@pytest.mark.asyncio
async def test_the_message_row_follows_the_outbox_row(db_session, workspace) -> None:
    """The CRM reads ``messages``. Left behind, it would name the mailbox that
    refused the message as the one that sent it."""
    fixture = await build_sendable(db_session, workspace, suffix="rp4")
    spare = await _second_mailbox(
        db_session, workspace, fixture.campaign_id, fixture.sender_id
    )
    await _spend_sender_quota(db_session, workspace, fixture.sender_id)

    await worker(MockEmailProvider()).run_once()

    async with get_sessionmaker()() as s:
        message = await s.get(Message, fixture.message_id)
        assert message.sender_identity_id == spare.id
        assert message.from_email == spare.from_email


@pytest.mark.asyncio
async def test_a_moved_message_is_retried_soon_rather_than_tomorrow(
    db_session, workspace
) -> None:
    """A mailbox-specific refusal used to cost the message the rest of the day.
    Having moved it, waiting until midnight to try would waste the move."""
    fixture = await build_sendable(db_session, workspace, suffix="rp5")
    await _second_mailbox(db_session, workspace, fixture.campaign_id, fixture.sender_id)
    await _spend_sender_quota(db_session, workspace, fixture.sender_id)

    await worker(MockEmailProvider()).run_once()

    async with get_sessionmaker()() as s:
        row = await outbox_row(s, fixture.outbox_id)
        assert row.next_attempt_at <= NOW + dt.timedelta(minutes=5)


@pytest.mark.asyncio
async def test_a_pool_with_nowhere_better_to_go_defers_as_it_always_did(
    db_session, workspace
) -> None:
    """A pool of one is the common case and must not be made worse. With no
    alternative mailbox the message defers to the next window, unmoved."""
    fixture = await build_sendable(db_session, workspace, suffix="rp6")
    await _spend_sender_quota(db_session, workspace, fixture.sender_id)

    await worker(MockEmailProvider()).run_once()

    async with get_sessionmaker()() as s:
        row = await outbox_row(s, fixture.outbox_id)
        assert row.status is OutboxStatus.DEFERRED
        assert row.sender_identity_id == fixture.sender_id
        assert "moved to" not in (row.blocked_reason or "")
        assert row.next_attempt_at > NOW + dt.timedelta(hours=1)


@pytest.mark.asyncio
async def test_a_mailbox_this_worker_cannot_authenticate_as_is_not_chosen(
    db_session, workspace
) -> None:
    """Moving to a pool member with no SMTP credential in this process would
    turn a deferral into a refusal at the connection."""
    fixture = await build_sendable(db_session, workspace, suffix="rp7")
    await _second_mailbox(db_session, workspace, fixture.campaign_id, fixture.sender_id)
    await _spend_sender_quota(db_session, workspace, fixture.sender_id)

    provider = MockEmailProvider()
    # A provider that can only authenticate as an address nobody in the pool
    # uses: the pool has a candidate with room, and it is still not chosen.
    provider.routable_addresses = ("someone-else@elsewhere.test",)

    await worker(provider).run_once()

    async with get_sessionmaker()() as s:
        row = await outbox_row(s, fixture.outbox_id)
        assert row.sender_identity_id == fixture.sender_id
        assert "moved to" not in (row.blocked_reason or "")


@pytest.mark.asyncio
async def test_a_workspace_budget_does_not_move_the_message(
    db_session, workspace
) -> None:
    """A workspace limit is spent from every mailbox equally. Re-pinning against
    it would be the same refusal from a different address."""
    fixture = await build_sendable(db_session, workspace, suffix="rp8")
    await _second_mailbox(db_session, workspace, fixture.campaign_id, fixture.sender_id)
    await _saturate(db_session, workspace, quotas.QuotaScope.WORKSPACE, str(workspace))

    await worker(MockEmailProvider()).run_once()

    async with get_sessionmaker()() as s:
        row = await outbox_row(s, fixture.outbox_id)
        assert row.status is OutboxStatus.DEFERRED
        assert row.sender_identity_id == fixture.sender_id


# ==========================================================================
# Quiet hours, and the loop they used to create
# ==========================================================================
async def _dublin_recipient(session, fixture) -> None:
    """A recipient on a clock an hour ahead of UTC, with quiet hours in force."""
    await session.execute(
        update(OrganizationLocation)
        .where(OrganizationLocation.workspace_id == fixture.workspace_id)
        .values(country_code="IE", timezone="Europe/Dublin")
    )
    await session.execute(
        update(CampaignPolicy)
        .where(CampaignPolicy.campaign_id == fixture.campaign_id)
        .values(respect_quiet_hours=True)
    )
    await session.commit()


#: 06:00 in Dublin -- two hours before quiet hours end, which is where every
#: message in the production loop was first evaluated.
DAWN = dt.datetime(2026, 8, 4, 5, 0, tzinfo=dt.UTC)


@pytest.mark.asyncio
async def test_quiet_hours_retry_at_the_end_of_quiet_hours(
    db_session, workspace
) -> None:
    """Not at the next UTC midnight, which for this recipient is 01:00 -- still
    quiet, deferred again, and round forever."""
    fixture = await build_sendable(db_session, workspace, suffix="qh1")
    await _dublin_recipient(db_session, fixture)

    w = worker(MockEmailProvider(), now_fn=lambda: DAWN, quiet_hours_enabled=True)
    await w.run_once()

    async with get_sessionmaker()() as s:
        row = await outbox_row(s, fixture.outbox_id)
        assert row.status is OutboxStatus.DEFERRED
        # 08:00 in Dublin, which in August is 07:00 UTC.
        assert row.next_attempt_at == dt.datetime(2026, 8, 4, 7, 0, tzinfo=dt.UTC)


@pytest.mark.asyncio
async def test_the_retry_is_not_inside_quiet_hours_again(
    db_session, workspace
) -> None:
    """The property that actually mattered, stated without reference to any
    particular hour: whatever time is chosen, the recipient must be awake at it.
    A retry that lands back inside quiet hours is not a delay, it is a loop."""
    import zoneinfo

    fixture = await build_sendable(db_session, workspace, suffix="qh2")
    await _dublin_recipient(db_session, fixture)

    w = worker(MockEmailProvider(), now_fn=lambda: DAWN, quiet_hours_enabled=True)
    await w.run_once()

    async with get_sessionmaker()() as s:
        row = await outbox_row(s, fixture.outbox_id)

    settings = sending_settings(quiet_hours_enabled=True)
    local = row.next_attempt_at.astimezone(zoneinfo.ZoneInfo("Europe/Dublin"))
    assert not (
        local.hour >= settings.quiet_hours_start or local.hour < settings.quiet_hours_end
    ), f"retry lands at {local:%H:%M} local, back inside quiet hours"


@pytest.mark.asyncio
async def test_a_recipient_already_awake_is_not_given_a_quiet_hours_retry(
    db_session, workspace
) -> None:
    """The new retry time is only for messages the quiet-hours gate stopped.
    A midday deferral is about something else and keeps the old fallback."""
    fixture = await build_sendable(db_session, workspace, suffix="qh3")
    await _dublin_recipient(db_session, fixture)
    noon = dt.datetime(2026, 8, 4, 11, 0, tzinfo=dt.UTC)
    await _spend_sender_quota(
        db_session, workspace, fixture.sender_id, day=noon.date()
    )

    w = worker(MockEmailProvider(), now_fn=lambda: noon, quiet_hours_enabled=True)
    await w.run_once()

    async with get_sessionmaker()() as s:
        row = await outbox_row(s, fixture.outbox_id)
        assert row.status is OutboxStatus.DEFERRED
        assert row.next_attempt_at > noon + dt.timedelta(hours=6)
