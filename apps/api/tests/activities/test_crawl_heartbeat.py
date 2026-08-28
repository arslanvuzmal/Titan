"""A slow crawl is not a dead one, and Temporal can only tell by the beat.

``crawl_lead_website`` heartbeated once before dispatching to the browser
worker and once after it returned, with nothing in between. The gap is longer
than the workflow's 90s ``heartbeat_timeout``: the HTTP client alone allows
``crawl_timeout_seconds + 60`` (180s by default), and since crawls queue for a
browser lane a request may also wait before it is even sent.

Live consequence: sixteen ``LeadResearchWorkflow`` runs sat at ``Attempt 8 of
8`` with ``LastFailure: activity Heartbeat timeout``, having produced nothing.
Temporal killed them for being quiet, retried them, and they went quiet again.

The fix heartbeats on a timer from beside the work, because the wait is inside a
single ``await`` we do not control -- there is nowhere to put a checkpoint.
"""

from __future__ import annotations

import asyncio

import pytest
from titan.activities.pipeline import HEARTBEAT_EVERY_SECONDS, _heartbeating

#: The value in titan/workflows/research.py. Duplicated deliberately: if that
#: one moves, the test below should fail rather than silently follow it.
WORKFLOW_HEARTBEAT_TIMEOUT_SECONDS = 90.0


@pytest.fixture
def beats(monkeypatch):
    """Capture heartbeats without a Temporal activity context."""
    recorded: list[str] = []
    monkeypatch.setattr("titan.activities.pipeline.activity.in_activity", lambda: True)
    monkeypatch.setattr(
        "titan.activities.pipeline.activity.heartbeat",
        lambda note: recorded.append(note),
    )
    return recorded


async def test_the_interval_leaves_room_to_miss_several_beats() -> None:
    """One beat per timeout would make a single scheduling hiccup fatal."""
    assert HEARTBEAT_EVERY_SECONDS * 4 <= WORKFLOW_HEARTBEAT_TIMEOUT_SECONDS


async def test_a_slow_call_is_heartbeated_while_it_runs(monkeypatch, beats) -> None:
    """The property the change exists for: beats arrive *during* the await, not
    only either side of it."""
    monkeypatch.setattr("titan.activities.pipeline.HEARTBEAT_EVERY_SECONDS", 0.01)

    async def slow() -> str:
        await asyncio.sleep(0.1)
        return "pages"

    result = await _heartbeating(slow(), note="crawling")

    assert result == "pages"
    assert beats, "the call ran to completion without a single heartbeat"
    assert set(beats) == {"crawling"}


async def test_a_fast_call_is_not_heartbeated_at_all(beats) -> None:
    """Most crawls finish well inside the interval. Beating anyway would be
    noise, and would cost a scheduler round-trip per crawl."""

    async def quick() -> int:
        return 7

    assert await _heartbeating(quick(), note="crawling") == 7
    assert beats == []


async def test_the_result_is_passed_through_unchanged(beats) -> None:
    """It is a wrapper, not a transformer."""
    sentinel = object()

    async def produce() -> object:
        return sentinel

    assert await _heartbeating(produce(), note="x") is sentinel


async def test_an_exception_propagates(beats) -> None:
    """A crawl that fails must still fail. Swallowing it here would turn a
    broken worker into a silent no-evidence result."""

    async def boom() -> None:
        raise RuntimeError("browser worker unreachable")

    with pytest.raises(RuntimeError, match="unreachable"):
        await _heartbeating(boom(), note="crawling")


async def test_an_exception_after_several_beats_still_propagates(
    monkeypatch, beats
) -> None:
    """The failing path and the slow path together -- the combination the live
    saturation case actually produced."""
    monkeypatch.setattr("titan.activities.pipeline.HEARTBEAT_EVERY_SECONDS", 0.01)

    async def slow_boom() -> None:
        await asyncio.sleep(0.05)
        raise RuntimeError("browser worker saturated")

    with pytest.raises(RuntimeError, match="saturated"):
        await _heartbeating(slow_boom(), note="crawling")

    assert beats, "no heartbeat before the failure"


async def test_no_activity_context_is_not_an_error(monkeypatch) -> None:
    """This module is exercised by tests and by operator commands, where
    ``activity.heartbeat`` raises. Losing the beat outside Temporal costs
    nothing; raising would break the command."""
    monkeypatch.setattr("titan.activities.pipeline.HEARTBEAT_EVERY_SECONDS", 0.01)
    monkeypatch.setattr("titan.activities.pipeline.activity.in_activity", lambda: False)

    async def slow() -> str:
        await asyncio.sleep(0.05)
        return "done"

    assert await _heartbeating(slow(), note="crawling") == "done"


async def test_cancellation_reaches_the_wrapped_call(beats) -> None:
    """Temporal cancels activities on worker shutdown. The wrapper must not
    hold the task alive past its cancellation, or a restart leaks crawls."""

    started = asyncio.Event()

    async def forever() -> None:
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(_heartbeating(forever(), note="crawling"))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
