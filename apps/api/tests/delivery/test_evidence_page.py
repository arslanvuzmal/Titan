"""The evidence page: signed links, measured findings only, every visit graded."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from titan.api import evidence as evidence_api
from titan.db.enums import Severity, VerificationMethod
from titan.db.models import AuditFinding, FindingEvidence, ResearchRun
from titan.intelligence import evidence_page as ep

from .conftest import NOW, build_sendable, sending_settings

SECRET = "test-evidence-secret"
CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/129.0"


# ------------------------------------------------------------------ tokens
def test_a_token_round_trips() -> None:
    lead = uuid.uuid4()
    assert ep.verify_evidence_token(ep.evidence_token(lead, SECRET), SECRET) == lead


def test_a_forged_or_foreign_token_is_refused() -> None:
    lead = uuid.uuid4()
    token = ep.evidence_token(lead, SECRET)
    assert ep.verify_evidence_token(token, "another-secret") is None
    assert ep.verify_evidence_token(f"{lead}.0000000000000000", SECRET) is None
    assert ep.verify_evidence_token(str(lead), SECRET) is None
    assert ep.verify_evidence_token("not-a-uuid.abc", SECRET) is None


def test_the_url_is_short_and_on_the_given_origin() -> None:
    url = ep.evidence_url("https://titan.arslanvuzmallone.com/", uuid.uuid4(), SECRET)
    assert url.startswith("https://titan.arslanvuzmallone.com/e/")
    assert len(url) < 100


# ------------------------------------------------------------------ rendering
def _page(**overrides) -> ep.EvidencePage:
    finding = ep.PageFinding(
        issue_type="broken_primary_cta",
        title="The main button on your booking page goes nowhere",
        page_url="https://example.com/book",
        observed_value="404",
        business_impact="People ready to book cannot.",
        recommended_solution="Point the button at the booking form.",
        measured_at=NOW,
        excerpts=("<a href='/booking-old'>Book now</a>",),
    )
    base = {
        "workspace_id": uuid.uuid4(),
        "lead_id": uuid.uuid4(),
        "business_name": "Example <Dental>",
        "domain": "example.com",
        "findings": (finding,),
    }
    base.update(overrides)
    return ep.EvidencePage(**base)


def test_everything_from_the_crawl_is_escaped() -> None:
    html = ep.render(
        _page(), token="t.x", owner_name="Arslan", portfolio_url="https://a.com"
    )
    assert "<Dental>" not in html
    assert "Example &lt;Dental&gt;" in html
    assert "<a href='/booking-old'>" not in html


def test_the_page_is_noindex_and_loads_nothing_from_elsewhere() -> None:
    html = ep.render(
        _page(), token="t.x", owner_name="Arslan", portfolio_url="https://a.com"
    )
    assert 'name="robots" content="noindex' in html
    assert "<link" not in html
    assert "src=" not in html


def test_an_empty_page_says_so_rather_than_inventing_something() -> None:
    html = ep.render(
        _page(findings=()),
        token="t.x",
        owner_name="Arslan",
        portfolio_url="https://a.com",
    )
    assert "nothing to show" in html


# ------------------------------------------------------------- the database
async def _with_findings(session, workspace) -> uuid.UUID:
    fixture = await build_sendable(session, workspace, suffix=uuid.uuid4().hex[:6])
    run = ResearchRun(
        workspace_id=workspace,
        lead_id=fixture.lead_id,
        campaign_id=fixture.campaign_id,
        idempotency_key=f"run-{uuid.uuid4().hex[:8]}",
        status="completed",
        started_at=NOW,
        finished_at=NOW,
        pages_crawled=1,
        findings_count=2,
    )
    session.add(run)
    await session.flush()

    def finding(issue: str, method: VerificationMethod, contradicted: bool = False):
        row = AuditFinding(
            workspace_id=workspace,
            research_run_id=run.id,
            lead_id=fixture.lead_id,
            category="conversion",
            issue_type=issue,
            title=f"title {issue}",
            page_url="https://fixture.test/book",
            severity=Severity.HIGH,
            confidence=0.95,
            verification_method=method,
            contradicted=contradicted,
            finding_fingerprint=f"{issue}:{uuid.uuid4().hex}",
        )
        session.add(row)
        return row

    measured = finding("broken_primary_cta", VerificationMethod.DOM_ASSERTION)
    guessed = finding("slow_page", VerificationMethod.MODEL_INFERENCE)
    withdrawn = finding("no_visible_phone_number", VerificationMethod.DOM_ASSERTION, True)
    await session.flush()
    for row in (measured, guessed, withdrawn):
        session.add(
            FindingEvidence(
                workspace_id=workspace,
                finding_id=row.id,
                excerpt="the button returns 404",
                excerpt_fingerprint=hashlib.sha256(row.issue_type.encode()).hexdigest(),
                source_url="https://fixture.test/book",
                captured_at=NOW,
            )
        )
    await session.commit()
    return fixture.lead_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_only_measured_uncontradicted_findings_are_shown(
    db_session, workspace
) -> None:
    lead_id = await _with_findings(db_session, workspace)
    page = await ep.load_page(db_session, lead_id=lead_id)
    assert page is not None
    assert [f.issue_type for f in page.findings] == ["broken_primary_cta"]
    assert page.findings[0].excerpts == ("the button returns 404",)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_missing_lead_is_none(db_session) -> None:
    assert await ep.load_page(db_session, lead_id=uuid.uuid4()) is None


# ------------------------------------------------------------------ the route
@pytest.fixture
def routed(monkeypatch):
    monkeypatch.setattr(
        evidence_api, "get_settings", lambda: sending_settings(evidence_secret=SECRET)
    )
    from titan.api.main import app

    return TestClient(app)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_visit_is_served_and_graded(db_session, workspace, routed) -> None:
    lead_id = await _with_findings(db_session, workspace)
    token = ep.evidence_token(lead_id, SECRET)

    r = routed.get(
        f"/e/{token}", headers={"user-agent": CHROME, "x-real-ip": "81.2.69.1"}
    )
    assert r.status_code == 200
    assert "title broken_primary_cta" in r.text
    assert r.headers["x-robots-tag"].startswith("noindex")

    r = routed.post(f"/e/{token}/seen", content=b'{"dwell":5,"scroll":80}')
    assert r.status_code == 204

    rows = (
        await db_session.execute(
            text(
                "SELECT kind, grade FROM engagement_events "
                "WHERE workspace_id = :ws AND lead_id = :lead ORDER BY occurred_at"
            ),
            {"ws": workspace, "lead": lead_id},
        )
    ).all()
    assert [(r.kind, r.grade) for r in rows] == [
        ("visit", "likely"),
        ("visit_confirmed", "confirmed"),
    ]


def test_a_forged_token_gets_the_same_page_as_a_missing_lead(routed) -> None:
    forged = routed.get(f"/e/{uuid.uuid4()}.0000000000000000")
    assert forged.status_code == 404
    assert "not available" in forged.text


def test_the_feature_off_serves_nothing(monkeypatch) -> None:
    monkeypatch.setattr(evidence_api, "get_settings", lambda: sending_settings())
    from titan.api.main import app

    token = ep.evidence_token(uuid.uuid4(), SECRET)
    assert TestClient(app).get(f"/e/{token}").status_code == 404


def test_a_dated_measurement_shows_its_date() -> None:
    page = _page()
    html = ep.render(page, token="t.x", owner_name="A", portfolio_url="https://a.com")
    assert f"{dt.datetime(2026, 8, 3):%d %B %Y}" in html


# ------------------------------------------------------------------ the pixel
@pytest.mark.integration
@pytest.mark.asyncio
async def test_apples_proxy_records_delivery_and_does_not_stamp_an_open(
    db_session, workspace, monkeypatch
) -> None:
    from titan.api import placement as placement_api
    from titan.api.main import app
    from titan.delivery.open_tracking import open_token

    fixture = await build_sendable(db_session, workspace, suffix=uuid.uuid4().hex[:6])
    await db_session.execute(
        text("UPDATE messages SET sent_at = now() - interval '3 hours' WHERE id = :id"),
        {"id": fixture.message_id},
    )
    await db_session.commit()
    monkeypatch.setattr(
        placement_api,
        "get_settings",
        lambda: sending_settings(open_tracking_secret=SECRET),
    )
    client = TestClient(app)
    token = open_token(fixture.message_id, SECRET)

    r = client.get(
        f"/o/{token}.gif",
        headers={"user-agent": "Mozilla/5.0 (iPhone)", "x-real-ip": "17.5.6.7"},
    )
    assert r.status_code == 200
    opened = await db_session.scalar(
        text("SELECT first_opened_at FROM messages WHERE id = :id"),
        {"id": fixture.message_id},
    )
    assert opened is None, "Apple's proxy loads every image; it is not a reading"

    client.get(
        f"/o/{token}.gif",
        headers={
            "user-agent": "Mozilla/5.0 (via ggpht.com GoogleImageProxy)",
            "x-real-ip": "66.249.84.1",
        },
    )
    db_session.expire_all()
    opened = await db_session.scalar(
        text("SELECT first_opened_at FROM messages WHERE id = :id"),
        {"id": fixture.message_id},
    )
    assert opened is not None
    grades = (
        (
            await db_session.execute(
                text(
                    "SELECT grade FROM engagement_events WHERE workspace_id = :ws "
                    "AND message_id = :id ORDER BY occurred_at"
                ),
                {"ws": workspace, "id": fixture.message_id},
            )
        )
        .scalars()
        .all()
    )
    assert grades == ["delivered", "likely"]
