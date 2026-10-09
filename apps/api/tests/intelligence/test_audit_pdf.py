"""The personal PDF: what it says, what it is called, and when an email carries it."""

from __future__ import annotations

import datetime as dt
import re
import tempfile
import uuid

import pytest
from coldops.db.enums import MessageState
from coldops.db.models import Message
from coldops.delivery.providers.mock import MockEmailProvider
from coldops.intelligence import audit_pdf
from coldops.intelligence.case_studies import CaseStudy
from coldops.intelligence.evidence_page import EvidencePage, PageFinding
from sqlalchemy import update

from tests.delivery.conftest import NOW, build_sendable
from tests.delivery.test_outbox_delivery import worker

SHOT = "shots/ab/" + "ab" * 32 + ".jpg"


def _finding(**overrides) -> PageFinding:
    base = dict(
        issue_type="no_booking_or_enquiry_path",
        title="There is no way to book or enquire online",
        page_url="https://smile.example/",
        observed_value="No booking link, form or enquiry button on the homepage.",
        business_impact="A visitor who wants an appointment has to phone during opening hours.",
        recommended_solution="Add a booking button that opens a short form.",
        measured_at=dt.datetime(2026, 10, 2, tzinfo=dt.UTC),
    )
    base.update(overrides)
    return PageFinding(**base)


def _page(*findings: PageFinding, shots: bool = True) -> EvidencePage:
    return EvidencePage(
        workspace_id=uuid.uuid4(),
        lead_id=uuid.uuid4(),
        business_name="Smile <Dental> & Co",
        domain="smile.example",
        findings=findings or (_finding(),),
        shots=({"desktop": (SHOT, NOW), "mobile": (SHOT, NOW)} if shots else {}),
    )


# ---------------------------------------------------------------- content


def test_the_document_names_the_business_and_escapes_it():
    html = audit_pdf.build_html(
        _page(),
        finding=_finding(),
        case_study=None,
        evidence_url=None,
        sender_name="Arslan",
        sender_site="https://arslanvuzmallone.com",
    )
    assert "Smile &lt;Dental&gt; &amp; Co" in html
    assert "<Dental>" not in html


def test_every_sentence_about_them_is_the_finding_itself():
    finding = _finding()
    html = audit_pdf.build_html(
        _page(finding),
        finding=finding,
        case_study=None,
        evidence_url=None,
        sender_name="Arslan",
        sender_site=None,
    )
    for field in (
        finding.title,
        finding.observed_value,
        finding.business_impact,
        finding.recommended_solution,
    ):
        assert field in html


def test_images_are_only_ever_screenshot_keys():
    """Nothing the page could fetch: the worker refuses the network, and this never asks."""
    html = audit_pdf.build_html(
        _page(),
        finding=_finding(),
        case_study=None,
        evidence_url="https://titan.example/e/token",
        sender_name="A",
        sender_site=None,
    )
    sources = re.findall(r'src="([^"]*)"', html)
    assert sources == [f"artifact:{SHOT}", f"artifact:{SHOT}"]
    assert "<script" not in html.lower()
    assert "<link" not in html.lower()


def test_no_screenshots_still_makes_a_document():
    html = audit_pdf.build_html(
        _page(shots=False),
        finding=_finding(),
        case_study=None,
        evidence_url=None,
        sender_name="A",
        sender_site=None,
    )
    assert "artifact:" not in html
    assert "What I found" in html


def test_the_case_study_is_the_registry_sentence():
    study = CaseStudy(
        reference="vox",
        name="VoxCircuit",
        summary="an AI receptionist that books appointments",
        url="https://voxcircuit.example",
    )
    html = audit_pdf.build_html(
        _page(),
        finding=_finding(),
        case_study=study,
        evidence_url=None,
        sender_name="A",
        sender_site=None,
    )
    assert "I built VoxCircuit, an AI receptionist that books appointments." in html


