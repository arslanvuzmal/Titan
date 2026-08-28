"""The one-page brief, attached — and bounded.

I argued against attaching this and was overruled, which is the operator's call.
So it is built properly, and the concern is answered where it can be: in a gate
that refuses the shapes which actually trigger filtering, rather than in a
refusal to implement.

The load-bearing property is that the body and the attachment cannot disagree.
The words are written at compose time and the file is read days later at send
time, so a message saying "attached" while carrying nothing is a real failure
mode with two separate causes -- a missing file, an unset path. ``with_one_pager``
does both or neither, which makes that state unreachable rather than unlikely.
"""

from __future__ import annotations

import pathlib

import pytest
from titan.delivery.deliverability import (
    ALLOWED_ATTACHMENT_TYPES,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    Severity,
    check_attachments,
)
from titan.delivery.outbox_worker import ONE_PAGER_NOTE, with_one_pager
from titan.delivery.providers.base import Attachment, OutboundEmail
from titan.delivery.providers.smtp import SmtpProvider

BODY = (
    "Hi Sarah,\n\n"
    "I came across example.com and noticed the button returns 404.\n\n"
    "Happy to put twenty minutes in the diary.\n\n"
    "Arslan Vuzmal\n"
    "1 Some Street, Manchester M1 1AA\n\n"
    "References\n"
    "- Page examined: https://example.com/book\n\n"
    "If you would rather not hear from me again, reply to this message.\n"
)
HTML = (
    '<div><p style="margin:0 0 16px;">Hi Sarah,</p>'
    '<p style="margin:0 0 16px;">Body.</p>'
    '<p style="margin:24px 0 0;color:#444;">Arslan Vuzmal</p></div>'
)


def _email(text: str = BODY, html: str | None = HTML) -> OutboundEmail:
    return OutboundEmail(
        to_email="owner@practice.example",
        from_email="outreach@arslanvuzmallone.com",
        from_name="Arslan Vuzmal",
        reply_to="outreach@arslanvuzmallone.com",
        subject="Quick note about your booking page",
        text_body=text,
        html_body=html,
    )


