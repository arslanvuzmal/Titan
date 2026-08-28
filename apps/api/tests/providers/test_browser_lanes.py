"""Do not start more crawls than the browser worker has lanes to run them.

The Temporal worker's own comment already stated the rule -- "an unbounded
worker will happily start more crawls than the browser worker can serve and
then time out on all of them" -- and then set ``max_concurrent_activities=8``
against a browser worker serving four at a time. Under any real backlog half of
every batch met an instant 503.

Waiting for a lane (see :mod:`tests.providers.test_browser_saturation`) made
that survivable; this stops it being provoked. The two are complementary: the
semaphore keeps the caller inside the worker's capacity, and the waiting
absorbs whatever still collides -- another process, a restart mid-flight.

Lowering the activity limit to four instead would have starved everything else.
Most activities on that worker never touch a browser, and throttling reporting
and verification to the crawl budget is the wrong trade. The bound belongs at
the resource.
"""

from __future__ import annotations

import asyncio

import pytest
from titan.config import Settings
from titan.providers import browser_client


@pytest.fixture(autouse=True)
def fresh_semaphore(monkeypatch):
    """The semaphore is process-global and cached; tests must not inherit one
    another's permit count."""
    monkeypatch.setattr(browser_client, "_lane_semaphore", None)
    monkeypatch.setattr(browser_client, "_lane_permits", None)


def settings_with(lanes: int) -> Settings:
    return Settings(browser_worker_concurrency=lanes)


def test_the_permit_count_is_the_lane_count() -> None:
    """The number that matters. One permit per lane, not per activity slot."""
    assert browser_client._lanes(settings_with(4))._value == 4


def test_the_semaphore_is_shared_across_callers() -> None:
    """A per-client semaphore would bound nothing: each activity builds its own
    ``BrowserWorkerClient``, so eight of them would hold eight private permits
    and the worker would see all eight crawls."""
    first = browser_client._lanes(settings_with(4))
    second = browser_client._lanes(settings_with(4))

    assert first is second


def test_changing_the_setting_rebuilds_it() -> None:
    """Otherwise the first caller in a process pins the limit forever, and a
    config change appears to be ignored."""
    four = browser_client._lanes(settings_with(4))
    eight = browser_client._lanes(settings_with(8))

    assert four is not eight
    assert eight._value == 8


async def test_only_as_many_crawls_run_at_once_as_there_are_lanes() -> None:
    """The property itself, measured: peak concurrency never exceeds the
    permits, however many callers pile in."""
    lanes = 4
    semaphore = browser_client._lanes(settings_with(lanes))

    in_flight = 0
    peak = 0

    async def crawl() -> None:
        nonlocal in_flight, peak
        async with semaphore:
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0)
            in_flight -= 1

    await asyncio.gather(*(crawl() for _ in range(32)))

    assert peak <= lanes
    assert in_flight == 0, "a permit was leaked"


async def test_a_failing_crawl_still_returns_its_permit() -> None:
    """``async with`` is load-bearing here. A crawl that raises inside the lane
    -- an unreachable worker, a guard rejection -- must not retire a lane for
    the life of the process, or the pipeline degrades to zero one error at a
    time."""
    semaphore = browser_client._lanes(settings_with(1))

    with pytest.raises(RuntimeError):
        async with semaphore:
            raise RuntimeError("crawl blew up")

    # Still obtainable: the permit came back.
    await asyncio.wait_for(semaphore.acquire(), timeout=1.0)
    semaphore.release()


async def test_a_waiting_crawl_proceeds_when_a_lane_frees() -> None:
    """Queueing, not refusing. Over-capacity callers wait their turn and then
    run -- which is the whole difference between this and a 503."""
    semaphore = browser_client._lanes(settings_with(1))
    order: list[str] = []

    async def crawl(name: str) -> None:
        async with semaphore:
            order.append(name)
            await asyncio.sleep(0.01)

    await asyncio.gather(crawl("first"), crawl("second"))

    assert sorted(order) == ["first", "second"], "a crawl was dropped, not queued"


def test_the_default_matches_the_browser_workers_own_default() -> None:
    """These are two independent settings in two services describing one
    number. If they drift the symptom is silent: 503s under load, and research
    runs that fail for a reason that looks like a crawler bug.

    The compose file passes BROWSER_WORKER_CONCURRENCY to both; this asserts the
    fallback defaults agree for anyone running outside compose.
    """
    assert Settings().browser_worker_concurrency == 4
