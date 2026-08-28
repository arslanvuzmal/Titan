"""The throttle counts risk in flight, not releases.

Two versions of this existed and the difference is the whole point.

The first counted "risky channels made active today" as the day's spend. It was
wrong on the live workspace before it ever ran: 34 such channels appeared in one
day from the webmail recovery -- ``snowymedispa@gmail.com`` is not a role
address, so it is risky by shape, and it went active because it was *found*
that day. The activity would have read 34 against a budget of 5 and released
nothing, for a reason unconnected to sending.

It also could not see the accumulation failure. Releasing five a day while every
mailbox is blocked builds a queue: ten quiet days, fifty risky messages, all
leaving together the moment the mailboxes recover -- precisely the event the
throttle exists to prevent.

Counting sent-today plus still-queued fixes both. These hold the arithmetic
directly, without a database, because the counting rule is the part worth
pinning and it is pure.
"""

from __future__ import annotations

import pytest
from titan.activities.trickle import DAILY_RELEASE_BUDGET
from titan.intelligence.contacts import is_never_contact, is_role_address

SAFE = [
    "info@practice.co.uk",
    "reception@clinic.co.uk",
    "kontakt@zahnarzt.de",
    "praxis@zahnarzt.de",
    "biuro@kancelaria.pl",
]
RISKY = [
    "katie@practice.co.uk",
    "lettings@agency.co.uk",
    "dubai@firm.com",
    "snowymedispa@gmail.com",
]
REFUSED = ["careers@firm.com", "datenschutz@praxis.de", "privacy@firm.com"]


def _is_risky(email: str) -> bool:
    """The activity's own rule, restated once so the test states it too."""
    return not is_role_address(email) and not is_never_contact(email)


def _in_flight(sent_today: list[str], queued: list[str]) -> int:
    return sum(1 for e in sent_today if _is_risky(e)) + sum(
        1 for e in queued if _is_risky(e)
    )


def _remaining(sent_today: list[str], queued: list[str], budget: int) -> int:
    return max(0, budget - _in_flight(sent_today, queued))


class TestWhatCountsAsRisk:
    @pytest.mark.parametrize("email", SAFE)
    def test_a_front_desk_is_not_risk(self, email: str) -> None:
        assert not _is_risky(email)

    @pytest.mark.parametrize("email", RISKY)
    def test_a_named_or_departmental_mailbox_is(self, email: str) -> None:
        assert _is_risky(email)

    @pytest.mark.parametrize("email", REFUSED)
    def test_a_refused_address_is_not_counted_as_risk(self, email: str) -> None:
        """It is not throttled, it is forbidden. Counting it here would let a
        hiring inbox consume the budget a real lead was waiting for."""
        assert not _is_risky(email)


class TestTheBudgetIsOnFlight:
    def test_an_empty_day_gets_the_whole_budget(self) -> None:
        assert _remaining([], [], DAILY_RELEASE_BUDGET) == DAILY_RELEASE_BUDGET

    def test_safe_sends_do_not_consume_it(self) -> None:
        """Fifty front-desk messages are not what the throttle is for."""
        assert _remaining(SAFE * 10, [], DAILY_RELEASE_BUDGET) == DAILY_RELEASE_BUDGET

    def test_risky_sends_consume_it(self) -> None:
        assert _remaining(RISKY[:3], [], 5) == 2

    def test_queued_risk_consumes_it_too(self) -> None:
        """The accumulation failure, in one assertion.

        A blocked mailbox means these never left, so they are still exposure --
        counting only what was *sent* would release five more every day and
        deliver the lot at once when the block lifted."""
        assert _remaining([], RISKY[:4], 5) == 1

    def test_sent_and_queued_are_added_not_maxed(self) -> None:
        assert _remaining(RISKY[:2], RISKY[:2], 5) == 1

    def test_the_budget_cannot_go_negative(self) -> None:
        """Over the ceiling releases nothing, rather than releasing backwards."""
        assert _remaining(RISKY * 5, RISKY * 5, 5) == 0

    def test_a_blocked_queue_holds_the_line(self) -> None:
        """The accumulation failure, run forward ten days.

        Each pass releases only its headroom and the queue never drains, so the
        total in flight tops out at the budget and stays there. The version
        that counted releases would have added five a day for ten days and
        delivered fifty at once when the block lifted.
        """
        queue: list[str] = []
        for _ in range(10):
            release = _remaining([], queue, DAILY_RELEASE_BUDGET)
            queue.extend((RISKY * 3)[:release])
            assert _in_flight([], queue) <= DAILY_RELEASE_BUDGET
        assert _in_flight([], queue) == DAILY_RELEASE_BUDGET

    def test_headroom_reappears_as_the_queue_drains(self) -> None:
        full = _remaining([], RISKY[:4], 5)
        drained = _remaining([], RISKY[:1], 5)
        assert drained > full


class TestTheBudgetItself:
    def test_it_is_small_enough_to_keep_the_blend_under_the_block(self) -> None:
        """5 risky at 8.11% inside ~50 sends at 1.30% must stay under 2%.

        The number is a judgement, but this arithmetic is the reason for it and
        an edit that loses the reason should fail here.
        """
        risky, safe = DAILY_RELEASE_BUDGET, 45
        blended = (risky * 0.0811 + safe * 0.0130) / (risky + safe)
        assert blended < 0.02
