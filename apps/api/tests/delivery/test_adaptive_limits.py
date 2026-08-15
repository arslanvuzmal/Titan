"""Adaptive sending limits: the pure rule, and the ceiling actually applied.

Two properties carry the most weight here and both are refusals. The effective
limit can never exceed the configured one -- a system that raised its own
sending limit would be stepping over the control it exists to respect. And a
throttle must never silently become a pause: a small ceiling multiplied by a
reduction factor floors toward zero, and zero means "this scope is closed".
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import delete, select, update
from titan.db.models import (
    Message,
    OutboxMessage,
    QuotaCounter,
    SenderHealthSnapshot,
    SenderIdentity,
)
from titan.db.session import get_sessionmaker
from titan.delivery.adaptive_limits import (
    HEALTH_FACTORS,
    MIN_ACTIVE_LIMIT,
    RECOVERY_LOOKBACK_DAYS,
    daily_limit,
    recovery_factor,
)
from titan.delivery.providers.mock import MockEmailProvider
from titan.delivery.sender_health import SenderHealth

from .conftest import NOW, build_sendable, sending_settings

H = SenderHealth


# ==========================================================================
# The ceiling is a ceiling
# ==========================================================================
@pytest.mark.parametrize("health", list(SenderHealth))
def test_the_effective_limit_never_exceeds_the_configured_one(
    health: SenderHealth,
) -> None:
    """The bounded-autonomy line, as an assertion. Recovering toward a number a
    human approved is adaptation; going past it is bypassing the control."""
    for configured in (0, 1, 3, 25, 50, 1000):
        decision = daily_limit(configured, recent=(health,))
        assert decision.effective <= configured, (health, configured)


def test_a_healthy_mailbox_gets_its_whole_ceiling() -> None:
    decision = daily_limit(50, recent=(H.HEALTHY,) * 8)

    assert decision.effective == 50
    assert decision.factor == 1.0
    assert decision.reduced is False
    assert decision.paused is False


def test_no_history_at_all_changes_nothing() -> None:
    """A mailbox with no snapshot yet is not evidence of anything, and the
    configured number is already a human's conservative choice."""
    decision = daily_limit(50, recent=())

    assert decision.effective == 50
    assert decision.health is H.UNKNOWN


# ==========================================================================
# Turning it down
# ==========================================================================
def test_a_degraded_mailbox_is_cut_hard() -> None:
    decision = daily_limit(50, recent=(H.DEGRADED,))

    assert decision.effective == 12  # floor(50 * 0.25)
    assert decision.reduced is True
    assert decision.paused is False
    assert "degraded" in decision.explain()


def test_a_watched_mailbox_is_cut_gently() -> None:
    decision = daily_limit(50, recent=(H.WATCH,))

    assert decision.effective == 30  # floor(50 * 0.6)
    assert HEALTH_FACTORS[H.WATCH] > HEALTH_FACTORS[H.DEGRADED]


def test_a_blocked_mailbox_is_paused() -> None:
    """Zero is not an accident: quotas.reserve_all treats a limit of zero as a
    closed scope, which defers the message rather than failing it."""
    decision = daily_limit(50, recent=(H.BLOCKED,))

    assert decision.effective == 0
    assert decision.paused is True
    assert "paused" in decision.explain()


@pytest.mark.parametrize("configured", [1, 2, 3, 4])
def test_a_throttle_never_becomes_a_pause_by_rounding(configured: int) -> None:
    """floor(3 * 0.25) is 0, and 0 closes the scope. A degraded mailbox should
    send less, not stop -- stopping is what BLOCKED means."""
    decision = daily_limit(configured, recent=(H.DEGRADED,))

    assert decision.effective >= MIN_ACTIVE_LIMIT
    assert decision.paused is False


def test_a_configured_limit_of_zero_stays_zero() -> None:
    """The floor lifts a throttle off zero; it must not lift a limit somebody
    deliberately set to zero."""
    assert daily_limit(0, recent=(H.HEALTHY,)).effective == 0


# ==========================================================================
# Warm-up is the other ceiling
# ==========================================================================
def test_warmup_caps_a_healthy_mailbox() -> None:
    decision = daily_limit(200, recent=(H.WARMING,), warmup_limit=20)

    assert decision.effective == 20
    assert "warm-up" in decision.explain()


def test_warming_is_not_double_penalised() -> None:
    """Warm-up already imposes a much lower ceiling. Multiplying it by a health
    reduction as well would slow a new mailbox for a reason nobody wrote down."""
    assert HEALTH_FACTORS[H.WARMING] == 1.0
    assert daily_limit(200, recent=(H.WARMING,), warmup_limit=20).effective == 20


def test_the_lower_of_the_two_ceilings_wins() -> None:
    assert daily_limit(10, recent=(H.HEALTHY,), warmup_limit=40).effective == 10
    assert daily_limit(100, recent=(H.HEALTHY,), warmup_limit=40).effective == 40


# ==========================================================================
# Recovery
# ==========================================================================
def test_no_dip_means_no_recovery() -> None:
    assert recovery_factor((H.HEALTHY,) * 5) == (1.0, False)


def test_the_first_healthy_day_after_a_dip_is_half() -> None:
    factor, recovering = recovery_factor((H.HEALTHY, H.DEGRADED, H.HEALTHY))

    assert recovering is True
    assert factor == 0.5


def test_recovery_climbs_with_consecutive_healthy_days() -> None:
    two = recovery_factor((H.HEALTHY, H.HEALTHY, H.DEGRADED))
    three = recovery_factor((H.HEALTHY, H.HEALTHY, H.HEALTHY, H.DEGRADED))

    assert two == (0.75, True)
    assert three == (1.0, False), "three healthy days should be full recovery"


