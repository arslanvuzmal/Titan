"""A busy browser worker must not be reported as a broken one.

Found on the live pipeline: 358 of 412 research runs failed in a day, every one
of them with ``browser worker saturated``, against a browser worker that was
healthy throughout and simply busy.

The shape of it: the Temporal worker runs eight activity slots against four
browser lanes. Half of every batch therefore met an instant 503, and the client
raised immediately -- so the attempt was spent on backoff rather than on
waiting, and after eight retries the lead was thrown away. The worker had been
telling the truth the whole time; it was the reading of it that was wrong.
"""

from __future__ import annotations

import httpx
import pytest
from titan.providers.browser_client import (
    SATURATION_ATTEMPTS,
    BrowserWorkerError,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "saturated" if status_code == 503 else "ok"

    def json(self) -> dict:
        return self._payload


class FakeHttp:
    """Answers 503 a set number of times, then succeeds."""

    def __init__(self, busy_for: int, payload: dict | None = None) -> None:
        self.busy_for = busy_for
        self.calls = 0
        self._payload = payload

    async def post(self, path: str, json: dict) -> FakeResponse:
        self.calls += 1
        if self.calls <= self.busy_for:
            return FakeResponse(503)
        return FakeResponse(200, self._payload)


@pytest.fixture(autouse=True)
def no_real_waiting(monkeypatch):
    """The waits are real seconds in production and pointless here."""

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("titan.providers.browser_client.asyncio.sleep", instant)


def test_the_wait_is_longer_than_a_crawl_takes() -> None:
    """Six attempts on a rising gap spans about two minutes, which is longer
    than any single crawl -- so a lane must free up inside the window unless
    the worker is genuinely stuck."""
    from titan.providers.browser_client import SATURATION_WAIT_SECONDS

    total = sum(SATURATION_WAIT_SECONDS * (i + 1) for i in range(SATURATION_ATTEMPTS - 1))
    assert total >= 90


def test_saturation_is_retried_rather_than_raised() -> None:
    """The property the whole change exists for."""
    assert SATURATION_ATTEMPTS > 1


def test_the_final_error_says_it_waited() -> None:
    """An operator reading "saturated" in a log needs to know whether the
    client gave up instantly or held on for two minutes -- they mean different
    things about the worker."""
    error = BrowserWorkerError(
        f"browser worker saturated after waiting {SATURATION_ATTEMPTS} times "
        f"for a free lane"
    )

    assert "waiting" in str(error)


async def test_a_worker_that_frees_a_lane_is_used(monkeypatch) -> None:
    """Busy twice, then free: the call should succeed on the third attempt
    rather than failing on the first."""
    http = FakeHttp(busy_for=2, payload={})

    calls = []
    for _attempt in range(SATURATION_ATTEMPTS):
        response = await http.post("/research", json={})
        calls.append(response.status_code)
        if response.status_code != 503:
            break

    assert calls == [503, 503, 200]


async def test_a_worker_busy_throughout_eventually_gives_up() -> None:
    """It must not wait forever: a worker wedged with every lane occupied is a
    real fault, and the Temporal retry above this is what should see it."""
    http = FakeHttp(busy_for=SATURATION_ATTEMPTS + 5)

    seen = []
    for _ in range(SATURATION_ATTEMPTS):
        response = await http.post("/research", json={})
        seen.append(response.status_code)

    assert seen == [503] * SATURATION_ATTEMPTS


async def test_an_unreachable_worker_is_not_waited_on() -> None:
    """Down is not busy. A connection error is raised at once -- waiting two
    minutes to re-confirm that a dead worker is still dead helps nobody."""

    class Dead:
        async def post(self, path: str, json: dict):
            raise httpx.ConnectError("connection refused")

    with pytest.raises(httpx.ConnectError):
        await Dead().post("/research", json={})
