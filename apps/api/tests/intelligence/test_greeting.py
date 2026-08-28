"""The salutation belongs to the moment the message is sent, not written.

A draft composed on Monday can leave on Thursday: the send window releases it
08:00-17:00 in the recipient's timezone, and nothing about compose time
predicts which side of noon that lands on. "Good morning" arriving at four in
the afternoon is not a small infelicity -- it is the message telling the reader
that nobody was present when it was sent, which is the one thing a cold
approach cannot afford to say out loud.

So these hold two properties. The right word is chosen for the right hour, and
-- far more important -- the rewrite refuses to touch anything it does not
recognise. A stale greeting costs a little credibility. A mangled first line
costs the message.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo

import pytest
from titan.intelligence.greeting import (
    NEUTRAL,
    greeting_at,
    greeting_for_hour,
    retime_greeting,
    retimed_pair,
)

LONDON = zoneinfo.ZoneInfo("Europe/London")


def _at(hour: int) -> dt.datetime:
    return dt.datetime(2026, 8, 27, hour, 30, tzinfo=LONDON)


class TestTheRightWordForTheHour:
    @pytest.mark.parametrize(
        ("hour", "expected"),
        [
            (5, "Good morning"),
            (8, "Good morning"),
            (11, "Good morning"),
            (12, "Good afternoon"),
            (15, "Good afternoon"),
            (17, "Good afternoon"),
            (18, "Good evening"),
            (21, "Good evening"),
        ],
    )
    def test_civil_hours(self, hour: int, expected: str) -> None:
        assert greeting_for_hour(hour) == expected

    @pytest.mark.parametrize("hour", [0, 3, 4, 22, 23])
    def test_outside_civil_hours_falls_back(self, hour: int) -> None:
        """A greeting naming the time of day at 3am is a machine talking."""
        assert greeting_for_hour(hour) == NEUTRAL

    def test_an_unknown_hour_is_the_old_behaviour(self) -> None:
        """``resolve_timezone`` refuses rather than guessing, and so does this."""
        assert greeting_for_hour(None) == NEUTRAL

    def test_the_name_is_carried_through(self) -> None:
        assert greeting_for_hour(9, name="Sarah") == "Good morning Sarah"
        assert greeting_for_hour(None, name="Sarah") == "Hi Sarah"

    def test_a_blank_name_is_not_a_name(self) -> None:
        assert greeting_for_hour(9, name="   ") == "Good morning"

    def test_greeting_at_reads_the_local_hour(self) -> None:
        assert greeting_at(_at(9)) == "Good morning"
        assert greeting_at(_at(14)) == "Good afternoon"
        assert greeting_at(None) == NEUTRAL


class TestRewritingABody:
    def test_the_composer_opener_is_retimed(self) -> None:
        body = "Hi Sarah,\n\nI came across example.com and noticed a problem."
        assert retime_greeting(body, _at(14)).startswith("Good afternoon Sarah,")

    def test_the_rest_of_the_body_is_untouched(self) -> None:
        body = "Hi Sarah,\n\nI came across example.com and noticed a problem."
        rewritten = retime_greeting(body, _at(14))
        assert rewritten.endswith("I came across example.com and noticed a problem.")

    def test_it_is_idempotent(self) -> None:
        body = "Hi Sarah,\n\nBody."
        once = retime_greeting(body, _at(9))
        assert retime_greeting(once, _at(9)) == once

    def test_an_already_correct_greeting_is_left_alone(self) -> None:
        body = "Good morning Sarah,\n\nBody."
        assert retimed_pair(body, _at(9)) is None
        assert retime_greeting(body, _at(9)) == body

    @pytest.mark.parametrize(
        "opener",
        ["Hi there,", "Hello,", "Good morning,", "Good evening Dr Patel,", "Dear Sarah,"],
    )
    def test_every_salutation_shape_is_recognised(self, opener: str) -> None:
        body = opener + "\n\nBody."
        assert retime_greeting(body, _at(14)).startswith("Good afternoon")

    def test_the_greeting_only_is_replaced_not_the_name(self) -> None:
        body = "Dear Dr Patel,\n\nBody."
        assert retime_greeting(body, _at(9)) == "Good morning Dr Patel,\n\nBody."


class TestRefusingToTouchWhatItDoesNotUnderstand:
    """The expensive failure is a mangled first line, not a stale greeting."""

    def test_an_unresolvable_timezone_changes_nothing(self) -> None:
        body = "Hi Sarah,\n\nBody."
        assert retime_greeting(body, None) == body

    def test_an_empty_body_changes_nothing(self) -> None:
        assert retime_greeting("", _at(9)) == ""

    def test_a_body_that_does_not_open_with_a_salutation_is_left_alone(self) -> None:
        body = "I came across example.com and noticed the booking page is down."
        assert retime_greeting(body, _at(9)) == body

    def test_a_salutation_further_down_is_not_touched(self) -> None:
        """Anchored at the start, so prose containing "hello" is safe."""
        body = "Thanks for the note.\n\nHi Sarah, I meant to add something."
        assert retime_greeting(body, _at(9)) == body

    def test_it_does_not_run_past_the_first_line(self) -> None:
        body = "Hi Sarah,\nI came across example.com.\n\nMore."
        rewritten = retime_greeting(body, _at(9))
        assert rewritten == "Good morning Sarah,\nI came across example.com.\n\nMore."

    def test_punctuation_is_preserved_as_written(self) -> None:
        assert retime_greeting("Hello!\n\nBody.", _at(9)) == "Good morning!\n\nBody."
