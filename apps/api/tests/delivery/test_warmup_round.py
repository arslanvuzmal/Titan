"""The scheduled warm-up round: who is in the pool, and when it refuses to run."""

from __future__ import annotations

import uuid

import pytest
from titan.activities import warmup as warmup_activity
from titan.delivery.mailboxes import Endpoint
from titan.delivery.warmup import (
    Participant,
    PoolConflict,
    involving_ours,
    plan,
    round_pool,
)
from titan.workflows.schedules import plan_schedules
from titan.workflows.types import WarmupRoundInput

from tests.delivery.conftest import sending_settings

EP = Endpoint(host="mail.example", port=587, username="u", password="p")


def mailbox(address: str, day: int = 0) -> Participant:
    return Participant(address=address, smtp=EP, imap=EP, day=day)


OURS = [
    mailbox("arslan@arslanvuzmallone.com", day=3),
    mailbox("sales@arslanvuzmallone.com"),
]
PARTNERS = [mailbox("titan.partner1@gmail.com"), mailbox("titan.partner2@outlook.com")]


# ------------------------------------------------------------------ the pool
def test_partners_join_the_pool_at_full_volume() -> None:
    pool = round_pool(OURS, PARTNERS, seed_addresses=set())
    by_address = {p.address: p for p in pool}
    assert set(by_address) == {p.address for p in OURS + PARTNERS}
    assert by_address["arslan@arslanvuzmallone.com"].day == 3
    assert by_address["titan.partner1@gmail.com"].day > 0


def test_a_seed_may_not_be_a_partner() -> None:
    """A seed warmed by our mail files our next probe in the inbox because of
    its own history, and the gate would reopen on a reading it manufactured."""
    with pytest.raises(PoolConflict, match="seeds"):
        round_pool(OURS, PARTNERS, seed_addresses={"TITAN.partner1@gmail.com"})


def test_a_sending_mailbox_may_not_be_a_partner() -> None:
    with pytest.raises(PoolConflict, match="sending"):
        round_pool(OURS, [mailbox("arslan@arslanvuzmallone.com")], seed_addresses=set())


def test_every_planned_message_involves_one_of_ours() -> None:
    pool = round_pool(OURS, PARTNERS, seed_addresses=set())
    ours = {p.address for p in OURS}
    today = involving_ours(plan(pool), OURS)
    assert today, "a pool with partners should plan something"
    assert all(s.sender.address in ours or s.recipient.address in ours for s in today)


def test_partner_to_partner_mail_is_dropped() -> None:
    pool = round_pool(OURS, PARTNERS, seed_addresses=set())
    everything = plan(pool)
    kept = involving_ours(everything, OURS)
    partner_only = [
        s
        for s in everything
        if s.sender.address not in {p.address for p in OURS}
        and s.recipient.address not in {p.address for p in OURS}
    ]
    assert len(kept) == len(everything) - len(partner_only)


# ------------------------------------------------------------- the activity
async def _run(monkeypatch, **overrides):
    monkeypatch.setattr(
        warmup_activity, "get_settings", lambda: sending_settings(**overrides)
    )
    return await warmup_activity.run_warmup_round(
        WarmupRoundInput(workspace_id=str(uuid.uuid4()))
    )


@pytest.mark.asyncio
async def test_switched_off_is_a_skip(monkeypatch) -> None:
    result = await _run(monkeypatch, warmup_enabled=False)
    assert result.skipped == "warm-up is switched off"
    assert result.sent == 0


@pytest.mark.asyncio
async def test_no_partners_is_a_skip_that_says_why(monkeypatch) -> None:
    result = await _run(
        monkeypatch,
        warmup_enabled=True,
        mailbox_file="/run/secrets/mailboxes.json",
        warmup_partner_file=None,
    )
    assert result.skipped is not None
    assert "never reaches Gmail or Microsoft" in result.skipped


# -------------------------------------------------------------- the schedule
def test_the_round_is_scheduled_daily() -> None:
    jobs = {j.workflow: j for j in plan_schedules(uuid.uuid4(), task_queue="q")}
    assert "WarmupRoundWorkflow" in jobs
    assert jobs["WarmupRoundWorkflow"].cron == "10 9 * * *"
