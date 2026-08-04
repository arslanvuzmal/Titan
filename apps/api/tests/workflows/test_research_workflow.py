"""Workflow tests against a real Temporal test environment.

These use ``WorkflowEnvironment.start_time_skipping()``, so a seven-day approval
timeout is exercised in milliseconds rather than skipped as untestable.

Activities are stubbed with recording fakes. The point is to prove the
*workflow body* behaves -- that it is deterministic, that it stops where it
should, that a retry does not duplicate an event, and that the approval wait has
a real deadline. The activities themselves are covered separately.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from titan.workflows.research import (
    LeadResearchWorkflow,
    research_workflow_id,
)
from titan.workflows.types import (
    AnalyseActivityInput,
    AnalyseActivityResult,
    ApprovalDecisionSignal,
    ContactActivityInput,
    ContactActivityResult,
    CrawlActivityInput,
    CrawlActivityResult,
    DraftActivityInput,
    DraftActivityResult,
    QueueActivityInput,
    QueueActivityResult,
    RecordEventInput,
    ResearchLeadInput,
    ResearchOutcome,
    ScoreActivityInput,
    ScoreActivityResult,
)

#: A test workflow is time-skipped, so anything past this is a stuck run
#: rather than a slow one.
WORKFLOW_TIMEOUT = 60.0

TASK_QUEUE = "titan-research-test"


@dataclass
class Recorder:
    """Configurable activity doubles that record what the workflow asked for."""

    crawl: CrawlActivityResult = field(
        default_factory=lambda: CrawlActivityResult(
            crawl_run_id="crawl-1", status="completed", pages_captured=5
        )
    )
    analysis: AnalyseActivityResult = field(
        default_factory=lambda: AnalyseActivityResult(
            findings_created=4, pitchable_findings=3, top_issue_type="broken_primary_cta"
        )
    )
    score: ScoreActivityResult = field(
        default_factory=lambda: ScoreActivityResult(
            total=88, band="high_priority", passed_threshold=True, threshold=70
        )
    )
    contact: ContactActivityResult = field(
        default_factory=lambda: ContactActivityResult(eligible_channel_id="chan-1")
    )
    draft: DraftActivityResult = field(
        default_factory=lambda: DraftActivityResult(
            draft_id="draft-1", validation_passed=True
        )
    )
    queued: QueueActivityResult = field(
        default_factory=lambda: QueueActivityResult(outbox_id="outbox-1", queued=True)
    )
    needs_approval: bool = False

    #: Every event key the workflow emitted, including duplicates.
    events: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    #: Fail the crawl activity this many times before succeeding.
    crawl_failures: int = 0

    def activities(self) -> list:
        recorder = self

        @activity.defn(name="open_research_run")
        async def open_research_run(request: ResearchLeadInput) -> str:
            recorder.calls.append("open_research_run")
            return "run-1"

        @activity.defn(name="crawl_lead_website")
        async def crawl_lead_website(request: CrawlActivityInput) -> CrawlActivityResult:
            recorder.calls.append("crawl_lead_website")
            if recorder.crawl_failures > 0:
                recorder.crawl_failures -= 1
                raise RuntimeError("transient browser worker failure")
            return recorder.crawl

        @activity.defn(name="analyse_evidence")
        async def analyse_evidence(
            request: AnalyseActivityInput,
        ) -> AnalyseActivityResult:
            recorder.calls.append("analyse_evidence")
            return recorder.analysis

        @activity.defn(name="score_lead")
        async def score_lead(request: ScoreActivityInput) -> ScoreActivityResult:
            recorder.calls.append("score_lead")
            return recorder.score

        @activity.defn(name="resolve_contact")
        async def resolve_contact(request: ContactActivityInput) -> ContactActivityResult:
            recorder.calls.append("resolve_contact")
            return recorder.contact

        @activity.defn(name="generate_draft")
        async def generate_draft(request: DraftActivityInput) -> DraftActivityResult:
            recorder.calls.append("generate_draft")
            return recorder.draft

        @activity.defn(name="requires_human_approval")
        async def requires_human_approval(request: ResearchLeadInput) -> bool:
            recorder.calls.append("requires_human_approval")
            return recorder.needs_approval

        @activity.defn(name="queue_message")
        async def queue_message(request: QueueActivityInput) -> QueueActivityResult:
            recorder.calls.append("queue_message")
            return recorder.queued

        @activity.defn(name="record_workflow_event")
        async def record_workflow_event(request: RecordEventInput) -> None:
            # Records the KEY, so a duplicate emission is visible as a repeated
            # entry -- which is exactly what the unique constraint would collapse.
            recorder.events.append(request.event_key)

        return [
            open_research_run,
            crawl_lead_website,
            analyse_evidence,
            score_lead,
            resolve_contact,
            generate_draft,
            requires_human_approval,
            queue_message,
            record_workflow_event,
        ]


def make_input(**overrides) -> ResearchLeadInput:
    base = {
        "workspace_id": str(uuid.uuid4()),
        "campaign_id": str(uuid.uuid4()),
        "lead_id": str(uuid.uuid4()),
        "run_key": "run-key-1",
        "seed_url": "https://fixture-business.test/",
    }
    base.update(overrides)
    return ResearchLeadInput(**base)


async def run_workflow(
    env: WorkflowEnvironment,
    recorder: Recorder,
    request: ResearchLeadInput,
    *,
    signal_after=None,
):
    client: Client = env.client
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[LeadResearchWorkflow],
        activities=recorder.activities(),
    ):
        handle = await client.start_workflow(
            LeadResearchWorkflow.run,
            request,
            id=research_workflow_id(
                request.workspace_id, request.campaign_id, request.lead_id
            ),
            task_queue=TASK_QUEUE,
        )
        if signal_after is not None:
            await signal_after(handle)
        # Bounded. A bug inside the workflow body is a *workflow task* failure,
        # which Temporal retries forever by design -- correct in production,
        # where you deploy a fix and the run resumes, but in a test it hangs the
        # suite. This turns that into a failure with a usable message.
        try:
            result = await asyncio.wait_for(handle.result(), timeout=WORKFLOW_TIMEOUT)
        except TimeoutError as exc:
            described = await handle.describe()
            raise AssertionError(
                f"workflow did not complete within {WORKFLOW_TIMEOUT}s "
                f"(status {described.status}); a workflow task is most likely "
                f"failing and being retried -- check the worker log above"
            ) from exc
        return result, handle


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


# ==========================================================================
# The happy path
# ==========================================================================
@pytest.mark.asyncio
async def test_full_research_run_queues_a_message(env) -> None:
    recorder = Recorder()
    result, _ = await run_workflow(env, recorder, make_input())

    assert result.outcome == ResearchOutcome.COMPLETED.value
    assert result.outbox_id == "outbox-1"
    assert result.score == 88
    assert recorder.calls == [
        "open_research_run",
        "crawl_lead_website",
        "analyse_evidence",
        "score_lead",
        "resolve_contact",
        "generate_draft",
        "requires_human_approval",
        "queue_message",
    ]


@pytest.mark.asyncio
async def test_workflow_id_is_derived_from_the_work_not_the_caller(env) -> None:
    """Starting the same research twice must not run it twice."""
    request = make_input()
    expected = research_workflow_id(
        request.workspace_id, request.campaign_id, request.lead_id
    )
    assert expected == research_workflow_id(
        request.workspace_id, request.campaign_id, request.lead_id
    )
    assert request.lead_id in expected


# ==========================================================================
# Stopping conditions
# ==========================================================================
@pytest.mark.asyncio
async def test_blocked_crawl_stops_before_drafting(env) -> None:
    recorder = Recorder(
        crawl=CrawlActivityResult(
            crawl_run_id="c",
            status="blocked",
            pages_captured=0,
            blocked_reason="private_or_reserved_address",
        )
    )
    result, _ = await run_workflow(env, recorder, make_input())

    assert result.outcome == ResearchOutcome.BLOCKED.value
    assert "private" in (result.detail or "")
    assert "generate_draft" not in recorder.calls
    assert "queue_message" not in recorder.calls


@pytest.mark.asyncio
async def test_no_evidence_stops_before_drafting(env) -> None:
    """Invariant 7 enforced early: nothing evidenced means nothing to say."""
    recorder = Recorder(
        analysis=AnalyseActivityResult(findings_created=2, pitchable_findings=0)
    )
    result, _ = await run_workflow(env, recorder, make_input())

    assert result.outcome == ResearchOutcome.NO_EVIDENCE.value
    assert "generate_draft" not in recorder.calls


@pytest.mark.asyncio
async def test_score_below_threshold_stops(env) -> None:
    recorder = Recorder(
        score=ScoreActivityResult(
            total=41, band="reject", passed_threshold=False, threshold=70
        )
    )
    result, _ = await run_workflow(env, recorder, make_input())

    assert result.outcome == ResearchOutcome.BELOW_THRESHOLD.value
    assert result.score == 41
    assert "resolve_contact" not in recorder.calls


@pytest.mark.asyncio
async def test_no_eligible_contact_stops(env) -> None:
    recorder = Recorder(
        contact=ContactActivityResult(
            eligible_channel_id=None,
            rejected_reasons=("address was pattern-guessed and is never eligible",),
        )
    )
    result, _ = await run_workflow(env, recorder, make_input())

    assert result.outcome == ResearchOutcome.NO_ELIGIBLE_CONTACT.value
    assert "pattern-guessed" in (result.detail or "")
    assert "generate_draft" not in recorder.calls


@pytest.mark.asyncio
async def test_failed_validation_stops_before_queueing(env) -> None:
    recorder = Recorder(
        draft=DraftActivityResult(
            draft_id="draft-bad",
            validation_passed=False,
            violation_codes=("unsupported_claim", "missing_evidence"),
        )
    )
    result, _ = await run_workflow(env, recorder, make_input())

    assert result.outcome == ResearchOutcome.DRAFT_REJECTED.value
    assert "queue_message" not in recorder.calls


@pytest.mark.asyncio
async def test_queue_refusal_is_reported_not_swallowed(env) -> None:
    recorder = Recorder(
        queued=QueueActivityResult(
            outbox_id=None,
            queued=False,
            refused_reasons=("recipient is suppressed",),
        )
    )
    result, _ = await run_workflow(env, recorder, make_input())

    assert result.outcome == ResearchOutcome.BLOCKED.value
    assert "suppressed" in (result.detail or "")


# ==========================================================================
# Retry behaviour (invariant 11 at the workflow layer)
# ==========================================================================
@pytest.mark.asyncio
async def test_transient_crawl_failure_is_retried_then_succeeds(env) -> None:
    recorder = Recorder(crawl_failures=2)
    result, _ = await run_workflow(env, recorder, make_input())

    assert result.outcome == ResearchOutcome.COMPLETED.value
    # Three attempts at the activity, one successful completion.
    assert recorder.calls.count("crawl_lead_website") == 3


@pytest.mark.asyncio
async def test_activity_retries_do_not_duplicate_workflow_events(env) -> None:
    """Mission 5.1.

    The event key is semantic, so however many times an activity runs, the
    workflow emits the same key -- which the unique constraint collapses.
    """
    recorder = Recorder(crawl_failures=2)
    await run_workflow(env, recorder, make_input())

    assert len(recorder.events) == len(set(recorder.events)), (
        f"duplicate event keys emitted: {recorder.events}"
    )
    assert all(key.startswith("run-key-1:") for key in recorder.events)


@pytest.mark.asyncio
async def test_event_keys_are_stable_across_runs(env) -> None:
    """Two executions with the same run_key produce identical event keys."""
    first = Recorder()
    await run_workflow(env, first, make_input(lead_id=str(uuid.uuid4())))
    second = Recorder()
    await run_workflow(env, second, make_input(lead_id=str(uuid.uuid4())))
    assert first.events == second.events


# ==========================================================================
# Approval (invariant 18, and a real deadline)
# ==========================================================================
@pytest.mark.asyncio
async def test_approval_required_waits_then_queues_on_approval(env) -> None:
    recorder = Recorder(needs_approval=True)

    async def approve(handle):
        # Wait until the workflow is actually parked on the signal.
        for _ in range(50):
            status = await handle.query(LeadResearchWorkflow.status)
            if status.stage == "awaiting_approval":
                break
            await env.sleep(1)
        await handle.signal(
            LeadResearchWorkflow.approval_decision,
            ApprovalDecisionSignal(decision="approved", approval_id="appr-1"),
        )

    result, _ = await run_workflow(env, recorder, make_input(), signal_after=approve)

    assert result.outcome == ResearchOutcome.COMPLETED.value
    assert "queue_message" in recorder.calls


@pytest.mark.asyncio
async def test_rejection_stops_before_queueing(env) -> None:
    recorder = Recorder(needs_approval=True)

    async def reject(handle):
        for _ in range(50):
            status = await handle.query(LeadResearchWorkflow.status)
            if status.stage == "awaiting_approval":
                break
            await env.sleep(1)
        await handle.signal(
            LeadResearchWorkflow.approval_decision,
            ApprovalDecisionSignal(decision="rejected", reason="tone is off"),
        )

    result, _ = await run_workflow(env, recorder, make_input(), signal_after=reject)

    assert result.outcome == ResearchOutcome.DRAFT_REJECTED.value
    assert "tone is off" in (result.detail or "")
    assert "queue_message" not in recorder.calls


@pytest.mark.asyncio
async def test_approval_expires_rather_than_waiting_forever(env) -> None:
    """The pre-0.2 workflow had a wait_condition with no timeout."""
    recorder = Recorder(needs_approval=True)

    async def wait_past_the_deadline(handle):
        for _ in range(50):
            status = await handle.query(LeadResearchWorkflow.status)
            if status.stage == "awaiting_approval":
                break
            await env.sleep(1)
        # Time-skipping makes an 8-day sleep instantaneous.
        await env.sleep(60 * 60 * 24 * 8)

    result, _ = await run_workflow(
        env, recorder, make_input(), signal_after=wait_past_the_deadline
    )

    assert result.outcome == ResearchOutcome.APPROVAL_EXPIRED.value
    assert "queue_message" not in recorder.calls


@pytest.mark.asyncio
async def test_only_the_first_approval_signal_counts(env) -> None:
    """A second signal must not overturn a recorded decision."""
    recorder = Recorder(needs_approval=True)

    async def double_signal(handle):
        for _ in range(50):
            status = await handle.query(LeadResearchWorkflow.status)
            if status.stage == "awaiting_approval":
                break
            await env.sleep(1)
        await handle.signal(
            LeadResearchWorkflow.approval_decision,
            ApprovalDecisionSignal(decision="rejected", reason="first"),
        )
        await handle.signal(
            LeadResearchWorkflow.approval_decision,
            ApprovalDecisionSignal(decision="approved", reason="second"),
        )

    result, _ = await run_workflow(
        env, recorder, make_input(), signal_after=double_signal
    )
    assert result.outcome == ResearchOutcome.DRAFT_REJECTED.value


# ==========================================================================
# Cancellation and queries
# ==========================================================================
@pytest.mark.asyncio
async def test_cancel_signal_stops_the_run(env) -> None:
    recorder = Recorder(needs_approval=True)

    async def cancel(handle):
        for _ in range(50):
            status = await handle.query(LeadResearchWorkflow.status)
            if status.stage == "awaiting_approval":
                break
            await env.sleep(1)
        await handle.signal(LeadResearchWorkflow.cancel_research, "operator stopped it")

    result, _ = await run_workflow(env, recorder, make_input(), signal_after=cancel)

    assert result.outcome == ResearchOutcome.CANCELLED.value
    assert "operator stopped it" in (result.detail or "")
    assert "queue_message" not in recorder.calls


@pytest.mark.asyncio
async def test_status_query_reports_progress(env) -> None:
    recorder = Recorder(needs_approval=True)
    observed: list[str] = []

    async def observe(handle):
        for _ in range(50):
            status = await handle.query(LeadResearchWorkflow.status)
            observed.append(status.stage)
            if status.stage == "awaiting_approval":
                assert status.draft_id == "draft-1"
                assert status.score == 88
                assert status.pitchable_findings == 3
                assert status.awaiting_approval_since is not None
                break
            await env.sleep(1)
        await handle.signal(
            LeadResearchWorkflow.approval_decision,
            ApprovalDecisionSignal(decision="approved"),
        )

    await run_workflow(env, recorder, make_input(), signal_after=observe)
    assert "awaiting_approval" in observed


# Synchronous: it reads a file, and an async test doing blocking file I/O is
# both pointless and a lint error.
def test_workflow_body_performs_no_io() -> None:
    """The determinism property, checked structurally.

    A workflow that imports a database session or an HTTP client will corrupt
    its history on replay. The pre-0.2 workflow called LangGraph inline.
    """
    import ast
    import pathlib

    source = pathlib.Path(
        pathlib.Path(__file__).resolve().parents[2] / "titan/workflows/research.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    banned = {"httpx", "sqlalchemy", "psycopg", "asyncio", "requests", "socket"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not (imported & banned), (
        f"workflow module imports non-deterministic dependencies: {imported & banned}"
    )