@pytest.fixture
def brief(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "one-pager.pdf"
    path.write_bytes(b"%PDF-1.4\n" + b"x" * 2048)
    return path


class TestTheBodyAndTheAttachmentCannotDisagree:
    def test_attaching_also_says_so(self, brief: pathlib.Path) -> None:
        result = with_one_pager(_email(), str(brief))
        assert len(result.attachments) == 1
        assert ONE_PAGER_NOTE in result.text_body

    def test_no_file_means_no_claim(self) -> None:
        """The failure this design exists to make unreachable."""
        original = _email()
        assert with_one_pager(original, "/does/not/exist.pdf") is original

    def test_an_unset_path_attaches_nothing_and_claims_nothing(self) -> None:
        original = _email()
        assert with_one_pager(original, None) is original
        assert ONE_PAGER_NOTE not in original.text_body

    def test_an_empty_file_is_refused_rather_than_announced(
        self, tmp_path: pathlib.Path
    ) -> None:
        empty = tmp_path / "empty.pdf"
        empty.write_bytes(b"")
        original = _email()
        assert with_one_pager(original, str(empty)) is original

    def test_the_html_part_says_it_too(self, brief: pathlib.Path) -> None:
        """Two parts of one message disagreeing is its own failure."""
        result = with_one_pager(_email(), str(brief))
        assert ONE_PAGER_NOTE in (result.html_body or "")

    def test_an_html_part_it_cannot_place_the_note_in_still_attaches(
        self, brief: pathlib.Path
    ) -> None:
        result = with_one_pager(_email(html="<div><p>Other markup.</p></div>"), str(brief))
        assert len(result.attachments) == 1
        assert ONE_PAGER_NOTE in result.text_body


class TestWhereTheNoteGoes:
    def test_it_sits_above_the_signature(self, brief: pathlib.Path) -> None:
        """End of the pitch, where the offer of more belongs. Below the
        signature it reads as a footer artefact."""
        body = with_one_pager(_email(), str(brief)).text_body
        assert body.index(ONE_PAGER_NOTE) < body.index("\nArslan Vuzmal")

    def test_the_footer_survives_intact(self, brief: pathlib.Path) -> None:
        body = with_one_pager(_email(), str(brief)).text_body
        assert "1 Some Street, Manchester M1 1AA" in body
        assert "References" in body
        assert "reply to this message" in body

    def test_the_pitch_is_not_otherwise_rewritten(self, brief: pathlib.Path) -> None:
        body = with_one_pager(_email(), str(brief)).text_body
        assert "I came across example.com and noticed the button returns 404." in body


class TestTheGate:
    """What may leave with a cold message, and what may not."""

    @staticmethod
    def _pdf(size: int = 2048) -> Attachment:
        return Attachment(filename="brief.pdf", content=b"%PDF-1.4\n" + b"x" * size)

    def test_a_real_pdf_is_accepted(self) -> None:
        assert check_attachments([self._pdf()]) == []

    def test_no_attachment_is_accepted(self) -> None:
        assert check_attachments([]) == []

    def test_two_documents_are_refused(self) -> None:
        signals = check_attachments([self._pdf(), self._pdf()])
        assert [s.code for s in signals] == ["too_many_attachments"]
        assert MAX_ATTACHMENTS == 1

    def test_an_oversize_brief_is_refused(self) -> None:
        big = Attachment(
            filename="big.pdf", content=b"%PDF-1.4" + b"x" * (MAX_ATTACHMENT_BYTES + 1)
        )
        assert "attachment_too_large" in [s.code for s in check_attachments([big])]

    def test_a_non_pdf_is_refused(self) -> None:
        doc = Attachment(
            filename="deck.pptx",
            content=b"PK\x03\x04",
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.presentationml",
        )
        assert "attachment_type_not_allowed" in [s.code for s in check_attachments([doc])]

    @pytest.mark.parametrize(
        ("magic", "what"),
        [
            (b"MZ\x90\x00", "a Windows executable"),
            (b"\x7fELF\x02", "a Linux executable"),
            (b"PK\x03\x04zz", "a zip archive"),
            (b"#!/bin/sh\n", "a script"),
        ],
    )
    def test_executable_content_is_refused_whatever_it_is_named(
        self, magic: bytes, what: str
    ) -> None:
        """Checked on the bytes: the filename is the part an attacker gets free."""
        disguised = Attachment(filename="one-pager.pdf", content=magic + b"rest")
        codes = [s.code for s in check_attachments([disguised])]
        assert "attachment_is_executable" in codes, what

    def test_a_mislabelled_pdf_is_refused(self) -> None:
        not_really = Attachment(filename="brief.pdf", content=b"just some text")
        assert "attachment_not_a_pdf" in [s.code for s in check_attachments([not_really])]

    def test_an_empty_attachment_is_refused(self) -> None:
        assert "attachment_empty" in [
            s.code for s in check_attachments([Attachment(filename="x.pdf", content=b"")])
        ]

    def test_every_signal_blocks(self) -> None:
        """None of these are advisory. An attachment that trips one is the kind
        that gets a sending domain filtered."""
        bad = Attachment(filename="x.exe", content=b"MZ", subtype="octet-stream")
        signals = check_attachments([bad, bad])
        assert signals
        assert all(s.severity is Severity.BLOCK for s in signals)

    def test_only_pdf_is_allowed(self) -> None:
        assert ALLOWED_ATTACHMENT_TYPES == frozenset({("application", "pdf")})


class TestItReachesTheWire:
    def test_the_smtp_message_carries_the_document(self, brief: pathlib.Path) -> None:
        """A gate that passes and a provider that drops it would be worse than
        not building this at all."""
        email = with_one_pager(_email(), str(brief))
        provider = SmtpProvider.__new__(SmtpProvider)
        provider._message_id_domain = "arslanvuzmallone.com"  # noqa: SLF001
        message = provider._build(email)  # noqa: SLF001

        attachments = [
            part
            for part in message.walk()
            if part.get_content_disposition() == "attachment"
        ]
        assert len(attachments) == 1
        assert attachments[0].get_filename() == "one-pager.pdf"
        assert attachments[0].get_content_type() == "application/pdf"

    def test_both_body_parts_survive_the_attachment(self, brief: pathlib.Path) -> None:
        """add_attachment promotes the message to multipart/mixed. A client that
        falls back to plain text because the HTML alternative was displaced is a
        silent regression."""
        email = with_one_pager(_email(), str(brief))
        provider = SmtpProvider.__new__(SmtpProvider)
        provider._message_id_domain = "arslanvuzmallone.com"  # noqa: SLF001
        message = provider._build(email)  # noqa: SLF001

        types = {
            part.get_content_type()
            for part in message.walk()
            if part.get_content_disposition() != "attachment"
        }
        assert "text/plain" in types
        assert "text/html" in types

    def test_a_message_without_one_is_unchanged(self) -> None:
        provider = SmtpProvider.__new__(SmtpProvider)
        provider._message_id_domain = "arslanvuzmallone.com"  # noqa: SLF001
        message = provider._build(_email())  # noqa: SLF001
        assert not [
            part
            for part in message.walk()
            if part.get_content_disposition() == "attachment"
        ]


class TestTheShippedDocument:
    """The real file, if it has been generated."""

    PATH = pathlib.Path(__file__).resolve().parents[4] / "docs/one-pager/one-pager.pdf"

    @pytest.mark.skipif(
        not PATH.is_file(), reason="one-pager.pdf not generated in this checkout"
    )
    def test_it_passes_its_own_gate(self) -> None:
        content = self.PATH.read_bytes()
        assert check_attachments(
            [Attachment(filename=self.PATH.name, content=content)]
        ) == []

    @pytest.mark.skipif(not PATH.is_file(), reason="not generated")
    def test_it_is_one_page(self) -> None:
        """A two-page one-pager is a different document, and finding that out
        from a recipient is the wrong way to find out."""
        import re

        content = self.PATH.read_bytes()
        assert len(re.findall(rb"/Type\s*/Page[^s]", content)) == 1
