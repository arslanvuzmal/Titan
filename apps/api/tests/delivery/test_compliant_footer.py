"""The postal address is repaired against the mailbox that actually sends.

The composer writes the footer when the draft is made, from the sender identity
the campaign names. The sender *pool* then picks a mailbox at send time by
remaining headroom, and an address filled in on the identities afterwards never
reaches a body already composed. Either way the send-time check finds an
address configured and missing from the text, and blocks the message
permanently -- a body does not change on its own.

That cost 101 validated, approved messages on the live workspace, plus 28 more
on the unsubscribe header, over a setting being filled in late.

What these tests hold in place is the shape of the fix: the *requirement* is
untouched -- a message with no address configured anywhere is still refused --
and only the *drift* is closed.
"""

from __future__ import annotations

from titan.delivery.outbox_worker import (
    with_compliant_footer,
    with_one_click_unsubscribe,
)
from titan.delivery.providers.base import OutboundEmail

ADDRESS = "Clifton Business District, DHA, Karachi"


def _email(text_body: str, html_body: str | None = None) -> OutboundEmail:
    return OutboundEmail(
        to_email="owner@practice.example",
        from_email="sales@arslanvuzmallone.com",
        from_name="Arslan",
        reply_to="sales@arslanvuzmallone.com",
        subject="Your booking page",
        text_body=text_body,
        html_body=html_body,
    )


class TestRepair:
    def test_a_missing_address_is_appended(self) -> None:
        email = with_compliant_footer(
            _email("Hi there,\n\nYour booking page 404s.\n"), ADDRESS
        )
        assert ADDRESS in email.text_body

    def test_an_address_already_present_is_left_alone(self) -> None:
        """No duplicate footer on the messages that were composed correctly."""
        body = f"Hi there,\n\nYour booking page 404s.\n\nArslan\n{ADDRESS}\n"
        email = with_compliant_footer(_email(body), ADDRESS)
        assert email.text_body == body
        assert email.text_body.count(ADDRESS) == 1

    def test_the_html_part_is_repaired_too(self) -> None:
        html = '<div style="font-size:15px;">\n    <p>Hi there,</p>\n</div>'
        email = with_compliant_footer(_email("Hi there,\n", html), ADDRESS)
        assert email.html_body is not None
        assert ADDRESS in email.html_body
        # Repaired inside the wrapper, not tacked on after it.
        assert email.html_body.rstrip().endswith("</div>")

    def test_html_is_escaped(self) -> None:
        email = with_compliant_footer(
            _email("Hi there,\n", "<div><p>Hi</p></div>"), "A & B Ltd, <Karachi>"
        )
        assert email.html_body is not None
        assert "&amp;" in email.html_body
        assert "<Karachi>" not in email.html_body

    def test_a_message_with_no_html_part_stays_text_only(self) -> None:
        email = with_compliant_footer(_email("Hi there,\n"), ADDRESS)
        assert email.html_body is None


class TestTheRequirementIsUntouched:
    """The repair closes the drift. It must not weaken the rule."""

    def test_no_configured_address_means_no_repair(self) -> None:
        """The send gate must still refuse -- there is nothing to write."""
        body = "Hi there,\n\nYour booking page 404s.\n"
        assert with_compliant_footer(_email(body), None).text_body == body
        assert with_compliant_footer(_email(body), "   ").text_body == body

    def test_nothing_else_about_the_message_changes(self) -> None:
        original = _email("Hi there,\n")
        repaired = with_compliant_footer(original, ADDRESS)
        assert repaired.to_email == original.to_email
        assert repaired.from_email == original.from_email
        assert repaired.subject == original.subject
        assert repaired.idempotency_key == original.idempotency_key


class TestOneClickUnsubscribe:
    """The other half of the same drift, and 28 more cancelled messages."""

    def _with(self, lu: str | None, post: str | None = None) -> OutboundEmail:
        return OutboundEmail(
            to_email="owner@practice.example",
            from_email="sales@arslanvuzmallone.com",
            from_name="Arslan",
            reply_to="sales@arslanvuzmallone.com",
            subject="Your booking page",
            text_body="Hi there,\n",
            list_unsubscribe=lu,
            list_unsubscribe_post=post,
        )

    def test_an_https_target_gains_the_rfc_8058_header(self) -> None:
        email = with_one_click_unsubscribe(
            self._with("<https://arslanvuzmallone.com/u/abc>")
        )
        assert email.list_unsubscribe_post == "List-Unsubscribe=One-Click"

    def test_a_mailto_only_target_does_not(self) -> None:
        """One-click is meaningless beside a bare mailto:."""
        email = self._with("<mailto:unsubscribe@arslanvuzmallone.com>")
        assert with_one_click_unsubscribe(email).list_unsubscribe_post is None

    def test_no_unsubscribe_target_means_nothing_is_invented(self) -> None:
        assert with_one_click_unsubscribe(self._with(None)).list_unsubscribe_post is None

    def test_an_existing_header_is_never_overwritten(self) -> None:
        email = self._with("<https://x.example/u/1>", "List-Unsubscribe=One-Click")
        assert (
            with_one_click_unsubscribe(email).list_unsubscribe_post
            == "List-Unsubscribe=One-Click"
        )
