"""The brief goes to a sample, and the sample holds still.

An attachment is a new signal to receivers, and this one shipped while two of
three mailboxes were recovering from bounces. So it goes to a share of messages
first and to everything only once the mailboxes are healthy -- which needs a
switch with a number on it, not an on/off.

Two properties carry the weight, and neither is about the arithmetic:

**A retry must decide the same way.** Random sampling re-rolls on every attempt,
so a message that failed carrying the PDF could retry without it -- two
different documents under one idempotency key, and a reply-rate comparison
measuring the retry path rather than the attachment.

**Widening must not reshuffle.** Because the decision is a hash of the message's
own id against a threshold, raising 10% to 25% only ever *adds* messages. The
first cohort stays intact, so what the trial has already measured survives the
change.
"""

from __future__ import annotations

import uuid

import pytest
from titan.delivery.outbox_worker import carries_one_pager

# Fixed ids: a seeded sample, so a failure is reproducible rather than a
# once-in-a-thousand flake somebody re-runs until it passes.
IDS = [uuid.UUID(int=seed) for seed in range(2000)]


class TestTheSwitchHasANumberOnIt:
    def test_zero_attaches_to_nothing(self) -> None:
        """The default. A trial that silently became a launch because nobody
        set a number is the failure this prevents."""
        assert not any(carries_one_pager(i, 0) for i in IDS)

    def test_one_hundred_attaches_to_everything(self) -> None:
        assert all(carries_one_pager(i, 100) for i in IDS)

    def test_a_negative_percentage_attaches_to_nothing(self) -> None:
        assert not any(carries_one_pager(i, -5) for i in IDS)

    def test_over_one_hundred_attaches_to_everything(self) -> None:
        assert all(carries_one_pager(i, 150) for i in IDS)

    @pytest.mark.parametrize("percent", [5, 10, 25, 50, 75])
    def test_the_share_is_about_right(self, percent: int) -> None:
        """Within three points over two thousand messages -- close enough to
        plan a trial around, and this is a hash rather than a quota."""
        share = 100 * sum(carries_one_pager(i, percent) for i in IDS) / len(IDS)
        assert abs(share - percent) < 3


class TestARetryDecidesTheSameWay:
    def test_the_same_message_always_gets_the_same_answer(self) -> None:
        one = uuid.UUID(int=42)
        assert len({carries_one_pager(one, 10) for _ in range(20)}) == 1

    def test_every_message_is_stable(self) -> None:
        first = [carries_one_pager(i, 25) for i in IDS]
        second = [carries_one_pager(i, 25) for i in IDS]
        assert first == second

    def test_different_messages_get_different_answers(self) -> None:
        """A decision stable per message but identical across messages would be
        a broken switch that happens to pass the test above."""
        answers = {carries_one_pager(i, 50) for i in IDS[:100]}
        assert answers == {True, False}


class TestWideningKeepsTheCohort:
    def test_raising_the_percentage_only_adds(self) -> None:
        """The property that lets the trial grow without discarding its data."""
        for lower, higher in ((5, 10), (10, 25), (25, 50), (50, 100)):
            at_lower = {i for i in IDS if carries_one_pager(i, lower)}
            at_higher = {i for i in IDS if carries_one_pager(i, higher)}
            assert at_lower <= at_higher, f"{lower}% is not a subset of {higher}%"

    def test_lowering_the_percentage_only_removes(self) -> None:
        """The same property backwards: pulling the trial back to a smaller
        share must not sweep in messages that were never in it."""
        at_50 = {i for i in IDS if carries_one_pager(i, 50)}
        at_10 = {i for i in IDS if carries_one_pager(i, 10)}
        assert at_10 <= at_50

    def test_the_cohort_is_not_just_the_first_n(self) -> None:
        """Sampling by id order would put the whole trial in one campaign, or
        one day, or one industry -- and measure that instead of the attachment."""
        treated = [i for i in IDS if carries_one_pager(i, 10)]
        positions = [IDS.index(i) for i in treated]
        assert max(positions) > len(IDS) * 0.8
        assert min(positions) < len(IDS) * 0.2
