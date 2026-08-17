"""A response arriving and a human engaging are two different facts.

They used to be one call. Smartlead's statistics row carries ``reply_time`` and
no body, so the delivery poller knew something came back and nothing about what
it said -- and marked the lead REPLIED, which halts outreach permanently and
counts as a success in every rate the system tunes on.

The only reply this workspace has ever received reads "I am currently on annual
leave until Wed 19th August 2026".
"""

from __future__ import annotations

import inspect

from titan.delivery.webhooks import record_reply
from titan.intelligence.replies import ReplyClassification, ReplyKind


def test_recording_a_reply_can_leave_the_sequence_running() -> None:
    """The parameter that separates the two facts."""
    signature = inspect.signature(record_reply)

    assert "stops_sequence" in signature.parameters
    assert signature.parameters["stops_sequence"].default is True


def test_an_auto_reply_does_not_stop_the_sequence() -> None:
    """No human has read anything yet, so there is nothing to respect."""
    assert not ReplyClassification(kind=ReplyKind.AUTO).stops_the_sequence


def test_a_human_reply_does() -> None:
    assert ReplyClassification(kind=ReplyKind.HUMAN).stops_the_sequence


def test_an_unsubscribe_stops_it_and_suppresses() -> None:
    unsub = ReplyClassification(kind=ReplyKind.UNSUBSCRIBE)

    assert unsub.stops_the_sequence
    assert unsub.requires_suppression


def test_the_timestamp_only_path_asks_for_no_stop() -> None:
    """The delivery poller has a time and no body. Whatever it does must not
    depend on what the message said, because it cannot know."""
    from titan.activities import delivery_events

    source = inspect.getsource(delivery_events)

    assert "stops_sequence=False" in source
