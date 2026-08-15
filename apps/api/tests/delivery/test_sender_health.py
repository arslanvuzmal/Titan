"""Sender health: classification, trend, and the snapshot that makes both possible.

The classifier is pure and tested here without a database. The persistence half
is at the bottom, against a real one, because the whole point of this feature is
that the numbers survive the send that produced them -- and a test that mocked
the write would be testing the mock.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select, update
from titan.db.models import OutboxMessage, SenderHealthSnapshot, SenderIdentity
from titan.db.session import get_sessionmaker
from titan.delivery import deliverability, sender_health
from titan.delivery.deliverability import ReputationWindow
from titan.delivery.providers.mock import MockEmailProvider
from titan.delivery.sender_health import (
    SenderHealth,
    SenderSnapshot,
    Trend,
    classify,
    reasons,
    severity_rank,
    should_alert,
    trend,
)
from titan.intelligence.sender_auth import MAX_VERIFICATION_AGE

from .conftest import NOW, sending_settings

TODAY = dt.date(2026, 8, 15)


def snapshot(**overrides) -> SenderSnapshot:
    """A healthy, fully authenticated, warmed-up sender. Tests break one thing."""
    base: dict = {
        "sender_identity_id": "sender-1",
        "sending_domain": "arslanvuzmallone.test",
        "captured_on": TODAY,
        "domain_verified": True,
        "spf_ok": True,
        "dkim_ok": True,
        "dmarc_ok": True,
        "auth_stale": False,
        "window": ReputationWindow(sent=400, delivered=390, hard_bounced=2, complained=0),
        "attempts": 400,
        "retries": 3,
        "deferred": 0,
        "sent_today": 40,
        "warmup_day": None,
        "warmup_limit": None,
    }
    base.update(overrides)
    return SenderSnapshot(**base)


# ==========================================================================
# The control
# ==========================================================================
def test_a_clean_sender_is_healthy() -> None:
    """If this fails every other case in this file is vacuous."""
    assert classify(snapshot()) is SenderHealth.HEALTHY
    assert reasons(snapshot()) == ()


# ==========================================================================
# Authentication
# ==========================================================================
@pytest.mark.parametrize("broken", ["domain_verified", "spf_ok", "dkim_ok", "dmarc_ok"])
def test_any_failing_authentication_blocks(broken: str) -> None:
    assert classify(snapshot(**{broken: False})) is SenderHealth.BLOCKED


def test_stale_authentication_blocks_even_with_every_flag_set() -> None:
    """The lesson SenderIdentity.authorization_errors already records: twenty
    identities claimed SPF, DKIM and DMARC on a domain with no DNS at all. A
    boolean is an assertion; only a recent check is evidence."""
    stale = snapshot(auth_stale=True)

    assert stale.domain_verified and stale.spf_ok and stale.dkim_ok and stale.dmarc_ok
    assert stale.authenticated is False
    assert classify(stale) is SenderHealth.BLOCKED
    assert any("re-checked" in r for r in reasons(stale))


def test_the_staleness_window_matches_the_gate_that_refuses_sends() -> None:
    """Two different answers to "is this identity current" would mean a sender
    reported healthy while the send gate refused it."""
    assert MAX_VERIFICATION_AGE == dt.timedelta(days=14)


def test_missing_authentication_is_named_specifically() -> None:
    detail = reasons(snapshot(dkim_ok=False, dmarc_ok=False))
    assert any("DKIM, DMARC" in r for r in detail)


# ==========================================================================
# Reputation -- thresholds come from deliverability, not from here
# ==========================================================================
def test_a_complaint_rate_past_the_pause_threshold_blocks() -> None:
    over = deliverability.COMPLAINT_RATE_PAUSE
    window = ReputationWindow(
        sent=1000, delivered=1000, hard_bounced=0, complained=int(1000 * over) + 1
    )
    assert classify(snapshot(window=window)) is SenderHealth.BLOCKED


def test_an_elevated_complaint_rate_degrades_without_blocking() -> None:
    # 10,000 rather than 1,000: the gap between the warn and pause thresholds
    # is five hundredths of a percent, which does not contain a whole message
    # at the smaller denominator.
    window = ReputationWindow(sent=10_000, delivered=10_000, hard_bounced=0, complained=7)
    rate = window.complaint_rate
    assert (
        deliverability.COMPLAINT_RATE_WARN <= rate < deliverability.COMPLAINT_RATE_PAUSE
    )
    assert classify(snapshot(window=window)) is SenderHealth.DEGRADED


def test_a_bounce_rate_past_the_pause_threshold_blocks() -> None:
    over = deliverability.BOUNCE_RATE_PAUSE
    window = ReputationWindow(
        sent=1000, delivered=900, hard_bounced=int(1000 * over) + 1, complained=0
    )
    assert classify(snapshot(window=window)) is SenderHealth.BLOCKED


def test_rates_below_the_sample_floor_are_ignored() -> None:
    """Two complaints in ten sends is 20% and means nothing. Acting on it would
    pause every new mailbox on its first bad morning."""
    tiny = ReputationWindow(
        sent=deliverability.MIN_SAMPLE_FOR_RATES - 1,
        delivered=8,
        hard_bounced=4,
        complained=2,
    )
    assert tiny.has_signal is False
    assert classify(snapshot(window=tiny, warmup_day=None)) is SenderHealth.HEALTHY


def test_the_thresholds_are_not_restated_here() -> None:
    """Guards the single-source rule. If sender_health grew its own constants
    they would drift from the ones that actually refuse a send."""
    source = (__import__("pathlib").Path(sender_health.__file__)).read_text(
        encoding="utf-8"
    )
    for forbidden in ("0.001", "0.0005", "0.02", "0.01"):
        assert f"= {forbidden}" not in source, (
            f"{forbidden} looks like a copy of a deliverability threshold"
        )


# ==========================================================================
# Throttling
# ==========================================================================
def test_heavy_retrying_is_watched() -> None:
    watched = snapshot(attempts=100, retries=25)
    assert watched.under_throttling_pressure is True
    assert classify(watched) is SenderHealth.WATCH
    assert any("pushing back" in r for r in reasons(watched))


def test_a_few_retries_are_ordinary() -> None:
    assert classify(snapshot(attempts=100, retries=5)) is SenderHealth.HEALTHY


def test_retry_pressure_needs_a_minimum_number_of_attempts() -> None:
    """Two retries out of three attempts is 67% and is three messages."""
    small = snapshot(attempts=3, retries=2)
    assert small.retry_ratio > sender_health.RETRY_PRESSURE_RATIO
    assert small.under_throttling_pressure is False
    assert classify(small) is SenderHealth.HEALTHY


def test_reputation_outranks_throttling() -> None:
    both = snapshot(
        attempts=100,
        retries=40,
        window=ReputationWindow(1000, 1000, 0, 50),
    )
    assert classify(both) is SenderHealth.BLOCKED


# ==========================================================================
# Warm-up is a status, not a fault
# ==========================================================================
def test_a_warming_sender_is_warming_not_degraded() -> None:
    warming = snapshot(
        warmup_day=2,
        warmup_limit=40,
        sent_today=18,
        window=ReputationWindow(60, 58, 0, 0),
    )
    assert classify(warming) is SenderHealth.WARMING
    assert any("warm-up day 3" in r for r in reasons(warming))


def test_warming_does_not_excuse_a_real_problem() -> None:
    assert (
        classify(snapshot(warmup_day=2, warmup_limit=40, spf_ok=False))
        is SenderHealth.BLOCKED
    )


def test_a_sender_that_has_never_sent_is_unknown() -> None:
    assert (
        classify(snapshot(window=ReputationWindow(0, 0, 0, 0), warmup_day=None))
        is SenderHealth.UNKNOWN
    )


# ==========================================================================
# Trend
# ==========================================================================
def test_a_trend_needs_two_points() -> None:
    assert trend([]) is Trend.INSUFFICIENT
    assert trend([SenderHealth.HEALTHY]) is Trend.INSUFFICIENT


def test_getting_worse_is_degrading() -> None:
    assert trend([SenderHealth.DEGRADED, SenderHealth.HEALTHY]) is Trend.DEGRADING
    assert trend([SenderHealth.BLOCKED, SenderHealth.DEGRADED]) is Trend.DEGRADING


def test_getting_better_is_improving() -> None:
    assert trend([SenderHealth.HEALTHY, SenderHealth.DEGRADED]) is Trend.IMPROVING


def test_unchanged_is_stable() -> None:
    assert trend([SenderHealth.DEGRADED, SenderHealth.DEGRADED]) is Trend.STABLE


def test_a_sender_going_quiet_is_not_an_improvement() -> None:
    """UNKNOWN ranks best because it is not a fault, which would make a silent
    mailbox look like a recovering one. There is nothing to compare."""
    assert severity_rank(SenderHealth.UNKNOWN) > severity_rank(SenderHealth.HEALTHY)
    assert trend([SenderHealth.UNKNOWN, SenderHealth.DEGRADED]) is Trend.INSUFFICIENT


# ==========================================================================
# Alerting fires on the edge, not the state
# ==========================================================================
def test_arriving_degraded_alerts() -> None:
    assert should_alert(SenderHealth.DEGRADED, None) is True
    assert should_alert(SenderHealth.BLOCKED, SenderHealth.HEALTHY) is True


def test_staying_degraded_does_not_alert_again() -> None:
    """A mailbox degraded for a fortnight is a known problem. Re-raising it
    daily trains the reader to skip the one that matters."""
    assert should_alert(SenderHealth.DEGRADED, SenderHealth.DEGRADED) is False


def test_recovering_does_not_alert() -> None:
    assert should_alert(SenderHealth.HEALTHY, SenderHealth.BLOCKED) is False
    assert should_alert(SenderHealth.WATCH, SenderHealth.DEGRADED) is False


def test_worsening_within_the_alerting_band_alerts() -> None:
    assert should_alert(SenderHealth.BLOCKED, SenderHealth.DEGRADED) is True


# ==========================================================================
# Persistence -- the part that could not exist before
# ==========================================================================
async def _run(provider: MockEmailProvider, **overrides):
    from titan.delivery.outbox_worker import OutboxWorker

    # now_fn matters: the fixture's approval expiry is relative to NOW, so a
    # worker running on the real clock refuses every message as stale.
    return await OutboxWorker(
        provider, sending_settings(**overrides), now_fn=lambda: NOW
    ).run_once()


async def _snapshots(workspace_id) -> list[SenderHealthSnapshot]:
    async with get_sessionmaker()() as s:
        return list(
            (
                await s.execute(
                    select(SenderHealthSnapshot)
                    .where(SenderHealthSnapshot.workspace_id == workspace_id)
                    .order_by(SenderHealthSnapshot.captured_on.desc())
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_a_send_leaves_a_health_snapshot(db_session, sendable) -> None:
    """The whole feature in one assertion: the numbers outlive the send.

    Before this, both halves were computed per message inside the worker and
    discarded, so no two points in time could ever be compared.
    """
    assert await _snapshots(sendable.workspace_id) == []

    provider = MockEmailProvider()
    assert [r.outcome for r in await _run(provider)] == ["sent"]

    rows = await _snapshots(sendable.workspace_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.sender_identity_id == sendable.sender_id
    assert row.status in {h.value for h in SenderHealth}
    assert row.sending_domain
    # The window is what the decision was made *on*, so it counts messages
    # already sent -- zero for a mailbox whose first message is this one. That
    # is the honest reading: the snapshot records the basis of the decision, not
    # its aftermath.
    assert row.window_sent == 0
    assert row.captured_on == NOW.date()


@pytest.mark.asyncio
async def test_a_second_capture_the_same_day_updates_rather_than_appends(
    db_session, sendable
) -> None:
    """One row per sender per day. Appending per message would produce tens of
    thousands of rows all describing the same rolling window.

    Captured twice against the same outbox row rather than by sending twice:
    build_sendable mints a fresh sender identity per call, so two sends are two
    senders and land in two rows -- correctly, which is why that would not test
    this at all.
    """
    from titan.delivery.outbox_worker import OutboxWorker

    provider = MockEmailProvider()
    await _run(provider)
    assert len(await _snapshots(sendable.workspace_id)) == 1

    worker = OutboxWorker(provider, sending_settings(), now_fn=lambda: NOW)
    async with get_sessionmaker()() as s, s.begin():
        row = (
            await s.execute(
                select(OutboxMessage).where(OutboxMessage.id == sendable.outbox_id)
            )
        ).scalar_one()
        await worker._capture_sender_health(s, row)

    rows = await _snapshots(sendable.workspace_id)
    assert len(rows) == 1, "a second capture on the same day appended a row"
    # The send landed in between, so this capture sees it where the first did not.
    assert rows[0].window_sent == 1


@pytest.mark.asyncio
async def test_the_snapshot_records_why_not_just_what(db_session, sendable) -> None:
    """A status with no reasons is a number an operator cannot act on."""
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(SenderIdentity)
            .where(SenderIdentity.id == sendable.sender_id)
            .values(last_verified_at=None)
        )

    provider = MockEmailProvider()
    await _run(provider)

    rows = await _snapshots(sendable.workspace_id)
    assert rows, "no snapshot written"
    assert rows[0].status == SenderHealth.BLOCKED.value
    assert rows[0].reasons, "blocked with no explanation"


@pytest.mark.asyncio
async def test_failing_to_record_health_never_stops_a_send(
    db_session, sendable, monkeypatch
) -> None:
    """History is a by-product. A message every gate cleared must still go."""
    from titan.delivery.outbox_worker import OutboxWorker

    def boom(*args, **kwargs):
        raise RuntimeError("classifier is on fire")

    # Patched *inside* the savepoint, not over it: replacing
    # _capture_sender_health itself would remove the very handler under test.
    monkeypatch.setattr(OutboxWorker, "_write_sender_health", boom)

    provider = MockEmailProvider()
    assert [r.outcome for r in await _run(provider)] == ["sent"]
    assert provider.delivered_count == 1
