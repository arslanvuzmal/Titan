"""A 5xx about *us* must not retire the business it was aimed at.

:mod:`titan.intelligence.smtp_probe` learned this on its first live run. Three
real addresses came back INVALID, and all three were refusals of *our* IP -- no
PTR record, a Barracuda listing -- which apply to every recipient on the
connection and say nothing about any mailbox. It grew
``SENDER_REJECTION_MARKERS`` and stopped believing them.

The delivery adapter never inherited that. ``554`` sat in
``PERMANENT_REPLY_CODES`` as "this recipient will never accept mail", and
Titan's own SMTP host answers a burst with::

    554 5.7.1 <DATA>: Data command rejected:
        Reject: too many messages from sender in last 60 minutes

So every message caught by that throttle was recorded as an invalid recipient
and the practice suppressed for good. Thirty of them on the live workspace --
correctly addressed businesses, retired because we sent too fast.

Both directions matter here, which is why the second class exists. Reading a
sender fault as a dead mailbox loses a lead permanently; reading a genuinely
dead mailbox as a sender fault means retrying a hard bounce, which is the thing
that damages a sending domain. The rule has to be sharp in both directions.
"""

from __future__ import annotations

import smtplib

import pytest
from titan.delivery.providers.base import PERMANENT_ERROR_KINDS, SendErrorKind
from titan.delivery.providers.smtp import SmtpProvider
from titan.intelligence.smtp_probe import is_sender_rejection


def _provider(monkeypatch: pytest.MonkeyPatch, code: int, text: str) -> SmtpProvider:
    provider = SmtpProvider(host="smtp.example", port=465)

    def refuse(email: object) -> tuple[bool, str | None, int | None]:
        exc = smtplib.SMTPResponseException(code, text)
        return False, f"{exc.smtp_code} {exc.smtp_error!r}", int(exc.smtp_code)

    monkeypatch.setattr(provider, "_send_blocking", refuse)
    return provider


def _email():
    from titan.delivery.providers.base import OutboundEmail

    return OutboundEmail(
        to_email="reception@realpractice.example",
        from_email="sales@arslanvuzmallone.com",
        from_name="Arslan",
        reply_to="sales@arslanvuzmallone.com",
        subject="Your booking page",
        text_body="Hi there,\n",
    )


class TestOurFaultDoesNotSuppress:
    @pytest.mark.parametrize(
        "code, text",
        [
            # The exact reply from Titan's own host, 30 times on the live
            # workspace.
            (
                554,
                "5.7.1 <DATA>: Data command rejected: Reject: "
                "too many messages from sender in last 60 minutes",
            ),
            (550, "JunkMail rejected - is in an RBL: Reverse DNS (PTR) missing"),
            (550, "http://www.barracudanetworks.com/reputation/?pr=1&ip=1.2.3.4"),
            (554, "5.7.1 Service unavailable; client host blocked using Spamhaus"),
            (553, "Access denied: SPF check failed for your domain"),
        ],
    )
    @pytest.mark.asyncio
    async def test_it_is_rate_limited_not_an_invalid_recipient(
        self, monkeypatch: pytest.MonkeyPatch, code: int, text: str
    ) -> None:
        result = await _provider(monkeypatch, code, text).send(_email())
        assert not result.accepted
        assert result.error_kind is SendErrorKind.RATE_LIMITED
        assert result.error_kind not in PERMANENT_ERROR_KINDS, (
            "a refusal of our own sender must never suppress the recipient"
        )


class TestTheirFaultStillSuppresses:
    """The fix must not turn every hard bounce into an endless retry."""

    @pytest.mark.parametrize(
        "code, text",
        [
            (
                550,
                "5.1.1 <reception@realpractice.example>: Recipient address rejected: User unknown",
            ),
            (550, "No such user here"),
            (551, "5.1.1 User not local; please try forwarding"),
            (553, "5.1.3 Invalid mailbox syntax"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_dead_mailbox_is_still_permanent(
        self, monkeypatch: pytest.MonkeyPatch, code: int, text: str
    ) -> None:
        result = await _provider(monkeypatch, code, text).send(_email())
        assert not result.accepted
        assert result.error_kind is SendErrorKind.INVALID_RECIPIENT
        assert result.error_kind in PERMANENT_ERROR_KINDS


class TestOneRuleForBothCallers:
    """Delivery and verification must not grow separate opinions about this."""

    def test_the_throttle_reply_reads_as_a_sender_rejection(self) -> None:
        assert is_sender_rejection(
            "554 5.7.1 Reject: too many messages from sender in last 60 minutes"
        )

    def test_a_user_unknown_reply_does_not(self) -> None:
        assert not is_sender_rejection(
            "550 5.1.1 Recipient address rejected: User unknown"
        )
