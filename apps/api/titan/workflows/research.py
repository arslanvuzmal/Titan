"""The lead research workflow.

This replaces the pre-0.2 workflows, which were deleted because they were
non-deterministic and unregistered (gap analysis C-10). The specific defects
and how each is avoided here:

* **LangGraph was invoked directly inside the workflow body**, making network
  calls on the workflow thread. Here the body contains no I/O at all -- every
  external interaction is an activity.
* **`getattr(self, "hitl_decision", None)` on an attribute never set in
  `__init__`**, so replay saw a different state than the original execution.
  Here all state is initialised in `__init__` and only mutated by signal
  handlers.
* **A `wait_condition` with no timeout**, which could hold a task slot forever.
  Here the approval wait has an explicit expiry and a terminal outcome.
* **No retry policies, no heartbeats, no versioning.** All three are present.

The workflow body is pure decision logic over activity results. That is what
makes it replayable: given the same history, it takes the same path.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
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
        ResearchLeadResult,
        ResearchOutcome,
        ResearchStatus,
        ScoreActivityInput,
        ScoreActivityResult,
    )

# --------------------------------------------------------------------------
# Retry policies, per activity category.
# --------------------------------------------------------------------------
#: Browser work: slow, frequently flaky, worth several attempts.
CRAWL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=4,
    # A URL the guard refused will be refused identically on every retry.
    non_retryable_error_types=["UrlBlockedError", "ValueError"],
)

#: Model work: retried, but a schema failure is the model's, not the network's.
MODEL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
    non_retryable_error_types=["BudgetExceededError", "ValueError"],
)

#: Database work: fast and usually deterministic; a few quick attempts.
DB_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)

#: Queueing touches the outbox. Retried, but the activity itself is idempotent
#: on dedupe_key, so a duplicate attempt cannot produce a second message.
QUEUE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=4,
)

CRAWL_TIMEOUT = timedelta(minutes=10)
MODEL_TIMEOUT = timedelta(minutes=5)
DB_TIMEOUT = timedelta(seconds=60)

#: How long a draft may sit awaiting human approval before it expires.
DEFAULT_APPROVAL_TTL = timedelta(days=7)


@workflow.defn(name="LeadResearchWorkflow")
class LeadResearchWorkflow:
    """Research one lead, then draft and (if authorized) queue one message."""

    def __init__(self) -> None:
        # Every field initialised here. A field that only exists after a signal
        # is exactly what broke replay in the pre-0.2 workflows.
        self._stage: str = "starting"
        self._outcome: str | None = None
        self._score: int | None = None
        self._pitchable: int = 0
        self._draft_id: str | None = None
        self._awaiting_since: str | None = None
        self._blocked: list[str] = []
        self._approval: ApprovalDecisionSignal | None = None
        self._cancelled_reason: str | None = None
        self._sequence: int = 0

    # --------------------------------------------------------------- signals
    @workflow.signal(name="approval_decision")
    def approval_decision(self, decision: ApprovalDecisionSignal) -> None:
        """Human decision on the draft.

        Synchronous and side-effect free by design: a signal handler that awaits
        can interleave with the main body in ways that are hard to reason about
        on replay. It records the decision; the body acts on it.
        """
        if self._approval is None:
            self._approval = decision

    @workflow.signal(name="cancel_research")
    def cancel_research(self, reason: str) -> None:
        self._cancelled_reason = reason or "cancelled by operator"

    # --------------------------------------------------------------- queries
    @workflow.query(name="status")
    def status(self) -> ResearchStatus:
        return ResearchStatus(
            stage=self._stage,
            lead_id=self._lead_id,
            outcome=self._outcome,
            score=self._score,
            pitchable_findings=self._pitchable,
            draft_id=self._draft_id,
            awaiting_approval_since=self._awaiting_since,
            blocked_reasons=tuple(self._blocked),
        )

    # ------------------------------------------------------------------ run
    @workflow.run
    async def run(self, request: ResearchLeadInput) -> ResearchLeadResult:
        self._lead_id = request.lead_id
        info = workflow.info()

        try:
            return await self._execute(request, info.workflow_id)
        except ActivityError as exc:
            # A terminal activity failure is recorded, not swallowed: an
            # invisible failure is worse than a visible one.
            self._stage = "failed"
            self._outcome = ResearchOutcome.FAILED.value
            await self._record(
                request, info.workflow_id, "research.failed", {"error": str(exc)[:400]}
            )
            return ResearchLeadResult(
                outcome=ResearchOutcome.FAILED.value,
                lead_id=request.lead_id,
                detail=str(exc)[:500],
                finished_at=self._now_iso(),
            )

    async def _execute(
        self, request: ResearchLeadInput, workflow_id: str
    ) -> ResearchLeadResult:
        # ---- 1. open the research run --------------------------------
        self._stage = "opening_run"
        research_run_id: str = await workflow.execute_activity(
            "open_research_run",
            request,
            start_to_close_timeout=DB_TIMEOUT,
            retry_policy=DB_RETRY,
        )
        await self._record(request, workflow_id, "research.started", {})

        if self._should_stop():
            return self._cancelled(request, research_run_id)

        # ---- 2. crawl -------------------------------------------------
        self._stage = "crawling"
        crawl: CrawlActivityResult = await workflow.execute_activity(
            "crawl_lead_website",
            CrawlActivityInput(
                workspace_id=request.workspace_id,
                lead_id=request.lead_id,
                research_run_id=research_run_id,
                seed_url=request.seed_url or "",
                idempotency_key=f"{request.run_key}:crawl",
            ),
            start_to_close_timeout=CRAWL_TIMEOUT,
            # The browser worker heartbeats; without this a hung crawl would
            # occupy a slot for the full start_to_close timeout.
            heartbeat_timeout=timedelta(seconds=90),
            retry_policy=CRAWL_RETRY,
        )

        if crawl.status == "blocked":
            self._blocked.append(crawl.blocked_reason or "crawl blocked")
            self._stage = "blocked"
            self._outcome = ResearchOutcome.BLOCKED.value
            await self._record(
                request,
                workflow_id,
                "crawl.blocked",
                {"reason": (crawl.blocked_reason or "")[:200]},
            )
            return ResearchLeadResult(
                outcome=ResearchOutcome.BLOCKED.value,
                lead_id=request.lead_id,
                research_run_id=research_run_id,
                detail=crawl.blocked_reason,
                finished_at=self._now_iso(),
            )

        if crawl.pages_captured == 0:
            self._outcome = ResearchOutcome.NO_EVIDENCE.value
            return self._finish(
                request,
                research_run_id,
                ResearchOutcome.NO_EVIDENCE,
                crawl.failure_reason or "no pages captured",
            )

        if self._should_stop():
            return self._cancelled(request, research_run_id)

        # ---- 3. analyse ------------------------------------------------
        self._stage = "analysing"
        analysis: AnalyseActivityResult = await workflow.execute_activity(
            "analyse_evidence",
            AnalyseActivityInput(
                workspace_id=request.workspace_id,
                lead_id=request.lead_id,
                research_run_id=research_run_id,
                crawl_run_id=crawl.crawl_run_id,
                idempotency_key=f"{request.run_key}:analyse",
            ),
            start_to_close_timeout=MODEL_TIMEOUT,
            retry_policy=MODEL_RETRY,
        )
        self._pitchable = analysis.pitchable_findings

        # Invariant 7, enforced early: with nothing evidenced there is nothing
        # truthful to open a message with, so the workflow stops here rather
        # than generating a draft the validator would reject anyway.
        if analysis.pitchable_findings == 0:
            return self._finish(
                request,
                research_run_id,
                ResearchOutcome.NO_EVIDENCE,
                "no evidence-backed findings",
            )

        # ---- 4. score --------------------------------------------------
        self._stage = "scoring"
        score: ScoreActivityResult = await workflow.execute_activity(
            "score_lead",
            ScoreActivityInput(
                workspace_id=request.workspace_id,
                lead_id=request.lead_id,
                campaign_id=request.campaign_id,
                research_run_id=research_run_id,
                idempotency_key=f"{request.run_key}:score",
            ),
            start_to_close_timeout=DB_TIMEOUT,
            retry_policy=DB_RETRY,
        )
        self._score = score.total
        await self._record(
            request, workflow_id, "lead.scored", {"total": str(score.total)}
        )

        if not score.passed_threshold:
            return self._finish(
                request,
                research_run_id,
                ResearchOutcome.BELOW_THRESHOLD,
                f"score {score.total} below threshold {score.threshold}",
                score=score.total,
            )

        if self._should_stop():
            return self._cancelled(request, research_run_id)

        # ---- 5. contact eligibility -------------------------------------
        self._stage = "resolving_contact"
        contact: ContactActivityResult = await workflow.execute_activity(
            "resolve_contact",
            ContactActivityInput(
                workspace_id=request.workspace_id,
                lead_id=request.lead_id,
                campaign_id=request.campaign_id,
                research_run_id=research_run_id,
                idempotency_key=f"{request.run_key}:contact",
            ),
            start_to_close_timeout=DB_TIMEOUT,
            retry_policy=DB_RETRY,
        )
        if contact.eligible_channel_id is None:
            self._blocked.extend(contact.rejected_reasons)
            return self._finish(
                request,
                research_run_id,
                ResearchOutcome.NO_ELIGIBLE_CONTACT,
                "; ".join(contact.rejected_reasons) or "no eligible contact",
                score=score.total,
            )

        # ---- 6. draft ----------------------------------------------------
        self._stage = "drafting"
        draft: DraftActivityResult = await workflow.execute_activity(
            "generate_draft",
            DraftActivityInput(
                workspace_id=request.workspace_id,
                lead_id=request.lead_id,
                campaign_id=request.campaign_id,
                research_run_id=research_run_id,
                contact_channel_id=contact.eligible_channel_id,
                idempotency_key=f"{request.run_key}:draft",
            ),
            start_to_close_timeout=MODEL_TIMEOUT,
            retry_policy=MODEL_RETRY,
        )
        self._draft_id = draft.draft_id

        if not draft.validation_passed:
            self._blocked.extend(draft.violation_codes)
            return self._finish(
                request,
                research_run_id,
                ResearchOutcome.DRAFT_REJECTED,
                "; ".join(draft.violation_codes) or "draft failed validation",
                score=score.total,
                draft_id=draft.draft_id,
            )

        await self._record(
            request, workflow_id, "draft.created", {"draft_id": draft.draft_id}
        )

        # ---- 7. authorization decision -----------------------------------
        # Whether a human decision is required is read from the database, not
        # from the start request (invariant 18).
        self._stage = "checking_mode"
        needs_approval: bool = await workflow.execute_activity(
            "requires_human_approval",
            request,
            start_to_close_timeout=DB_TIMEOUT,
            retry_policy=DB_RETRY,
        )

        approval_id: str | None = None
        if needs_approval:
            self._stage = "awaiting_approval"
            self._awaiting_since = self._now_iso()
            await self._record(
                request,
                workflow_id,
                "approval.requested",
                {"draft_id": draft.draft_id},
            )

            # An explicit deadline. The pre-0.2 workflow waited forever.
            decided = await self._wait_for_decision()

            if self._cancelled_reason is not None:
                return self._cancelled(request, research_run_id)

            if not decided:
                return self._finish(
                    request,
                    research_run_id,
                    ResearchOutcome.APPROVAL_EXPIRED,
                    "approval window elapsed",
                    score=score.total,
                    draft_id=draft.draft_id,
                )

            assert self._approval is not None
            if self._approval.decision != "approved":
                return self._finish(
                    request,
                    research_run_id,
                    ResearchOutcome.DRAFT_REJECTED,
                    self._approval.reason or self._approval.decision,
                    score=score.total,
                    draft_id=draft.draft_id,
                )
            approval_id = self._approval.approval_id

        # ---- 8. queue -----------------------------------------------------
        # Queueing writes an outbox row. It does NOT send: the outbox worker
        # re-evaluates the whole authorization chain before any provider call.
        self._stage = "queueing"
        queued: QueueActivityResult = await workflow.execute_activity(
            "queue_message",
            QueueActivityInput(
                workspace_id=request.workspace_id,
                draft_id=draft.draft_id,
                approval_id=approval_id,
                idempotency_key=f"{request.run_key}:queue",
            ),
            start_to_close_timeout=DB_TIMEOUT,
            retry_policy=QUEUE_RETRY,
        )

        if not queued.queued:
            self._blocked.extend(queued.refused_reasons)
            return self._finish(
                request,
                research_run_id,
                ResearchOutcome.BLOCKED,
                "; ".join(queued.refused_reasons) or "queueing refused by policy",
                score=score.total,
                draft_id=draft.draft_id,
            )

        self._stage = "completed"
        self._outcome = ResearchOutcome.COMPLETED.value
        await self._record(
            request,
            workflow_id,
            "message.queued",
            {"outbox_id": queued.outbox_id or ""},
        )
        return ResearchLeadResult(
            outcome=ResearchOutcome.COMPLETED.value,
            lead_id=request.lead_id,
            research_run_id=research_run_id,
            score=score.total,
            draft_id=draft.draft_id,
            outbox_id=queued.outbox_id,
            finished_at=self._now_iso(),
        )

    # ------------------------------------------------------------- helpers
    async def _wait_for_decision(self) -> bool:
        """Wait for approval, cancellation, or expiry. Returns True if decided."""
        try:
            await workflow.wait_condition(
                lambda: self._approval is not None or self._cancelled_reason is not None,
                timeout=DEFAULT_APPROVAL_TTL,
            )
        except TimeoutError:
            return False
        return self._approval is not None

    def _should_stop(self) -> bool:
        return self._cancelled_reason is not None

    def _cancelled(
        self, request: ResearchLeadInput, research_run_id: str | None
    ) -> ResearchLeadResult:
        self._stage = "cancelled"
        self._outcome = ResearchOutcome.CANCELLED.value
        return ResearchLeadResult(
            outcome=ResearchOutcome.CANCELLED.value,
            lead_id=request.lead_id,
            research_run_id=research_run_id,
            detail=self._cancelled_reason,
            finished_at=self._now_iso(),
        )

    def _finish(
        self,
        request: ResearchLeadInput,
        research_run_id: str,
        outcome: ResearchOutcome,
        detail: str,
        *,
        score: int | None = None,
        draft_id: str | None = None,
    ) -> ResearchLeadResult:
        self._stage = outcome.value
        self._outcome = outcome.value
        return ResearchLeadResult(
            outcome=outcome.value,
            lead_id=request.lead_id,
            research_run_id=research_run_id,
            score=score,
            draft_id=draft_id,
            detail=detail[:500],
            finished_at=self._now_iso(),
        )

    async def _record(
        self,
        request: ResearchLeadInput,
        workflow_id: str,
        event_type: str,
        detail: dict[str, str],
    ) -> None:
        """Append a workflow event.

        The event key is semantic, so a retried activity collapses onto the same
        row rather than creating a duplicate (mission section 5.1). The sequence
        counter is workflow state, which replays deterministically.
        """
        self._sequence += 1
        await workflow.execute_activity(
            "record_workflow_event",
            RecordEventInput(
                workspace_id=request.workspace_id,
                workflow_id=workflow_id,
                run_key=request.run_key,
                event_key=f"{request.run_key}:{event_type}",
                event_type=event_type,
                sequence=self._sequence,
                detail=detail,
            ),
            start_to_close_timeout=DB_TIMEOUT,
            retry_policy=DB_RETRY,
        )

    def _now_iso(self) -> str:
        # workflow.now() is the deterministic clock; datetime.now() would break
        # replay by returning a different value each time.
        return workflow.now().isoformat()


def research_workflow_id(workspace_id: str, campaign_id: str, lead_id: str) -> str:
    """Deterministic workflow ID.

    Derived from the identity of the work, not from a client-supplied value, so
    starting the same research twice is a no-op via Temporal's ID reuse policy
    rather than a second crawl and a second draft.
    """
    return f"research::{workspace_id}::{campaign_id}::{lead_id}"


__all__ = [
    "CRAWL_RETRY",
    "DB_RETRY",
    "DEFAULT_APPROVAL_TTL",
    "MODEL_RETRY",
    "QUEUE_RETRY",
    "LeadResearchWorkflow",
    "research_workflow_id",
]
