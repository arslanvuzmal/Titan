"""The one alarm that works by not going off.

Every other guard in this system runs inside the process it is watching, so
none of them can report the failure that has actually happened: the machine
being off. Nothing went out on 4, 5 or 6 September 2026 and nobody knew until
somebody went looking.

A ping already existed on the daily report. That is the right signal for "the
report went out" and the wrong one for "the stack is up" -- it fires once a
day, so a three-day outage is two days old before anyone hears. This runs on
the hourly housekeeping pass instead.

The tests below are mostly about the two ways this could quietly stop being a
dead man's switch: pinging when it should not, and failing loudly enough to
take the repairs down with it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from titan.activities.vitals import ALL_VITALS_ACTIVITIES, ping_watchdog
from titan.workflows.types import PingWatchdogInput

pytestmark = pytest.mark.asyncio

URL = "https://hc-ping.example/abc123"


class _Pinger:
    def __init__(self, fails: Exception | None = None) -> None:
        self.calls = 0
        self.fails = fails

    async def __call__(self) -> None:
        self.calls += 1
        if self.fails is not None:
            raise self.fails


@pytest.fixture
def pinger(monkeypatch) -> _Pinger:
    stub = _Pinger()
    monkeypatch.setattr("titan.activities.daily_report.healthcheck_pinger", stub)
    return stub


def _url(monkeypatch, value: str | None) -> None:
    """Point the activity at a configured URL, or at none.

    ``Settings`` is a frozen pydantic model, so the setting cannot be poked in
    place -- which is the right design and simply means the seam is the
    accessor rather than the object.
    """
    from titan.config import get_settings

    real = get_settings()
    stub = SimpleNamespace(**{**real.model_dump(), "healthcheck_ping_url": value})
    monkeypatch.setattr("titan.activities.vitals.get_settings", lambda: stub)


async def test_it_pings_when_a_url_is_configured(monkeypatch, pinger) -> None:
    """The whole point, and the only path that keeps the switch armed."""
    _url(monkeypatch, URL)

    result = await ping_watchdog(PingWatchdogInput())

    assert result.pinged is True
    assert pinger.calls == 1


async def test_no_url_is_not_a_failure(monkeypatch, pinger) -> None:
    """Planted violation: raise, or report success, when unconfigured.

    This is the shipped state -- the wiring lands before the URL exists, so
    turning it on later is a configuration change and not a deployment. It must
    not look like a broken hourly job in the meantime, and it must not claim to
    have pinged something that does not exist.
    """
    _url(monkeypatch, None)

    result = await ping_watchdog(PingWatchdogInput())

    assert result.pinged is False
    assert result.reason == "no url configured"
    assert pinger.calls == 0, "nothing to ping"


async def test_an_empty_url_counts_as_unconfigured(monkeypatch, pinger) -> None:
    """An env var set to the empty string is the commonest way to half-configure."""
    _url(monkeypatch, "")

    result = await ping_watchdog(PingWatchdogInput())

    assert result.pinged is False
    assert pinger.calls == 0


async def test_an_unreachable_watchdog_never_raises(monkeypatch) -> None:
    """Planted violation: let the ping fail the housekeeping pass.

    A watchdog that cannot be reached is a monitoring problem. Failing the pass
    over it would let an outage at the monitoring service stop the sweeps being
    monitored -- the repair job taken down by the thing watching it.
    """
    _url(monkeypatch, URL)
    monkeypatch.setattr(
        "titan.activities.daily_report.healthcheck_pinger",
        _Pinger(fails=OSError("connection refused")),
    )

    result = await ping_watchdog(PingWatchdogInput())

    assert result.pinged is False
    assert "OSError" in result.reason


async def test_a_failed_ping_is_distinguishable_from_an_unconfigured_one(
    monkeypatch,
) -> None:
    """Both report pinged=False, and they mean opposite things.

    One is "you have not turned this on yet"; the other is "you turned it on
    and it is broken". A log that cannot tell them apart is a log that lets the
    second hide behind the first.
    """
    _url(monkeypatch, None)
    unconfigured = await ping_watchdog(PingWatchdogInput())

    _url(monkeypatch, URL)
    monkeypatch.setattr(
        "titan.activities.daily_report.healthcheck_pinger",
        _Pinger(fails=TimeoutError("timed out")),
    )
    broken = await ping_watchdog(PingWatchdogInput())

    assert unconfigured.reason != broken.reason
    assert unconfigured.pinged is broken.pinged is False


async def test_the_activity_is_registered_on_the_maintenance_worker() -> None:
    """An activity the worker does not know about is a workflow that times out.

    ``sweep_stranded_drafts`` was reachable only from a CLI command for months
    for want of exactly this kind of check.
    """
    names = {a.__name__ for a in ALL_VITALS_ACTIVITIES}

    assert "ping_watchdog" in names
