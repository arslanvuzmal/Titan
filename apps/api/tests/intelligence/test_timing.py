"""Which hours of somebody's week are worth writing in.

Almost every test here is about refusing to answer. A working week is
forty-five slots and cold reply rates are single-digit percentages, so the
default state of this data is "not enough", and the failure mode that matters is
ranking noise confidently rather than getting a ranking slightly wrong.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import update
from titan.activities.reporting import _timing_slots
from titan.db.models import Lead, Message
from titan.db.session import get_sessionmaker
from titan.intelligence.timing import (
    MATERIAL_DIFFERENCE,
    MIN_SENDS_PER_SLOT,
    MIN_SLOTS_TO_RANK,
    Slot,
    SlotOutcome,
    SlotVerdict,
    describe,
    learn,
)

from tests.delivery.conftest import build_sendable

NOW = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.UTC)


def slot(weekday: int, hour: int, *, sent: int, replied: int) -> SlotOutcome:
    return SlotOutcome(Slot(weekday, hour), sent=sent, replied=replied)


def week(rate: float, *, n: int = 6, sent: int = MIN_SENDS_PER_SLOT) -> list[SlotOutcome]:
    """A week of judgeable slots all performing identically."""
    return [slot(i % 5, 9 + i, sent=sent, replied=round(sent * rate)) for i in range(n)]


# ==========================================================================
# Refusing to answer
# ==========================================================================
def test_no_data_says_so() -> None:
    report = learn([])

    assert report.total_sent == 0
    assert report.has_enough_to_rank is False
    assert "no sends recorded" in describe(report)


def test_a_slot_below_the_sample_floor_is_unknown_not_average() -> None:
    """Different claims, and only one of them is true. A slot with four
    messages has a reply rate; it does not have a meaning."""
    thin = slot(1, 9, sent=MIN_SENDS_PER_SLOT - 1, replied=1)

    assert thin.has_signal is False
    assert learn([thin]).verdict_for(thin) is SlotVerdict.UNKNOWN


def test_a_handful_of_judgeable_slots_is_not_enough_to_compare() -> None:
    """Comparing two slots is only meaningful against a spread, and a spread of
    two points is not one."""
    few = week(0.05, n=MIN_SLOTS_TO_RANK - 1)
    report = learn(few)

    assert report.judged == MIN_SLOTS_TO_RANK - 1
    assert report.has_enough_to_rank is False
    assert report.ranked() == []
    assert "are needed to compare" in describe(report)


def test_unknown_slots_are_excluded_from_the_ranking_not_sorted_last() -> None:
    """Putting a four-message slot in a ranked list invites somebody to read
    the number."""
    outcomes = [*week(0.05), slot(4, 16, sent=3, replied=3)]
    report = learn(outcomes)

    ranked_slots = [o.slot for o, _ in report.ranked()]
    assert Slot(4, 16) not in ranked_slots


def test_a_flat_week_reports_no_winner() -> None:
    """Every slot judged, none different. Naming a best hour here would be
    ranking noise."""
    report = learn(week(0.05))

    assert report.has_enough_to_rank is True
    assert report.best() == []
    assert report.worst() == []
    assert "none differs" in describe(report)


# ==========================================================================
# Answering, when there is something to say
# ==========================================================================
def test_a_materially_better_slot_is_named() -> None:
    outcomes = [
        *week(0.05),
        slot(2, 10, sent=200, replied=30),  # 15%, three times the baseline
    ]
    report = learn(outcomes)

    assert report.verdict_for(outcomes[-1]) is SlotVerdict.STRONG
    assert outcomes[-1] in report.best()
    assert "Wed 10:00" in describe(report)


def test_a_materially_worse_slot_is_named() -> None:
    outcomes = [*week(0.10), slot(4, 16, sent=200, replied=2)]
    report = learn(outcomes)

    assert report.verdict_for(outcomes[-1]) is SlotVerdict.WEAK
    assert "weakest" in describe(report)


def test_a_small_difference_is_not_a_difference() -> None:
    """Inside the noise a forty-message sample carries."""
    baseline_rate = 0.10
    nudge = baseline_rate * (1 + MATERIAL_DIFFERENCE / 2)
    outcomes = [*week(baseline_rate), slot(3, 14, sent=200, replied=round(200 * nudge))]
    report = learn(outcomes)

    assert report.verdict_for(outcomes[-1]) is SlotVerdict.TYPICAL


def test_the_baseline_ignores_slots_it_cannot_judge() -> None:
    """A hundred barely-used slots would drag the average toward zero and make
    every well-used slot look strong by comparison."""
    outcomes = [*week(0.10), *[slot(5, h, sent=2, replied=0) for h in range(12)]]
    report = learn(outcomes)

    assert report.baseline == pytest.approx(0.10, abs=0.01)


# ==========================================================================
# The slot is the recipient's, not ours
# ==========================================================================
def test_a_slot_is_a_local_weekday_and_hour() -> None:
    """Nine o'clock wherever they are, so a London and a Los Angeles business
    that both read their mail first thing land in the same slot."""
    assert str(Slot(1, 9)) == "Tue 09:00"
    assert str(Slot(6, 17)) == "Sun 17:00"


# ==========================================================================
# The query
# ==========================================================================
async def _sent_at_slot(
    session, workspace_id, *, suffix: str, weekday: int, hour: int, replied: bool = False
):
    fixture = await build_sendable(session, workspace_id, suffix=suffix)
    await session.execute(
        update(Message)
        .where(Message.id == fixture.message_id)
        .values(
            sent_at=NOW,
            local_sent_weekday=weekday,
            local_sent_hour=hour,
            sent_timezone="Europe/London",
        )
    )
    if replied:
        await session.execute(
            update(Lead).where(Lead.id == fixture.lead_id).values(replied_at=NOW)
        )
    await session.commit()
    return fixture


@pytest.mark.asyncio
async def test_the_query_groups_by_local_slot(db_session, workspace) -> None:
    """The regression guard. _timing_slots fails soft, so an empty result is
    indistinguishable from a broken query."""
    await _sent_at_slot(db_session, workspace, suffix="t1", weekday=1, hour=9)
    await _sent_at_slot(
        db_session, workspace, suffix="t2", weekday=1, hour=9, replied=True
    )
    await _sent_at_slot(db_session, workspace, suffix="t3", weekday=3, hour=14)

    outcomes = await _timing_slots(db_session, workspace, NOW)
    by_slot = {(o.slot.weekday, o.slot.hour): o for o in outcomes}

    assert by_slot, "the query returned nothing for three sent messages"
    assert by_slot[(1, 9)].sent == 2
    assert by_slot[(1, 9)].replied == 1
    assert by_slot[(3, 14)].sent == 1


@pytest.mark.asyncio
async def test_a_message_with_no_local_hour_is_excluded(db_session, workspace) -> None:
    """Null means the clock could not be resolved. Treating it as midnight
    would invent sends at 3am and then act on them."""
    fixture = await build_sendable(db_session, workspace, suffix="tnull")
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(Message)
            .where(Message.id == fixture.message_id)
            .values(sent_at=NOW, local_sent_hour=None, local_sent_weekday=None)
        )

    outcomes = await _timing_slots(db_session, workspace, NOW)

    assert sum(o.sent for o in outcomes) == 0


@pytest.mark.asyncio
async def test_a_reply_is_credited_to_the_first_send_not_the_last(
    db_session, workspace
) -> None:
    """A reply follows a conversation, not a single message. Attributing it to
    the most recent follow-up would credit the last message for the first
    message's work."""
    first = await _sent_at_slot(
        db_session, workspace, suffix="tfirst", weekday=1, hour=9, replied=True
    )
    follow_up = await build_sendable(db_session, workspace, suffix="tfollow")
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(Message)
            .where(Message.id == follow_up.message_id)
            .values(
                lead_id=first.lead_id,
                sent_at=NOW + dt.timedelta(days=3),
                local_sent_weekday=4,
                local_sent_hour=16,
            )
        )

    outcomes = await _timing_slots(db_session, workspace, NOW)
    by_slot = {(o.slot.weekday, o.slot.hour): o for o in outcomes}

    assert by_slot[(1, 9)].replied == 1
    assert (4, 16) not in by_slot, "the follow-up was counted as its own send"


@pytest.mark.asyncio
async def test_another_workspace_is_not_counted(db_session, workspace) -> None:
    from titan.db.models import Workspace

    other = Workspace(name="Other", slug=f"o-{uuid.uuid4().hex[:12]}")
    db_session.add(other)
    await db_session.commit()
    other_id = other.id
    try:
        await _sent_at_slot(db_session, other_id, suffix="tiso", weekday=2, hour=11)

        mine = await _timing_slots(db_session, workspace, NOW)
        assert sum(o.sent for o in mine) == 0
    finally:
        from sqlalchemy import delete

        await db_session.rollback()
        await db_session.execute(delete(Workspace).where(Workspace.id == other_id))
        await db_session.commit()