def test_the_pdf_follows_the_finding_the_email_led_with():
    speed = _finding(issue_type="slow_largest_contentful_paint", title="Slow")
    booking = _finding()
    page = _page(booking, speed)
    assert audit_pdf.choose_finding(page, ["slow_largest_contentful_paint"]) is speed
    assert audit_pdf.choose_finding(page, ["not_on_the_page"]) is booking
    assert (
        audit_pdf.choose_finding(
            _page(shots=False).__class__(
                workspace_id=uuid.uuid4(),
                lead_id=uuid.uuid4(),
                business_name="x",
                domain=None,
            ),
            [],
        )
        is None
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Smile Dental", "Smile Dental - website check.pdf"),
        ("Café Dentaire 😁", "Caf Dentaire - website check.pdf"),
        ("../../etc/passwd", "etc passwd - website check.pdf"),
        ("", "Website check.pdf"),
        (None, "Website check.pdf"),
    ],
)
def test_the_filename_is_plain(name, expected):
    assert audit_pdf.attachment_filename(name) == expected


def test_the_note_names_the_site():
    assert "smile.example" in audit_pdf.attachment_note("smile.example")
    assert "your website" in audit_pdf.attachment_note(None)


# ---------------------------------------------------------------- sending


def _pdf_dir(draft_id: uuid.UUID, size: int = 4096) -> str:
    root = tempfile.mkdtemp(prefix="coldops-pdf-")
    path = audit_pdf.pdf_path(root, draft_id)
    assert path is not None
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF-1.4\n" + b"x" * size)
    return root


@pytest.mark.asyncio
async def test_a_first_email_carries_its_own_pdf(db_session, workspace):
    fx = await build_sendable(db_session, workspace)
    provider = MockEmailProvider()
    root = _pdf_dir(fx.draft_id)

    results = await worker(provider, audit_pdf_enabled=True, artifact_dir=root).run_once()

    assert [r.outcome for r in results] == ["sent"], results
    email = provider.sends[0].email
    assert len(email.attachments) == 1
    assert email.attachments[0].filename.endswith(" - website check.pdf")
    assert "I have attached a one-page check of" in email.text_body


@pytest.mark.asyncio
async def test_a_missing_pdf_means_no_attachment_and_no_claim(db_session, workspace):
    await build_sendable(db_session, workspace)
    provider = MockEmailProvider()
    root = tempfile.mkdtemp(prefix="coldops-pdf-")

    await worker(provider, audit_pdf_enabled=True, artifact_dir=root).run_once()

    email = provider.sends[0].email
    assert email.attachments == ()
    assert "attached" not in email.text_body


@pytest.mark.asyncio
async def test_a_pdf_over_the_limit_is_not_attached(db_session, workspace):
    fx = await build_sendable(db_session, workspace)
    provider = MockEmailProvider()
    root = _pdf_dir(fx.draft_id, size=audit_pdf.MAX_PDF_BYTES + 1)

    await worker(provider, audit_pdf_enabled=True, artifact_dir=root).run_once()

    assert provider.sends[0].email.attachments == ()


@pytest.mark.asyncio
async def test_a_lead_already_written_to_gets_no_pdf(db_session, workspace):
    fx = await build_sendable(db_session, workspace)
    earlier = await build_sendable(db_session, workspace, suffix="earlier")
    # The same lead, already contacted once.
    await db_session.execute(
        update(Message)
        .where(Message.id == earlier.message_id)
        .values(
            lead_id=fx.lead_id,
            state=MessageState.SENT,
            sent_at=NOW - dt.timedelta(days=3),
        )
    )
    await db_session.commit()
    provider = MockEmailProvider()
    root = _pdf_dir(fx.draft_id)

    await worker(provider, audit_pdf_enabled=True, artifact_dir=root).run_once()

    sent_to_lead = [s.email for s in provider.sends if s.email.to_email == fx.to_email]
    assert sent_to_lead and sent_to_lead[0].attachments == ()
