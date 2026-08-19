"""Closing a research run that stopped short of analysis.

``analyse_evidence`` writes ``status='completed'`` and was the only writer of
that column. Every other exit from ``LeadResearchWorkflow`` -- a blocked crawl,
no pages captured, a score below threshold, no eligible contact, a rejected or
expired draft, an operator cancellation, an unhandled error -- returned a result
object and wrote nothing at all.

Measured on the live workspace the day this was added: 1,964 runs ``running``,
1,597 of them with no crawl ever started, against 151 ``completed``; and 1,819
``research.failed`` events against 2,005 ``research.started`` in twenty-four
hours, nearly all of them ``browser worker saturated``.

Runs against a real PostgreSQL because the whole behaviour is which rows the
activity is willing to overwrite.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from titan.activities.research import close_research_run
from titan.db.enums import LeadStatus
from titan.db.models import Lead, ResearchRun
from titan.db.session import workspace_unit_of_work
from titan.workflows.types import CloseResearchRunInput, ResearchOutcome

from tests.delivery.conftest import build_sendable

pytestmark = pytest.mark.asyncio


async def _open_run(
    workspace: uuid.UUID, lead_id: uuid.UUID, campaign_id: uuid.UUID
) -> uuid.UUID:
    """A run in the state the workflow leaves it in after ``open_research_run``."""
    async with workspace_unit_of_work(workspace) as session:
        run = ResearchRun(
            lead_id=lead_id,
            campaign_id=campaign_id,
            idempotency_key=f"run-{uuid.uuid4().hex}",
            status="running",
            started_at=dt.datetime.now(dt.UTC),
            workspace_id=workspace,
        )
        session.add(run)
        await session.flush()
        run_id = run.id

        lead = await session.get(Lead, lead_id)
        lead.status = LeadStatus.RESEARCHING
    return run_id


def _request(workspace, run_id, lead_id, outcome, detail="") -> CloseResearchRunInput:
    return CloseResearchRunInput(
        workspace_id=str(workspace),
        research_run_id=str(run_id),
        lead_id=str(lead_id),
        outcome=outcome.value,
        detail=detail,
    )


async def test_a_terminal_outcome_closes_the_run(db_session, workspace):
    fixture = await build_sendable(db_session, workspace)
    run_id = await _open_run(workspace, fixture.lead_id, fixture.campaign_id)

    await close_research_run(
        _request(
            workspace,
            run_id,
            fixture.lead_id,
            ResearchOutcome.BELOW_THRESHOLD,
            "score 41 below threshold 70",
        )
    )

    async with workspace_unit_of_work(workspace) as session:
        run = await session.get(ResearchRun, run_id)
        assert run.status == ResearchOutcome.BELOW_THRESHOLD.value
        assert run.finished_at is not None


async def test_the_lead_leaves_researching(db_session, workspace):
    """The expensive half. ``RESEARCHING`` is not in ``RESEARCHABLE_STATUSES``,
    so a lead left there is never picked up by anything again."""
    fixture = await build_sendable(db_session, workspace)
    run_id = await _open_run(workspace, fixture.lead_id, fixture.campaign_id)

    await close_research_run(
        _request(workspace, run_id, fixture.lead_id, ResearchOutcome.NO_EVIDENCE, "none")
    )

    async with workspace_unit_of_work(workspace) as session:
        lead = await session.get(Lead, fixture.lead_id)
        assert lead.status is LeadStatus.RESEARCHED
        assert lead.status_reason


async def test_a_failure_returns_the_lead_to_the_queue(db_session, workspace):
    """Nobody reached a judgement about this business, so it is owed another
    pass -- the same destination the stale-run sweeper uses, for the same
    reason. Parking it as RESEARCHED would silently discard a lead that the
    browser worker was merely too busy to look at."""
    fixture = await build_sendable(db_session, workspace)
    run_id = await _open_run(workspace, fixture.lead_id, fixture.campaign_id)

    await close_research_run(
        _request(
            workspace,
            run_id,
            fixture.lead_id,
            ResearchOutcome.FAILED,
            "browser worker saturated",
        )
    )

    async with workspace_unit_of_work(workspace) as session:
        lead = await session.get(Lead, fixture.lead_id)
        assert lead.status is LeadStatus.DISCOVERED


async def test_a_second_call_does_not_overwrite_the_first_verdict(db_session, workspace):
    """Idempotent. A workflow replay or an activity retry must not turn a run
    that already recorded why it stopped into a different answer."""
    fixture = await build_sendable(db_session, workspace)
    run_id = await _open_run(workspace, fixture.lead_id, fixture.campaign_id)

    await close_research_run(
        _request(workspace, run_id, fixture.lead_id, ResearchOutcome.BLOCKED, "robots")
    )
    await close_research_run(
        _request(workspace, run_id, fixture.lead_id, ResearchOutcome.FAILED, "later")
    )

    async with workspace_unit_of_work(workspace) as session:
        run = await session.get(ResearchRun, run_id)
        assert run.status == ResearchOutcome.BLOCKED.value


async def test_a_lead_another_writer_already_moved_is_left_alone(db_session, workspace):
    """Planted violation: drop the ``is LeadStatus.RESEARCHING`` guard and this
    fails.

    Scoring, contact resolution and drafting all park the lead themselves and
    know more about why than this does. Overwriting them would make two writers
    of one field and replace the specific reason with a generic one.
    """
    fixture = await build_sendable(db_session, workspace)
    run_id = await _open_run(workspace, fixture.lead_id, fixture.campaign_id)

    async with workspace_unit_of_work(workspace) as session:
        lead = await session.get(Lead, fixture.lead_id)
        lead.status = LeadStatus.MANUAL_REVIEW
        lead.status_reason = "no publishable contact address"

    await close_research_run(
        _request(
            workspace,
            run_id,
            fixture.lead_id,
            ResearchOutcome.NO_ELIGIBLE_CONTACT,
            "no eligible contact",
        )
    )

    async with workspace_unit_of_work(workspace) as session:
        lead = await session.get(Lead, fixture.lead_id)
        assert lead.status is LeadStatus.MANUAL_REVIEW
        assert lead.status_reason == "no publishable contact address"
        # The run is still closed -- that half is unconditional.
        run = await session.get(ResearchRun, run_id)
        assert run.status == ResearchOutcome.NO_ELIGIBLE_CONTACT.value


async def test_a_missing_run_is_not_an_error(db_session, workspace):
    """The activity is best-effort by design: raising here would fail a
    workflow that had already reached its verdict, and the retry would crawl
    the site again."""
    fixture = await build_sendable(db_session, workspace)

    await close_research_run(
        _request(
            workspace, uuid.uuid4(), fixture.lead_id, ResearchOutcome.CANCELLED, "gone"
        )
    )
