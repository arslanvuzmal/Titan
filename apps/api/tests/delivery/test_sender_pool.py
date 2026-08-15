"""Sending from several mailboxes, and warming each one towards its own target.

Two failures are guarded here. The first is a pool that concentrates: a batch
queued in one go reads zero sends everywhere and piles onto one mailbox. The
second is a warm-up that finishes before it has ramped -- the state this
replaced, where a 50-a-day mailbox was at full volume on day four.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid

import pytest
from sqlalchemy import select, text, update
from titan.db.enums import OutboxStatus
from titan.db.models import Campaign, CampaignSender, OutboxMessage, SenderIdentity
from titan.delivery import deliverability as d
from titan.delivery import sender_pool
from titan.delivery.sender_pool import MailboxSlot, capacity, choose, describe

from tests.delivery.conftest import build_sendable

NOW = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.UTC)


def slot(name: str, limit: int, committed: int, *, excluded: str | None = None):
    return MailboxSlot(
        sender_identity_id=uuid.UUID(int=abs(hash(name)) % (2**128)),
        label=name,
        from_email=f"{name}@example.com",
        daily_limit=limit,
        committed=committed,
        excluded_because=excluded,
    )


# ==========================================================================
# Choosing a mailbox
# ==========================================================================
def test_the_mailbox_with_the_most_room_is_chosen() -> None:
    chosen = choose([slot("a", 50, 40), slot("b", 50, 5), slot("c", 50, 30)]).slot

    assert chosen is not None and chosen.label == "b"


def test_identical_mailboxes_rotate() -> None:
    """Round-robin is what headroom selection *degenerates* to when the pool is
    uniform -- it is not implemented, it falls out. Assign three messages across
    three equal mailboxes and each takes one."""
    slots = [slot("a", 50, 0), slot("b", 50, 0), slot("c", 50, 0)]
    used: list[str] = []

    for _ in range(3):
        picked = choose(slots).slot
        assert picked is not None
        used.append(picked.label)
        slots = [
            slot(
                s.label,
                s.daily_limit,
                s.committed + (1 if s.label == picked.label else 0),
            )
            for s in slots
        ]

    assert sorted(used) == ["a", "b", "c"], used


def test_a_warming_mailbox_gets_proportionally_less() -> None:
    """The case round-robin gets wrong. One mailbox on an early warm-up day
    beside two healthy ones: round-robin would hand it a third of the volume and
    watch it defer most of that."""
    slots = [slot("warming", 6, 0), slot("healthy1", 50, 0), slot("healthy2", 50, 0)]
    counts = {"warming": 0, "healthy1": 0, "healthy2": 0}

    for _ in range(30):
        picked = choose(slots).slot
        assert picked is not None
        counts[picked.label] += 1
        slots = [
            slot(
                s.label,
                s.daily_limit,
                s.committed + (1 if s.label == picked.label else 0),
            )
            for s in slots
        ]

    assert counts["warming"] == 0, (
        "a warming mailbox was given work before the healthy ones filled"
    )
    assert counts["healthy1"] + counts["healthy2"] == 30


def test_an_excluded_mailbox_is_never_chosen() -> None:
    """The whole point of a pool: one mailbox losing its DKIM record costs the
    campaign that mailbox's share, not its ability to send."""
    broken = slot("broken", 50, 0, excluded="DKIM not in place")
    chosen = choose([broken, slot("ok", 50, 45)]).slot

    assert chosen is not None and chosen.label == "ok"
    assert broken.headroom == 0


def test_a_full_pool_chooses_nothing() -> None:
    selection = choose([slot("a", 50, 50), slot("b", 50, 50)])

    assert selection.slot is None
    assert selection.considered, "the refusal must still say what it looked at"


def test_an_empty_pool_chooses_nothing() -> None:
    selection = choose([])

    assert selection.slot is None
    assert "no sending mailboxes" in describe(selection)


def test_a_mailbox_over_its_limit_is_full_not_owed() -> None:
    """Negative headroom would sort *below* a full mailbox and, worse, subtract
    from the pool's total capacity."""
    assert slot("a", 50, 60).headroom == 0
    assert capacity([slot("a", 50, 60), slot("b", 50, 10)]) == 40


def test_selection_is_deterministic_under_a_tie() -> None:
    """Two workers queueing the same message must pick the same mailbox, or the
    dedupe key sees two rows that differ only in mailbox."""
    slots = [slot("a", 50, 10), slot("b", 50, 10), slot("c", 50, 10)]
    assert len({choose(list(reversed(slots))).chosen_id for _ in range(5)}) == 1
    assert choose(slots).chosen_id == choose(list(reversed(slots))).chosen_id


