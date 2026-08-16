"""Delivery outcomes sliced the six ways decisions are actually made.

`_campaign_outcomes` answers "how is this campaign doing", which is what the
orchestrator asks before dispatching. Nothing answered the questions a learning
system asks -- which mailbox is degrading, which domain refuses us, which hour
of the recipient's day gets answered.

Two properties carry the weight here. Raw SQL must name its workspace, because
row-level security does not scope it; and a rate must not exist below the sample
floor, because a slice with two sends and one bounce is not a 50% bounce rate.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import update
from titan.db.models import Message, Workspace
from titan.db.session import workspace_session
from titan.delivery.deliverability import MIN_SAMPLE_FOR_RATES
from titan.intelligence.rollups import (
    Dimension,
    Slice,
    all_dimensions,
    best_by_positive_reply,
    outcomes_by,
    worst_by_bounce,
)

from tests.delivery.conftest import build_sendable

NOW = dt.datetime(2026, 8, 16, 12, 0, tzinfo=dt.UTC)


def slice_of(sent: int, *, bounced: int = 0, positive: int = 0) -> Slice:
    return Slice(
        dimension=Dimension.SENDER,
        key="k",
        label="l",
        sent=sent,
        delivered=sent - bounced,
        bounced=bounced,
        complained=0,
        replied=positive,
        positive_replies=positive,
        meetings=0,
    )


async def _sent_message(session, workspace_id, *, suffix: str, **overrides):
    """A message that actually went out, so it appears in every rollup."""
    built = await build_sendable(session, workspace_id, suffix=suffix)
    values = {
        "sent_at": NOW - dt.timedelta(days=1),
        "local_sent_hour": 10,
        "local_sent_weekday": 3,
        "sent_timezone": "Europe/London",
        **overrides,
    }
    await session.execute(
        update(Message).where(Message.id == built.message_id).values(**values)
    )
    await session.flush()
    return built


# --------------------------------------------------------------- the sample floor


def test_no_rate_exists_below_the_sample_floor() -> None:
    """A slice with two sends and one bounce is not a 50% bounce rate.

    Publishing the number invites acting on it, and any ranking built from such
    numbers sorts mostly by who has the smallest sample.
    """
    thin = slice_of(MIN_SAMPLE_FOR_RATES - 1, bounced=1)

    assert thin.has_signal is False
    assert thin.bounce_rate is None
    assert thin.reply_rate is None
    assert thin.positive_reply_rate is None
    assert "below the sample floor" in thin.describe()


def test_a_rate_appears_once_the_slice_is_measured() -> None:
    measured = slice_of(MIN_SAMPLE_FOR_RATES, bounced=MIN_SAMPLE_FOR_RATES // 10)

    assert measured.has_signal is True
    assert measured.bounce_rate == pytest.approx(0.1, abs=0.02)


def test_the_floor_is_the_one_the_rest_of_the_system_uses() -> None:
    """A private threshold here would rank slices the delivery gate ignores."""
    assert slice_of(MIN_SAMPLE_FOR_RATES - 1).has_signal is False
    assert slice_of(MIN_SAMPLE_FOR_RATES).has_signal is True


# ------------------------------------------------------------------- the rankings


def test_the_worst_slice_is_chosen_only_among_measured_ones() -> None:
    """Otherwise this returns whichever group has one send and one bounce."""
    noise = slice_of(1, bounced=1)
    real = slice_of(MIN_SAMPLE_FOR_RATES * 2, bounced=MIN_SAMPLE_FOR_RATES // 4)

    assert worst_by_bounce([noise, real]) is real


def test_nothing_is_ranked_when_nothing_is_measured() -> None:
    assert worst_by_bounce([slice_of(3, bounced=3)]) is None
    assert best_by_positive_reply([slice_of(3, positive=3)]) is None
    assert worst_by_bounce([]) is None


# ------------------------------------------------------------------ the isolation


async def test_the_rollup_does_not_cross_workspaces(db_session, workspace) -> None:
    """The invariant that matters most.

    `workspace_session` scopes ORM queries through `with_loader_criteria`, which
    never sees a `text()` block, and every RLS policy is permissive while
    `titan.workspace_id` is unset. Raw SQL that does not name its workspace is
    not scoped at all -- it only looks as though it is.
    """
    await _sent_message(db_session, workspace, suffix="mine")

    other = Workspace(name="Other", slug=f"other-{uuid.uuid4().hex[:10]}")
    db_session.add(other)
    await db_session.commit()
    try:
        await _sent_message(db_session, other.id, suffix="theirs")
        await db_session.commit()

        async with workspace_session(workspace) as scoped:
            mine = await outcomes_by(scoped, Dimension.CAMPAIGN, now=NOW)
        async with workspace_session(other.id) as scoped:
            theirs = await outcomes_by(scoped, Dimension.CAMPAIGN, now=NOW)

        assert len(mine) == 1
        assert len(theirs) == 1
        assert {s.key for s in mine}.isdisjoint({s.key for s in theirs})
    finally:
        from sqlalchemy import delete

        await db_session.execute(delete(Workspace).where(Workspace.id == other.id))
        await db_session.commit()


# ------------------------------------------------------------------- the slicings


async def test_every_dimension_returns_the_send(db_session, workspace) -> None:
    """Six groupings of the same counters. A send belongs to all of them."""
    await _sent_message(db_session, workspace, suffix="alldims")
    await db_session.commit()

    async with workspace_session(workspace) as scoped:
        data = await all_dimensions(scoped, now=NOW)

    assert set(data) == set(Dimension)
    for dimension in (
        Dimension.CAMPAIGN,
        Dimension.SENDER,
        Dimension.RECIPIENT_DOMAIN,
        Dimension.LOCAL_SLOT,
        Dimension.VARIANT,
    ):
        assert data[dimension], f"{dimension.value} returned nothing"
        assert sum(s.sent for s in data[dimension]) >= 1


async def test_a_send_with_no_resolved_clock_is_not_a_time_of_day(
    db_session, workspace
) -> None:
    """Dropped, not bucketed into a null slot.

    A row mixing every message whose clock could not be resolved is not a time
    of day, and ranking it against real slots compares a fact to an absence.
    """
    await _sent_message(
        db_session,
        workspace,
        suffix="noclock",
        local_sent_hour=None,
        local_sent_weekday=None,
        sent_timezone=None,
    )
    await db_session.commit()

    async with workspace_session(workspace) as scoped:
        slots = await outcomes_by(scoped, Dimension.LOCAL_SLOT, now=NOW)

    assert slots == []


async def test_the_local_slot_is_labelled_in_the_recipients_week(
    db_session, workspace
) -> None:
    """Monday is 0, matching `datetime.weekday()` and the stored column."""
    await _sent_message(
        db_session, workspace, suffix="thu", local_sent_weekday=3, local_sent_hour=9
    )
    await db_session.commit()

    async with workspace_session(workspace) as scoped:
        slots = await outcomes_by(scoped, Dimension.LOCAL_SLOT, now=NOW)

    assert slots[0].label == "Thu 09:00"


async def test_a_send_outside_the_window_is_not_counted(db_session, workspace) -> None:
    """The same trailing window the reputation gate uses."""
    await _sent_message(db_session, workspace, suffix="old")
    await db_session.execute(
        update(Message).values(created_at=NOW - dt.timedelta(days=90))
    )
    await db_session.commit()

    async with workspace_session(workspace) as scoped:
        rows = await outcomes_by(scoped, Dimension.CAMPAIGN, now=NOW)

    assert rows == []
