"""The allocator divides deliverable volume, not aspirational volume.

``_reallocate_capacity`` splits one number between the campaigns competing for
it, and that number was ``workspace.daily_send_limit`` -- a figure a human typed
because they own three mailboxes rated fifty a day. On those mailboxes' first
morning warm-up will pass five each. Dividing a hundred and fifty produced
campaign limits describing volume nobody could send, and the surplus came back
as a hundred and thirty-five deferrals rather than as a smaller plan.

Nothing here is load-bearing for safety, and that is deliberate: the per-mailbox
warm-up ceiling refuses the surplus at the gate whether or not this bound exists.
What it changes is that the plan and the reality agree, which is why it fails
soft *upward* -- an unreadable pool costs deferrals, never sends.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select, update
from titan.activities.orchestration import _deliverable_budget
from titan.db.models import SenderIdentity, Workspace

from tests.delivery.conftest import build_sendable

NOW = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.UTC)


async def _clone_sender(session, workspace_id, source_id: uuid.UUID, label: str):
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
    await session.flush()
    return clone.id


async def _pool_of_three(session, workspace_id, *, suffix: str):
    """Three ready mailboxes, none of which has ever sent."""
    fixture = await build_sendable(session, workspace_id, suffix=suffix)
    for name in ("budget-b", "budget-c"):
        await _clone_sender(session, workspace_id, fixture.sender_id, name)
    await session.commit()
    return fixture


async def _set_workspace_limit(session, workspace_id, limit: int) -> Workspace:
    await session.execute(
        update(Workspace)
        .where(Workspace.id == workspace_id)
        .values(daily_send_limit=limit)
    )
    await session.commit()
    workspace = await session.get(Workspace, workspace_id)
    assert workspace is not None
    await session.refresh(workspace)
    return workspace


@pytest.mark.asyncio
async def test_a_fresh_pool_is_budgeted_at_its_warmup_volume_not_its_rating(
    db_session, workspace
) -> None:
    """The case in the brief. Three mailboxes rated fifty are not worth a
    hundred and fifty on day one, whatever the workspace limit says."""
    fixture = await _pool_of_three(db_session, workspace, suffix="bud1")
    rated = await db_session.scalar(
        select(SenderIdentity.daily_send_limit).where(
            SenderIdentity.id == fixture.sender_id
        )
    )
    ws = await _set_workspace_limit(db_session, workspace, 3 * rated)

    budget = await _deliverable_budget(db_session, ws, NOW)

    assert ws.daily_send_limit == 3 * rated
    assert budget < ws.daily_send_limit, (
        "the allocator would divide volume the mailboxes cannot send"
    )
    assert budget == 3 * max(1, round(0.1 * rated)), budget


@pytest.mark.asyncio
async def test_the_budget_never_exceeds_what_a_human_approved(
    db_session, workspace
) -> None:
    """The bound is one-directional. A warmed-up pool able to send more than the
    workspace allows does not get to -- that would be the system raising its own
    sending limit, which is the line bounded autonomy draws."""
    await _pool_of_three(db_session, workspace, suffix="bud2")
    ws = await _set_workspace_limit(db_session, workspace, 7)

    budget = await _deliverable_budget(db_session, ws, NOW)

    assert budget == 7


@pytest.mark.asyncio
async def test_a_workspace_with_no_usable_mailboxes_is_budgeted_at_nothing(
    db_session, workspace
) -> None:
    """Not a safety mechanism -- the gate already refuses every send -- but a
    plan built on a pool that cannot send is a plan made of deferrals."""
    ws = await _set_workspace_limit(db_session, workspace, 150)

    budget = await _deliverable_budget(db_session, ws, NOW)

    assert budget == 0


@pytest.mark.asyncio
async def test_an_unreadable_pool_leaves_the_configured_limit_standing(
    db_session, workspace
) -> None:
    """Fails soft upward, to the behaviour before this existed. The warm-up
    ceiling is enforced independently at send time, so a budget that is too
    generous costs deferrals and never sends."""
    from titan.delivery import sender_pool

    ws = await _set_workspace_limit(db_session, workspace, 150)
    original = sender_pool.load_slots

    async def broken(*args, **kwargs):
        raise RuntimeError("pool query failed")

    sender_pool.load_slots = broken
    try:
        budget = await _deliverable_budget(db_session, ws, NOW)
    finally:
        sender_pool.load_slots = original

    assert budget == 150


@pytest.mark.asyncio
async def test_the_allocator_is_handed_the_bounded_figure(
    db_session, workspace, monkeypatch
) -> None:
    """The wiring, not the arithmetic. A correct bound computed and then not
    used is the same system as no bound at all."""
    from titan.activities import orchestration

    fixture = await _pool_of_three(db_session, workspace, suffix="bud4")
    rated = await db_session.scalar(
        select(SenderIdentity.daily_send_limit).where(
            SenderIdentity.id == fixture.sender_id
        )
    )
    await _set_workspace_limit(db_session, workspace, 3 * rated)

    seen: list[int] = []
    original = orchestration.allocate

    def capturing(demands, workspace_limit):
        seen.append(workspace_limit)
        return original(demands, workspace_limit)

    monkeypatch.setattr(orchestration, "allocate", capturing)
    await orchestration._reallocate_capacity(workspace, NOW)

    assert seen, "the allocator never ran; the test proved nothing"
    assert seen[0] == 3 * max(1, round(0.1 * rated)), (
        f"the allocator divided {seen[0]}, not the warm-up volume"
    )
    assert seen[0] < 3 * rated


@pytest.mark.asyncio
async def test_the_budget_is_stable_across_the_working_day(db_session, workspace) -> None:
    """Warm-up depends on the day number, not the clock. A budget that fell
    through the day would cut each campaign's daily limit on every cycle, and
    eventually below what it had already sent that morning."""
    await _pool_of_three(db_session, workspace, suffix="bud3")
    ws = await _set_workspace_limit(db_session, workspace, 150)

    morning = await _deliverable_budget(db_session, ws, NOW.replace(hour=8))
    evening = await _deliverable_budget(db_session, ws, NOW.replace(hour=17))

    assert morning == evening


# ==========================================================================
# A mailbox the health gate has stopped is not capacity
# ==========================================================================
#
# ``sender_pool`` deliberately leaves health out of a slot's limit, and its
# reasoning is right for what the pool is for: selection only needs the
# ordering, and it notes that a degraded mailbox is "usually degraded across the
# whole pool anyway".
#
# That is the assumption that failed. On the live workspace outreach@ was
# blocked on a bounce rate while sales@ was healthy, so the pool reported
# 31 + 25 and the allocator divided **56 sends that did not exist** between 23
# campaigns against a real capacity of 25. Over-committed by more than double,
# every campaign is back to competing by claim order -- the exact thing the
# allocator was written to end.


@pytest.mark.asyncio
async def test_a_blocked_mailbox_contributes_nothing_to_the_budget(
    db_session, workspace
) -> None:
    """Planted violation: drop the blocked-sender exclusion and this fails."""
    import datetime as _dt

    from titan.db.models import SenderHealthSnapshot

    fixture = await _pool_of_three(db_session, workspace, suffix="bud-blocked")
    ws = await _set_workspace_limit(db_session, workspace, 1000)

    healthy_budget = await _deliverable_budget(db_session, ws, NOW)

    senders = (
        (
            await db_session.execute(
                select(SenderIdentity).where(SenderIdentity.workspace_id == workspace)
            )
        )
        .scalars()
        .all()
    )
    blocked = next(s for s in senders if s.id == fixture.sender_id)
    db_session.add(
        SenderHealthSnapshot(
            workspace_id=workspace,
            sender_identity_id=blocked.id,
            sending_domain=blocked.sending_domain,
            captured_on=NOW.date(),
            status="blocked",
            domain_verified=True,
            spf_ok=True,
            dkim_ok=True,
            dmarc_ok=True,
            auth_stale=False,
            window_sent=94,
            window_delivered=89,
            window_bounced=5,
            window_complained=0,
            attempts=94,
            retries=0,
            deferred=0,
            sent_today=0,
            warmup_day=15,
            warmup_limit=31,
            reasons=["hard-bounce rate 5.32% over 94 sends"],
        )
    )
    await db_session.commit()

    budget = await _deliverable_budget(
        db_session, ws, _dt.datetime.combine(NOW.date(), _dt.time(12, 0), tzinfo=_dt.UTC)
    )

    assert budget < healthy_budget, (
        "a mailbox that cannot send today was counted as capacity, and the "
        "allocator would divide sends that do not exist"
    )


@pytest.mark.asyncio
async def test_an_unassessed_mailbox_is_not_treated_as_blocked(
    db_session, workspace
) -> None:
    """Null is not zero. A mailbox nobody has assessed yet is UNKNOWN, and
    treating that as blocked would zero a workspace's budget on its first
    morning -- when there is no snapshot for anything."""
    await _pool_of_three(db_session, workspace, suffix="bud-unknown")
    ws = await _set_workspace_limit(db_session, workspace, 1000)

    assert await _deliverable_budget(db_session, ws, NOW) > 0
