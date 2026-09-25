"""Mailbox verification must never be able to kill the activity around it.

`resolve_contact` is given 60 seconds. The SMTP verifier dials port 25, which
most cloud hosts block outbound -- and a blocked port drops rather than
refuses, so each probe waits its full socket timeout, once per MX host and once
per candidate.

When that overran, Temporal cancelled the activity. `asyncio.CancelledError` is
a BaseException, so the `except Exception` that exists precisely to survive a
verification outage never saw it: the research run failed, no draft was
written, and sending fell from 100 a day to 3 while every container reported
itself healthy.
"""

from __future__ import annotations

import asyncio

import pytest
from titan.activities import pipeline


@pytest.fixture(autouse=True)
def _reset_breaker():
    pipeline._verify_timeouts = 0
    yield
    pipeline._verify_timeouts = 0


def test_the_budget_is_well_inside_the_activity_timeout() -> None:
    """60 seconds is what the workflow allows resolve_contact."""
    assert pipeline._VERIFY_BUDGET_SECONDS < 60
    # Enough headroom for the rest of contact resolution to finish too.
    assert pipeline._VERIFY_BUDGET_SECONDS <= 30


@pytest.mark.asyncio
async def test_a_hanging_verifier_raises_a_catchable_timeout() -> None:
    """The fix in one line: TimeoutError is an Exception, CancelledError is not.

    `asyncio.wait_for` converts a hang into something the existing handler can
    catch. Cancellation from outside could never be caught there, which is why
    the whole run died instead of the verification alone.
    """

    async def never_answers() -> None:
        await asyncio.sleep(3600)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(never_answers(), timeout=0.01)

    assert issubclass(TimeoutError, Exception)
    assert not issubclass(asyncio.CancelledError, Exception)


def test_the_breaker_opens_after_repeated_timeouts() -> None:
    """A blocked port will still be blocked for the next lead."""
    assert not pipeline._verification_is_unreachable()
    for _ in range(pipeline._VERIFY_TIMEOUTS_BEFORE_GIVING_UP):
        pipeline._record_verification_timeout()
    assert pipeline._verification_is_unreachable()


def test_one_success_closes_the_breaker_again() -> None:
    """Opening the port restores probing without a deploy."""
    for _ in range(pipeline._VERIFY_TIMEOUTS_BEFORE_GIVING_UP):
        pipeline._record_verification_timeout()
    assert pipeline._verification_is_unreachable()

    pipeline._record_verification_reached()

    assert not pipeline._verification_is_unreachable()
    assert pipeline._verify_timeouts == 0