def test_the_refusal_names_each_mailbox_and_its_reason() -> None:
    """ "no sending capacity" is useless when one mailbox waits for tomorrow and
    another waits for a DNS record."""
    text_out = describe(
        choose([slot("a", 50, 50), slot("b", 0, 0, excluded="SPF not in place")])
    )

    assert "a: 50 of 50 used" in text_out
    assert "b: SPF not in place" in text_out


def test_capacity_is_the_pools_remaining_volume() -> None:
    """Three mailboxes at fifty is a hundred and fifty a day -- the number the
    single-sender campaign could not reach."""
    assert capacity([slot("a", 50, 0), slot("b", 50, 0), slot("c", 50, 0)]) == 150


# ==========================================================================
# Warm-up shape
# ==========================================================================
def test_warmup_ramps_towards_the_mailboxs_own_target() -> None:
    day_one = d.warmup_limit(first_send_at=None, now=NOW, target=50)
    assert day_one == 5

    big = d.warmup_limit(first_send_at=None, now=NOW, target=500)
    assert big == 50, "the ramp is a fraction of target, not an absolute figure"


def test_a_fifty_a_day_mailbox_is_not_at_full_volume_in_a_week() -> None:
    """The defect this replaced. The old absolute schedule ran 20, 30, 40, 60...
    so min(50, schedule) reached 50 on day four and warm-up stopped
    constraining anything from then on."""
    first = NOW - dt.timedelta(days=6)
    assert d.warmup_limit(first_send_at=first, now=NOW, target=50) == 11


def test_warmup_reaches_target_exactly_at_the_end() -> None:
    first = NOW - dt.timedelta(days=d.WARMUP_DAYS - 1)
    assert d.warmup_limit(first_send_at=first, now=NOW, target=50) == 50


def test_warmup_ends_and_stops_constraining() -> None:
    first = NOW - dt.timedelta(days=d.WARMUP_DAYS)
    assert d.warmup_limit(first_send_at=first, now=NOW, target=50) is None


def test_the_ramp_never_exceeds_the_target() -> None:
    """A ramp that overshoots would be this module quietly raising a limit a
    human set."""
    for target in (1, 6, 50, 137, 500):
        for day in range(d.WARMUP_DAYS):
            first = NOW - dt.timedelta(days=day)
            limit = d.warmup_limit(first_send_at=first, now=NOW, target=target)
            assert limit is not None and limit <= target, (target, day, limit)


def test_the_ramp_only_ever_climbs() -> None:
    limits = [
        d.warmup_limit(first_send_at=NOW - dt.timedelta(days=day), now=NOW, target=50)
        for day in range(d.WARMUP_DAYS)
    ]
    assert limits == sorted(limits), limits
    assert d.WARMUP_RAMP[-1] == 1.0


def test_a_small_target_still_sends_something() -> None:
    """A tenth of six rounds to nothing, and a mailbox that sends nothing never
    establishes any history to warm up with."""
    assert d.warmup_limit(first_send_at=None, now=NOW, target=6) >= 1


def test_a_mailbox_configured_for_nothing_stays_at_nothing() -> None:
    """The floor is for small targets, not for disabled mailboxes."""
    assert d.warmup_limit(first_send_at=None, now=NOW, target=0) == 0
    signals = d.check_warmup(first_send_at=None, sent_today=0, now=NOW, target=0)
    assert [s.code for s in signals] == ["no_sending_capacity"]


def test_a_backdated_first_send_does_not_finish_the_ramp() -> None:
    """Clock skew or an imported history should not hand a cold mailbox a
    completed warm-up."""
    future = NOW + dt.timedelta(days=3)
    assert d.warmup_limit(first_send_at=future, now=NOW, target=50) == 5


def test_the_warmup_signal_names_the_target() -> None:
    first = NOW - dt.timedelta(days=1)
    signals = d.check_warmup(first_send_at=first, sent_today=99, now=NOW, target=50)

    assert len(signals) == 1
    assert "of the mailbox's 50 messages" in signals[0].detail


# ==========================================================================
# The query
# ==========================================================================
async def _pool(session, workspace_id, campaign_id):
    return await sender_pool.load_slots(session, workspace_id, campaign_id, now=NOW)


@pytest.mark.asyncio
async def test_a_campaign_with_no_pool_rows_falls_back_to_its_own_sender(
    db_session, workspace
) -> None:
    """Every campaign configured before this table existed keeps working, as a
    pool of one."""
    fixture = await build_sendable(db_session, workspace, suffix="pf1")
    await db_session.execute(
        text("DELETE FROM campaign_senders WHERE campaign_id = :c"),
        {"c": fixture.campaign_id},
    )
    await db_session.commit()

    slots = await _pool(db_session, workspace, fixture.campaign_id)

    assert [s.sender_identity_id for s in slots] == [fixture.sender_id]