def test_a_dip_today_is_not_recovery() -> None:
    """Today is the dip. The health factor governs and there is nothing to climb
    back from yet."""
    assert recovery_factor((H.DEGRADED, H.HEALTHY)) == (1.0, False)
    assert daily_limit(50, recent=(H.DEGRADED, H.HEALTHY)).effective == 12


def test_a_dip_older_than_the_lookback_is_forgotten() -> None:
    old = (H.HEALTHY,) * RECOVERY_LOOKBACK_DAYS + (H.BLOCKED,)
    factor, recovering = recovery_factor(old)

    assert (factor, recovering) == (1.0, False)


def test_recovery_shows_in_the_effective_limit() -> None:
    decision = daily_limit(50, recent=(H.HEALTHY, H.DEGRADED, H.HEALTHY))

    assert decision.effective == 25
    assert decision.recovering is True
    assert "recovering" in decision.explain()


def test_volume_falls_faster_than_it_returns() -> None:
    """The asymmetry, stated as a test. A sudden return to previous volume after
    a dip is itself a pattern receivers watch for."""
    fell = daily_limit(100, recent=(H.DEGRADED, H.HEALTHY, H.HEALTHY))
    climbing = daily_limit(100, recent=(H.HEALTHY, H.DEGRADED, H.HEALTHY))

    assert fell.effective == 25
    assert climbing.effective == 50
    assert climbing.effective < 100, "it snapped straight back to full volume"


# ==========================================================================
# Applied by the worker
# ==========================================================================
async def _run(provider: MockEmailProvider, **overrides):
    from titan.delivery.outbox_worker import OutboxWorker

    return await OutboxWorker(
        provider, sending_settings(**overrides), now_fn=lambda: NOW
    ).run_once()


async def _warm_up(session, workspace_id, sender_id) -> None:
    """Age the mailbox past its warm-up ramp.

    Warm-up is measured from the first send, and a mailbox that has never sent
    sits on day one at 20 a day. That ceiling is lower than any health-driven
    reduction on a realistic configured limit, so without this the two are
    indistinguishable and a test of the health rule would really be a test of
    warm-up.
    """
    old = NOW - dt.timedelta(days=30)
    fixture = await build_sendable(session, workspace_id, suffix="warmed")
    await session.execute(
        update(Message)
        .where(Message.id == fixture.message_id)
        .values(sent_at=old, delivered_at=old, sender_identity_id=sender_id)
    )
    # Its outbox row is pending; leaving it would give the worker a second
    # message and every assertion below would be about the wrong one.
    await session.execute(
        delete(OutboxMessage).where(OutboxMessage.id == fixture.outbox_id)
    )
    await session.commit()


async def _sender_quota(workspace_id, sender_id) -> QuotaCounter | None:
    async with get_sessionmaker()() as s:
        return (
            await s.execute(
                select(QuotaCounter).where(
                    QuotaCounter.workspace_id == workspace_id,
                    QuotaCounter.scope_type == "sender",
                    QuotaCounter.scope_key == str(sender_id),
                )
            )
        ).scalar_one_or_none()


@pytest.mark.asyncio
async def test_a_healthy_send_reserves_against_the_configured_limit(
    db_session, sendable
) -> None:
    """The control. If this fails the reduction test below proves nothing."""
    await _warm_up(db_session, sendable.workspace_id, sendable.sender_id)

    provider = MockEmailProvider()
    assert [r.outcome for r in await _run(provider)] == ["sent"]

    counter = await _sender_quota(sendable.workspace_id, sendable.sender_id)
    assert counter is not None
    async with get_sessionmaker()() as s:
        sender = (
            await s.execute(
                select(SenderIdentity).where(SenderIdentity.id == sendable.sender_id)
            )
        ).scalar_one()
    assert counter.limit_value == sender.daily_send_limit


@pytest.mark.asyncio
async def test_a_degraded_history_lowers_the_reserved_ceiling(
    db_session, sendable
) -> None:
    """The whole feature, end to end: yesterday's health changes today's ceiling.

    Nothing else in the send path reads the snapshot table, so a reduced
    limit_value here can only have come from the adaptive rule.
    """
    await _warm_up(db_session, sendable.workspace_id, sendable.sender_id)
    async with get_sessionmaker()() as s, s.begin():
        s.add(
            SenderHealthSnapshot(
                workspace_id=sendable.workspace_id,
                sender_identity_id=sendable.sender_id,
                sending_domain="fixture.test",
                captured_on=NOW.date() - dt.timedelta(days=1),
                status=SenderHealth.DEGRADED.value,
                reasons=["seeded by a test"],
            )
        )

    provider = MockEmailProvider()
    await _run(provider)

    counter = await _sender_quota(sendable.workspace_id, sendable.sender_id)
    assert counter is not None
    async with get_sessionmaker()() as s:
        sender = (
            await s.execute(
                select(SenderIdentity).where(SenderIdentity.id == sendable.sender_id)
            )
        ).scalar_one()
    assert counter.limit_value < sender.daily_send_limit, (
        "yesterday's degraded snapshot did not lower today's ceiling"
    )


@pytest.mark.asyncio
async def test_a_broken_sender_pauses_rather_than_failing(db_session, sendable) -> None:
    """A paused mailbox defers its mail. Blocking would throw the message away
    over a condition that clears on its own."""
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(SenderIdentity)
            .where(SenderIdentity.id == sendable.sender_id)
            .values(dkim_ok=False)
        )

    provider = MockEmailProvider()
    results = await _run(provider)

    assert provider.delivered_count == 0
    assert [r.outcome for r in results] == ["blocked"]
