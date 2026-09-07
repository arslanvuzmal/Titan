"""The scheduled re-check.

What is pinned here is mostly refusal. The activity's job is to call a module
that already has its own tests, so the interesting behaviour is the two cases
where it must decline to call it at all -- both of which would do lasting
damage quietly rather than fail loudly.
"""

from __future__ import annotations

import pytest
from titan.activities.reverification import HOURLY_BATCH, reverify_contacts
from titan.workflows.types import ReverifyContactsInput


class _Verifier:
    def __init__(self, name: str, *, healthy: bool = True) -> None:
        self.name = name
        self._healthy = healthy

    async def health_check(self) -> tuple[bool, str]:
        return self._healthy, "scripted" if self._healthy else "connection refused"

    async def verify(self, email: str):  # pragma: no cover - must never be reached
        raise AssertionError("the verifier was asked despite the guard")


async def test_a_null_verifier_is_declined_rather_than_run(monkeypatch) -> None:
    """The expensive no-op, and the reason this guard exists.

    The null verifier answers UNKNOWN for everything. A pass would examine the
    batch, learn nothing, and *write a row saying it had asked* -- which then
    excludes those addresses from the next thirty days of real checks. Running
    it is strictly worse than not running it, and it fails silently.
    """
    monkeypatch.setattr(
        "titan.activities.reverification.build_verifier",
        lambda *_a, **_k: _Verifier("null"),
    )

    result = await reverify_contacts(
        ReverifyContactsInput(workspace_id="00000000-0000-0000-0000-000000000000")
    )

    assert result.examined == 0
    assert result.checked == 0
    assert "null verifier" in result.reason


async def test_an_unhealthy_verifier_is_not_asked_for_answers(monkeypatch) -> None:
    """An outage must not be recorded as a batch of answers."""
    monkeypatch.setattr(
        "titan.activities.reverification.build_verifier",
        lambda *_a, **_k: _Verifier("smtp_probe", healthy=False),
    )

    result = await reverify_contacts(
        ReverifyContactsInput(workspace_id="00000000-0000-0000-0000-000000000000")
    )

    assert result.examined == 0
    assert "connection refused" in result.reason


def test_the_batch_is_sized_against_the_window_not_the_hour() -> None:
    """A property worth stating in numbers rather than a comment alone.

    An answer stands for thirty days. The batch has to get through the list in
    comfortably less than that, or addresses expire faster than they are
    re-checked and the schedule never catches up.
    """
    hourly = HOURLY_BATCH
    live_list = 2_400
    days_for_a_full_sweep = live_list / (hourly * 24)

    assert days_for_a_full_sweep < 30, "the list would go stale faster than it is checked"
    # And not so large that a full sweep runs several times inside one window,
    # which spends strangers' connections for answers already in hand.
    assert days_for_a_full_sweep > 1


@pytest.mark.parametrize("batch", [1, 5, HOURLY_BATCH, 200])
def test_the_caller_may_size_a_pass(batch: int) -> None:
    """The input carries a batch so an operator can run a wider catch-up pass
    without editing the constant the schedule relies on."""
    request = ReverifyContactsInput(workspace_id="x", batch=batch)

    assert (request.batch or HOURLY_BATCH) == batch


def test_an_unset_batch_falls_back_to_the_scheduled_size() -> None:
    request = ReverifyContactsInput(workspace_id="x")

    assert request.batch is None
    assert (request.batch or HOURLY_BATCH) == HOURLY_BATCH