@pytest.mark.asyncio
async def test_the_pool_replaces_the_single_sender_rather_than_adding_to_it(
    db_session, workspace
) -> None:
    """Once a campaign has pool rows they are the whole answer. Unioning the two
    would make it impossible to *remove* a mailbox from a campaign that still
    carries it as its legacy sender."""
    fixture = await build_sendable(db_session, workspace, suffix="pf2")
    extra = await _clone_sender(db_session, workspace, fixture.sender_id, "pool-b")
    db_session.add(
        CampaignSender(
            workspace_id=workspace,
            campaign_id=fixture.campaign_id,
            sender_identity_id=extra,
        )
    )
    await db_session.commit()

    slots = await _pool(db_session, workspace, fixture.campaign_id)

    assert [s.sender_identity_id for s in slots] == [extra]
    assert (
        await db_session.scalar(
            select(Campaign.sender_identity_id).where(Campaign.id == fixture.campaign_id)
        )
        == fixture.sender_id
    ), "the legacy column is still set; the pool simply outranks it"


@pytest.mark.asyncio
async def test_three_mailboxes_give_a_campaign_three_mailboxes_of_capacity(
    db_session, workspace
) -> None:
    """The row this exists for. One mailbox at 50 a day was the ceiling; three
    is 150, with each mailbox still behaving like a mailbox."""
    fixture = await build_sendable(db_session, workspace, suffix="pf7")
    ids = [fixture.sender_id]
    for n in ("pool-x", "pool-y"):
        ids.append(await _clone_sender(db_session, workspace, fixture.sender_id, n))
    for sender_id in ids:
        db_session.add(
            CampaignSender(
                workspace_id=workspace,
                campaign_id=fixture.campaign_id,
                sender_identity_id=sender_id,
            )
        )
    await db_session.commit()
    # Warm-up finished on all three, so this measures the pool and not the ramp.
    for sender_id in ids:
        await _backdate_first_send(db_session, workspace, fixture, sender_id)
    await db_session.commit()

    per_mailbox = await db_session.scalar(
        select(SenderIdentity.daily_send_limit).where(
            SenderIdentity.id == fixture.sender_id
        )
    )
    slots = await _pool(db_session, workspace, fixture.campaign_id)

    assert len(slots) == 3
    assert all(s.daily_limit == per_mailbox for s in slots), [
        s.daily_limit for s in slots
    ]
    assert capacity(slots) == 3 * per_mailbox - sum(s.committed for s in slots)
    assert capacity(slots) > per_mailbox, "the pool must beat any single mailbox"


@pytest.mark.asyncio
async def test_unsent_outbox_rows_count_against_their_mailbox(
    db_session, workspace
) -> None:
    """The regression guard for a batch queued in one go. Without this, two
    hundred messages read sent_today = 0 everywhere and land on one mailbox."""
    fixture = await build_sendable(db_session, workspace, suffix="pf3")

    before = await _pool(db_session, workspace, fixture.campaign_id)
    assert before and before[0].committed == 1, "the fixture's own pending row"

    await db_session.execute(
        update(OutboxMessage)
        .where(OutboxMessage.id == fixture.outbox_id)
        .values(status=OutboxStatus.CANCELLED)
    )
    await db_session.commit()

    after = await _pool(db_session, workspace, fixture.campaign_id)
    assert after[0].committed == 0, "a cancelled row is not work in flight"


@pytest.mark.asyncio
async def test_a_mailbox_missing_dkim_is_excluded_with_the_reason(
    db_session, workspace
) -> None:
    fixture = await build_sendable(db_session, workspace, suffix="pf4")
    await db_session.execute(
        update(SenderIdentity)
        .where(SenderIdentity.id == fixture.sender_id)
        .values(dkim_ok=False)
    )
    await db_session.commit()

    slots = await _pool(db_session, workspace, fixture.campaign_id)

    assert slots[0].available is False
    assert "DKIM" in (slots[0].excluded_because or "")
    assert slots[0].headroom == 0


