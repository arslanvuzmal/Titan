"""Mail in the outreach mailbox is not the same thing as a prospect replying.

The live workspace reported fourteen human replies and had received none. Six
were Smartlead warm-up bots, three were delivery tests from the operator's own
Gmail, and the rest was vendor mail. Every reply-rate and conversion figure
derived from that was wrong.

One of them did real damage. The delivery tests threaded back onto lead
malmin.co.uk, classified HUMAN, and ``record_reply`` retired it -- ``REPLIED``
is terminal, so a real dental practice will never be written to again. Nothing
malfunctioned: the classifier answered "what did this person mean" on a message
no recipient had written, because it had no way to ask "was there a recipient".

That question now runs first, and these tests hold both halves of it: the mail
that is not a prospect is recognised, and the mail that *is* one still reaches
every rule that was there before.
"""

from __future__ import annotations

import pytest
from titan.intelligence.replies import (
    InboundMessage,
    ReplyKind,
    classify_reply,
    is_own_address,
)

OURS = frozenset(
    {
        "outreach@arslanvuzmallone.com",
        "sales@arslanvuzmallone.com",
        "projects@arslanvuzmallone.com",
        "arslanvuzmallone@gmail.com",
    }
)


def _msg(**kw: object) -> InboundMessage:
    payload: dict[str, object] = {
        "from_email": "owner@practice.example",
        "subject": "Re: your booking page",
        "body_text": "Thanks for getting in touch.",
    }
    payload.update(kw)
    return InboundMessage(**payload)  # type: ignore[arg-type]


class TestOurOwnMail:
    @pytest.mark.parametrize("address", sorted(OURS))
    def test_every_address_of_ours_is_recognised(self, address: str) -> None:
        result = classify_reply(_msg(from_email=address), own_addresses=OURS)
        assert result.kind is ReplyKind.NOT_A_PROSPECT
        assert "own_address" in result.signals

    def test_the_exact_message_that_retired_malmin(self) -> None:
        """The regression, in the words it actually arrived in."""
        result = classify_reply(
            _msg(
                from_email="arslanvuzmallone@gmail.com",
                subject="Re: delivery test",
                body_text="Ready ! On Mon, Aug 24, 2026 Titan wrote: > This is a "
                "delivery test sent by Titan",
            ),
            own_addresses=OURS,
        )
        assert result.kind is ReplyKind.NOT_A_PROSPECT
        assert not result.stops_the_sequence
        assert not result.requires_suppression

    def test_matching_ignores_case_and_padding(self) -> None:
        result = classify_reply(
            _msg(from_email="  ArslanVuzmalLone@Gmail.com "), own_addresses=OURS
        )
        assert result.kind is ReplyKind.NOT_A_PROSPECT

    def test_a_lookalike_address_is_not_ours(self) -> None:
        """Substring matching here would silence a real prospect."""
        result = classify_reply(
            _msg(from_email="notarslanvuzmallone@gmail.com"), own_addresses=OURS
        )
        assert result.kind is ReplyKind.HUMAN

    def test_the_helper_is_honest_about_empty_input(self) -> None:
        assert not is_own_address("", OURS)
        assert not is_own_address("owner@practice.example", frozenset())


class TestWarmUpTraffic:
    @pytest.mark.parametrize(
        "body",
        [
            "That book has great insights, Sarah. Let's meet on Tuesday at 10 AM "
            "to discuss how we can apply flame-smile these strategies.",
            "Thank you! model-using I've already marked it on my calendar.",
        ],
    )
    def test_spintax_markers_are_caught(self, body: str) -> None:
        """Both live examples, verbatim."""
        result = classify_reply(_msg(body_text=body))
        assert result.kind is ReplyKind.NOT_A_PROSPECT
        assert any(s.startswith("warmup_marker:") for s in result.signals)

    def test_it_has_no_effects(self) -> None:
        result = classify_reply(_msg(body_text="great flame-smile idea"))
        assert not result.stops_the_sequence
        assert not result.requires_suppression


class TestRealMailIsUntouched:
    """The expensive error would be silencing somebody who did write back."""

    def test_a_human_reply_still_reads_as_human(self) -> None:
        result = classify_reply(
            _msg(body_text="Yes, interested. Can you call me Thursday?"),
            own_addresses=OURS,
        )
        assert result.kind is ReplyKind.HUMAN
        assert result.stops_the_sequence

    def test_an_unsubscribe_still_suppresses(self) -> None:
        result = classify_reply(
            _msg(body_text="Please remove me from your list."), own_addresses=OURS
        )
        assert result.kind is ReplyKind.UNSUBSCRIBE
        assert result.requires_suppression

    def test_a_complaint_still_outranks_everything(self) -> None:
        result = classify_reply(
            _msg(body_text="This is spam, I never signed up."), own_addresses=OURS
        )
        assert result.kind is ReplyKind.COMPLAINT

    def test_an_out_of_office_is_still_an_auto_reply(self) -> None:
        result = classify_reply(
            _msg(body_text="I am currently on annual leave until Wednesday."),
            own_addresses=OURS,
        )
        assert result.kind is ReplyKind.AUTO
        assert not result.stops_the_sequence

    def test_a_bounce_is_still_a_bounce(self) -> None:
        result = classify_reply(
            _msg(
                from_email="MAILER-DAEMON@mx.example",
                subject="Undelivered Mail Returned to Sender",
                body_text="550 5.1.1 User unknown",
            ),
            own_addresses=OURS,
        )
        assert result.kind is ReplyKind.BOUNCE

    def test_no_own_addresses_configured_changes_nothing(self) -> None:
        """The guard must degrade to the old behaviour, not to silence."""
        result = classify_reply(_msg(body_text="Sounds good, send me pricing."))
        assert result.kind is ReplyKind.HUMAN
