"""The greeting is repaired at the wire, like the footer and the unsubscribe.

Three things about a message are unknowable when the draft is written and
certain when it is sent: which mailbox the pool picked, whether that mailbox
supports one-click unsubscribe, and what time it is where the recipient is.
The first two already had repairs here, both of them written after messages
were cancelled in bulk over the drift. This is the third, and it is the mildest
of them -- nothing is refused over a salutation -- so the whole design point is
that it never makes anything worse.

That is what most of these hold: an unresolvable timezone, an opening line the
matcher does not recognise, an HTML part that does not contain what the text
part does. Every one of them has to return the message exactly as it arrived.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo

import pytest
from titan.delivery.outbox_worker import with_local_greeting
from titan.delivery.providers.base import OutboundEmail

SYDNEY = zoneinfo.ZoneInfo("Australia/Sydney")


def _email(text: str, html: str | None = None) -> OutboundEmail:
    return OutboundEmail(
        to_email="owner@practice.example",
        from_email="outreach@arslanvuzmallone.com",
        from_name="Arslan Vuzmal",
        reply_to="outreach@arslanvuzmallone.com",
        subject="Quick note about your booking page",
        text_body=text,
        html_body=html,
    )


def _at(hour: int) -> dt.datetime:
    return dt.datetime(2026, 8, 27, hour, 15, tzinfo=SYDNEY)


BODY = "Hi Sarah,\n\nI came across example.com and noticed the button 404s.\n"
HTML = (
    '<div><p style="margin:0 0 16px;">Hi Sarah,</p>'
    '<p style="margin:0 0 16px;">I came across example.com.</p></div>'
)


class TestItSetsTheRecipientsTimeOfDay:
    @pytest.mark.parametrize(
        ("hour", "expected"),
        [(9, "Good morning Sarah,"), (14, "Good afternoon Sarah,"), (19, "Good evening Sarah,")],
    )
    def test_the_text_part(self, hour: int, expected: str) -> None:
        repaired = with_local_greeting(_email(BODY), _at(hour))
        assert repaired.text_body.startswith(expected)

    def test_the_html_part_is_repaired_to_match(self) -> None:
        """Two parts of one message disagreeing is worse than either being stale."""
        repaired = with_local_greeting(_email(BODY, HTML), _at(14))
        assert "Good afternoon Sarah," in (repaired.html_body or "")
        assert "Hi Sarah," not in (repaired.html_body or "")

    def test_only_the_greeting_changes(self) -> None:
        repaired = with_local_greeting(_email(BODY, HTML), _at(14))
        assert "I came across example.com and noticed the button 404s." in (
            repaired.text_body
        )
        assert repaired.subject == "Quick note about your booking page"
        assert repaired.to_email == "owner@practice.example"

    def test_it_reads_the_recipients_clock_not_ours(self) -> None:
        """The whole point. 09:00 in Sydney is the previous evening in London."""
        morning_in_sydney = dt.datetime(
            2026, 8, 27, 9, 0, tzinfo=SYDNEY
        )
        repaired = with_local_greeting(_email(BODY), morning_in_sydney)
        assert repaired.text_body.startswith("Good morning")

    def test_it_is_idempotent(self) -> None:
        once = with_local_greeting(_email(BODY, HTML), _at(9))
        twice = with_local_greeting(once, _at(9))
        assert twice.text_body == once.text_body
        assert twice.html_body == once.html_body


class TestItNeverMakesThingsWorse:
    def test_an_unresolvable_timezone_returns_the_message_untouched(self) -> None:
        """``resolve_timezone`` refuses rather than guessing, and so does this."""
        email = _email(BODY, HTML)
        assert with_local_greeting(email, None) is email

    def test_an_unrecognised_opening_line_is_left_alone(self) -> None:
        email = _email("I came across example.com and noticed a problem.\n")
        assert with_local_greeting(email, _at(9)) is email

    def test_an_empty_body_is_left_alone(self) -> None:
        email = _email("")
        assert with_local_greeting(email, _at(9)) is email

    def test_an_already_correct_greeting_is_not_rewritten(self) -> None:
        email = _email("Good morning Sarah,\n\nBody.\n")
        assert with_local_greeting(email, _at(9)) is email

    def test_a_missing_html_part_is_not_invented(self) -> None:
        repaired = with_local_greeting(_email(BODY, None), _at(9))
        assert repaired.html_body is None

    def test_an_html_part_that_does_not_contain_the_greeting_survives(self) -> None:
        """A template mismatch must not blank the HTML part."""
        html = "<div><p>Something else entirely.</p></div>"
        repaired = with_local_greeting(_email(BODY, html), _at(9))
        assert repaired.html_body == html
        assert repaired.text_body.startswith("Good morning Sarah,")

    def test_a_name_needing_escaping_still_matches_in_the_html(self) -> None:
        """The composer escapes the greeting, so a raw match would miss it."""
        text = "Hi Ben & Co,\n\nBody.\n"
        html = '<div><p style="margin:0 0 16px;">Hi Ben &amp; Co,</p></div>'
        repaired = with_local_greeting(_email(text, html), _at(9))
        assert "Good morning Ben &amp; Co," in (repaired.html_body or "")

    def test_the_greeting_is_replaced_once_not_everywhere(self) -> None:
        text = "Hi Sarah,\n\nI wrote 'Hi Sarah,' on the form as well.\n"
        repaired = with_local_greeting(_email(text), _at(9))
        assert repaired.text_body.count("Hi Sarah,") == 1
        assert repaired.text_body.startswith("Good morning Sarah,")