@pytest.mark.asyncio
async def test_another_workspaces_mailbox_is_not_in_the_pool(
    db_session, workspace
) -> None:
    from titan.db.models import Workspace

    fixture = await build_sendable(db_session, workspace, suffix="pf5")
    other = Workspace(name="Other", slug=f"o-{uuid.uuid4().hex[:12]}")
    db_session.add(other)
    await db_session.commit()
    other_id = other.id
    try:
        stolen = await _clone_sender(db_session, other_id, fixture.sender_id, "stolen")
        db_session.add(
            CampaignSender(
                workspace_id=other_id,
                campaign_id=fixture.campaign_id,
                sender_identity_id=stolen,
            )
        )
        await db_session.commit()

        slots = await _pool(db_session, workspace, fixture.campaign_id)

        assert all(s.sender_identity_id != stolen for s in slots)
    finally:
        from sqlalchemy import delete

        await db_session.rollback()
        await db_session.execute(delete(Workspace).where(Workspace.id == other_id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_warmup_caps_a_new_mailbox_in_the_pool(db_session, workspace) -> None:
    """A mailbox that has never sent gets a tenth of its target, not its target
    -- the ordering that makes the pool route around it."""
    fixture = await build_sendable(db_session, workspace, suffix="pf6")
    configured = await db_session.scalar(
        select(SenderIdentity.daily_send_limit).where(
            SenderIdentity.id == fixture.sender_id
        )
    )

    slots = await _pool(db_session, workspace, fixture.campaign_id)

    assert slots[0].daily_limit == math.ceil(d.WARMUP_RAMP[0] * configured)
    assert slots[0].daily_limit < configured, "a never-sent mailbox is on day one"


async def _clone_sender(session, workspace_id, source_id: uuid.UUID, label: str):
    """A second mailbox on the same domain, ready to send."""
    source = (
        await session.execute(
            select(SenderIdentity).where(SenderIdentity.id == source_id)
        )
    ).scalar_one()
    clone = SenderIdentity(
        workspace_id=workspace_id,
        label=label,
        from_email=f"{label}-{uuid.uuid4().hex[:8]}@{source.sending_domain}",
        from_name=source.from_name,
        reply_to_email=source.reply_to_email,
        sending_domain=source.sending_domain,
        domain_verified=True,
        spf_ok=True,
        dkim_ok=True,
        dmarc_ok=True,
        last_verified_at=NOW,
        daily_send_limit=source.daily_send_limit,
        mailing_address=source.mailing_address,
    )
    session.add(clone)
    await session.commit()
    return clone.id


async def _backdate_first_send(session, workspace_id, fixture, sender_id) -> None:
    """Give a mailbox a send old enough that its warm-up has finished.

    Copies the fixture's own message row rather than building one, so every NOT
    NULL column stays populated as the schema requires.
    """
    await session.execute(
        text(
            """
            INSERT INTO messages (
                id, workspace_id, draft_id, lead_id, campaign_id,
                sender_identity_id, dedupe_key, to_email, to_email_normalized,
                to_domain, from_email, subject, state, state_rank, provider,
                sent_at, version, created_at, updated_at
            )
            SELECT
                gen_random_uuid(), workspace_id, draft_id, lead_id, campaign_id,
                :sender, 'warm-' || gen_random_uuid()::text, to_email,
                to_email_normalized, to_domain, from_email, subject, state,
                state_rank, provider, :then, 1, now(), now()
              FROM messages
             WHERE id = :source
            """
        ),
        {
            "sender": sender_id,
            "then": NOW - dt.timedelta(days=60),
            "source": fixture.message_id,
        },
    )


@pytest.mark.asyncio
async def test_the_report_query_returns_the_whole_estate(db_session, workspace) -> None:
    """The regression guard for the weekly report's capacity section, which
    fails soft -- so an empty list is indistinguishable from a broken query
    unless something asserts rows come back.

    ``campaign_id=None`` is a switch, not an addition: it must return every
    mailbox in the workspace, including ones no campaign points at.
    """
    fixture = await build_sendable(db_session, workspace, suffix="pf8")
    unattached = await _clone_sender(db_session, workspace, fixture.sender_id, "spare")

    slots = await sender_pool.load_slots(db_session, workspace, None, now=NOW)

    ids = {s.sender_identity_id for s in slots}
    assert fixture.sender_id in ids
    assert unattached in ids, "a mailbox no campaign uses still has capacity"
    assert sender_pool.describe_slot(slots[0])


@pytest.mark.asyncio
async def test_the_report_query_does_not_cross_workspaces(db_session, workspace) -> None:
    from titan.db.models import Workspace

    fixture = await build_sendable(db_session, workspace, suffix="pf9")
    other = Workspace(name="Other", slug=f"o-{uuid.uuid4().hex[:12]}")
    db_session.add(other)
    await db_session.commit()
    other_id = other.id
    try:
        theirs = await _clone_sender(db_session, other_id, fixture.sender_id, "theirs")

        slots = await sender_pool.load_slots(db_session, workspace, None, now=NOW)

        assert all(s.sender_identity_id != theirs for s in slots)
    finally:
        from sqlalchemy import delete

        await db_session.rollback()
        await db_session.execute(delete(Workspace).where(Workspace.id == other_id))
        await db_session.commit()
