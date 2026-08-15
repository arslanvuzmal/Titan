"""Deciding whether one variant actually beat another.

The failure this defends against is not getting a ranking slightly wrong. It is
promoting on noise, and then the promotion looking like evidence for the next
decision -- which is how a system talks itself into a phrasing nobody tested.
So most of these assert that a difference was *refused*.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import update
from titan.activities.reporting import _variant_arms
from titan.autonomy.experiments import (
    MIN_OUTCOMES_PER_ARM,
    MIN_SENDS_PER_ARM,
    Arm,
    Verdict,
    assign,
    best_against_control,
    compare,
    describe,
)
from titan.db.models import Lead, Message, MessageDraft
from titan.db.session import get_sessionmaker

from tests.delivery.conftest import build_sendable

NOW = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.UTC)


# ==========================================================================
# Refusing to call a winner
# ==========================================================================
def test_a_plausible_looking_difference_is_refused() -> None:
    """5% against 7% over two hundred each is four replies. Promoting on that
    is promoting noise."""
    result = compare(Arm("v0", 200, 10), Arm("v1", 200, 14))

    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.p_value is not None
    assert result.p_value > 0.05
    assert "inside the noise" in describe(result)


def test_an_arm_below_the_send_floor_is_not_tested() -> None:
    result = compare(Arm("v0", MIN_SENDS_PER_ARM - 1, 8), Arm("v1", 400, 40))

    assert result.verdict is Verdict.INSUFFICIENT
    assert result.p_value is None, "a p-value here would be decoration"


def test_an_arm_with_too_few_replies_is_not_tested() -> None:
    """The normal approximation needs enough of *both* outcomes. Below that it
    is not conservative, it is undefined."""
    thin = Arm("v0", 400, MIN_OUTCOMES_PER_ARM - 1)

    assert thin.is_testable is False
    assert compare(thin, Arm("v1", 400, 40)).verdict is Verdict.INSUFFICIENT


def test_an_arm_with_too_few_non_replies_is_also_not_tested() -> None:
    """An arm of five that all replied fails as surely as one that none did."""
    everyone = Arm("v0", 400, 400 - (MIN_OUTCOMES_PER_ARM - 1))

    assert everyone.is_testable is False


def test_two_identical_arms_are_inconclusive_not_a_tie_for_first() -> None:
    result = compare(Arm("v0", 500, 25), Arm("v1", 500, 25))

    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.winner is None


def test_two_arms_that_never_replied_are_insufficient_not_tied() -> None:
    """Zero replies fails the outcome floor, so this never reaches the test at
    all -- which is also why compare() needs no zero-variance guard. A pair that
    would divide by zero is refused before the arithmetic."""
    result = compare(Arm("v0", 500, 0), Arm("v1", 500, 0))

    assert result.verdict is Verdict.INSUFFICIENT
    assert result.p_value is None
    assert result.lift is None, "an increase over nothing is undefined, not infinite"


def test_a_testable_arm_can_never_produce_zero_variance() -> None:
    """The property the missing guard would have covered, asserted directly."""
    for sent in (100, 500, 5000):
        for replied in (MIN_OUTCOMES_PER_ARM, sent // 2, sent - MIN_OUTCOMES_PER_ARM):
            arm = Arm("v", sent, replied)
            if not arm.is_testable:
                continue
            assert 0 < arm.reply_rate < 1


# ==========================================================================
# Calling a winner, when there is one
# ==========================================================================
def test_a_real_difference_is_found() -> None:
    result = compare(Arm("v0", 800, 40), Arm("v1", 800, 96))

    assert result.verdict is Verdict.CHALLENGER_WINS
    assert result.winner is not None
    assert result.winner.key == "v1"
    assert result.p_value is not None and result.p_value < 0.01
    assert result.lift == pytest.approx(1.4)


def test_the_control_can_win_too() -> None:
    """Two-tailed: the question is whether the arms differ, not whether the
    challenger is better. Testing one tail because you hoped for a direction
    turns a 5% false-positive rate into 10%."""
    result = compare(Arm("v0", 800, 96), Arm("v1", 800, 40))

    assert result.verdict is Verdict.CONTROL_WINS
    assert result.winner is not None and result.winner.key == "v0"


# ==========================================================================
# Assignment
# ==========================================================================
def test_assignment_is_stable_for_a_subject() -> None:
    """An assignment re-drawn on a retry puts one lead in two arms and quietly
    corrupts both."""
    assert len({assign("phrasing", "lead-7", 4) for _ in range(50)}) == 1


def test_two_experiments_do_not_split_the_population_the_same_way() -> None:
    """Otherwise every lead in arm 0 of the first is in arm 0 of the second and
    the two results are entangled in a way nothing downstream can detect."""
    overlap = sum(
        assign("a", f"lead{i}", 4) == assign("b", f"lead{i}", 4) for i in range(4000)
    )
    assert 0.20 < overlap / 4000 < 0.30  # 0.25 is independence


def test_assignment_spreads_across_the_arms() -> None:
    counts = [0] * 4
    for i in range(4000):
        counts[assign("phrasing", f"lead{i}", 4)] += 1
    assert all(850 < c < 1150 for c in counts), counts


def test_an_experiment_needs_an_arm() -> None:
    with pytest.raises(ValueError):
        assign("phrasing", "lead-1", 0)


# ==========================================================================
# Picking which comparison to report
# ==========================================================================
def test_the_control_is_the_arm_with_the_most_evidence() -> None:
    """Not the first alphabetically."""
    result = best_against_control(
        [Arm("v2", 900, 45), Arm("v0", 120, 6), Arm("v1", 150, 8)]
    )

    assert result is not None
    assert result.control.key == "v2"


def test_one_arm_is_not_an_experiment() -> None:
    assert best_against_control([Arm("v0", 900, 45)]) is None
    assert best_against_control([]) is None


def test_a_decisive_result_is_preferred_over_a_larger_inconclusive_one() -> None:
    arms = [
        Arm("v0", 2000, 100),  # control, 5%
        Arm("v1", 1500, 78),  # 5.2%, inconclusive
        Arm("v2", 400, 48),  # 12%, decisive
    ]
    result = best_against_control(arms)

    assert result is not None
    assert result.verdict is Verdict.CHALLENGER_WINS
    assert result.challenger.key == "v2"


def test_nothing_testable_still_describes_itself() -> None:
    assert "no variant has enough sends" in describe(None)
    assert describe(compare(Arm("v0", 10, 1), Arm("v1", 10, 2)))


# ==========================================================================
# The query
# ==========================================================================
async def _sent_with_variant(
    session, workspace_id, *, suffix: str, variant: str | None, replied: bool = False
):
    fixture = await build_sendable(session, workspace_id, suffix=suffix)
    await session.execute(
        update(MessageDraft)
        .where(MessageDraft.id == fixture.draft_id)
        .values(variant=variant)
    )
    await session.execute(
        update(Message).where(Message.id == fixture.message_id).values(sent_at=NOW)
    )
    if replied:
        await session.execute(
            update(Lead).where(Lead.id == fixture.lead_id).values(replied_at=NOW)
        )
    await session.commit()
    return fixture


@pytest.mark.asyncio
async def test_the_query_groups_by_variant(db_session, workspace) -> None:
    """The regression guard. _variant_arms fails soft, so empty is
    indistinguishable from broken."""
    await _sent_with_variant(db_session, workspace, suffix="e1", variant="v0")
    await _sent_with_variant(
        db_session, workspace, suffix="e2", variant="v0", replied=True
    )
    await _sent_with_variant(db_session, workspace, suffix="e3", variant="v1")

    arms = {a.key: a for a in await _variant_arms(db_session, workspace, NOW)}

    assert arms, "the query returned nothing for three sent drafts"
    assert arms["v0"].sent == 2
    assert arms["v0"].replied == 1
    assert arms["v1"].sent == 1


@pytest.mark.asyncio
async def test_follow_ups_do_not_inflate_an_arm(db_session, workspace) -> None:
    """A lead gets four messages from one variant. Counting messages would
    multiply every arm by its follow-up count without adding a single
    independent observation -- exactly what a significance test must not be
    given."""
    first = await _sent_with_variant(db_session, workspace, suffix="ef1", variant="v0")
    follow_up = await build_sendable(db_session, workspace, suffix="ef2")
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(MessageDraft)
            .where(MessageDraft.id == follow_up.draft_id)
            .values(variant="v0")
        )
        await s.execute(
            update(Message)
            .where(Message.id == follow_up.message_id)
            .values(lead_id=first.lead_id, sent_at=NOW + dt.timedelta(days=3))
        )

    arms = {a.key: a for a in await _variant_arms(db_session, workspace, NOW)}

    assert arms["v0"].sent == 1, "a follow-up was counted as a second observation"


@pytest.mark.asyncio
async def test_a_draft_with_no_variant_is_excluded(db_session, workspace) -> None:
    """Drafts written before the column existed. Null is not an arm."""
    await _sent_with_variant(db_session, workspace, suffix="enull", variant=None)

    assert await _variant_arms(db_session, workspace, NOW) == []


@pytest.mark.asyncio
async def test_another_workspace_is_not_counted(db_session, workspace) -> None:
    from titan.db.models import Workspace

    other = Workspace(name="Other", slug=f"o-{uuid.uuid4().hex[:12]}")
    db_session.add(other)
    await db_session.commit()
    other_id = other.id
    try:
        await _sent_with_variant(db_session, other_id, suffix="eiso", variant="v9")

        mine = await _variant_arms(db_session, workspace, NOW)
        assert all(a.key != "v9" for a in mine)
    finally:
        from sqlalchemy import delete

        await db_session.rollback()
        await db_session.execute(delete(Workspace).where(Workspace.id == other_id))
        await db_session.commit()
