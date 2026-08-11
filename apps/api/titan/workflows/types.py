"""Typed arguments and results for workflows and activities.

Every workflow and activity signature uses these dataclasses rather than loose
dicts. Two reasons, both learned from the pre-0.2 code:

* A dict argument means a typo becomes a silent ``None`` at replay time, long
  after the deploy that introduced it.
* Temporal serialises arguments into workflow history. A stable, explicit shape
  is what makes an old history replayable against new code.

Nothing here holds a database session, a provider client, or an open connection
-- workflow arguments must be plain serialisable data.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from enum import StrEnum


class ResearchOutcome(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    NO_EVIDENCE = "no_evidence"
    BELOW_THRESHOLD = "below_threshold"
    NO_ELIGIBLE_CONTACT = "no_eligible_contact"
    DRAFT_REJECTED = "draft_rejected"
    APPROVAL_EXPIRED = "approval_expired"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclasses.dataclass(frozen=True)
class ResearchLeadInput:
    """Everything the research workflow needs. Deliberately IDs, not objects.

    Invariant 18: the workflow receives a *reference* to a campaign, never its
    policy. Policy is read from the database inside activities at execution
    time, so a stale or forged start request cannot widen what Titan may do.
    """

    workspace_id: str
    campaign_id: str
    lead_id: str
    #: Stable across retries; used to derive every downstream idempotency key.
    run_key: str
    seed_url: str | None = None


@dataclasses.dataclass(frozen=True)
class CrawlActivityInput:
    workspace_id: str
    lead_id: str
    research_run_id: str
    seed_url: str
    idempotency_key: str
    priority_paths: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class CrawlActivityResult:
    crawl_run_id: str
    status: str
    pages_captured: int
    blocked_reason: str | None = None
    failure_reason: str | None = None


@dataclasses.dataclass(frozen=True)
class AnalyseActivityInput:
    workspace_id: str
    lead_id: str
    research_run_id: str
    crawl_run_id: str
    idempotency_key: str


@dataclasses.dataclass(frozen=True)
class AnalyseActivityResult:
    findings_created: int
    pitchable_findings: int
    top_issue_type: str | None = None
    #: Commercial roll-up of the findings. Defaulted, so a history recorded
    #: before opportunities existed still replays against this code.
    opportunities_created: int = 0
    #: Of those, the ones the owner sells a fix for. The rest are recorded gaps
    #: and must never reach a message -- see titan.intelligence.opportunities.
    deliverable_opportunities: int = 0
    top_offer_key: str | None = None


@dataclasses.dataclass(frozen=True)
class ScoreActivityInput:
    workspace_id: str
    lead_id: str
    campaign_id: str
    research_run_id: str
    idempotency_key: str


@dataclasses.dataclass(frozen=True)
class ScoreActivityResult:
    total: int
    band: str
    passed_threshold: bool
    threshold: int


@dataclasses.dataclass(frozen=True)
class ContactActivityInput:
    workspace_id: str
    lead_id: str
    campaign_id: str
    research_run_id: str
    idempotency_key: str


@dataclasses.dataclass(frozen=True)
class ContactActivityResult:
    eligible_channel_id: str | None
    rejected_reasons: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class DraftActivityInput:
    workspace_id: str
    lead_id: str
    campaign_id: str
    research_run_id: str
    contact_channel_id: str
    idempotency_key: str
    template_key: str = "first_observation"


@dataclasses.dataclass(frozen=True)
class DraftActivityResult:
    draft_id: str
    validation_passed: bool
    violation_codes: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class QueueActivityInput:
    workspace_id: str
    draft_id: str
    approval_id: str | None
    idempotency_key: str


@dataclasses.dataclass(frozen=True)
class QueueActivityResult:
    outbox_id: str | None
    queued: bool
    refused_reasons: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class RecordEventInput:
    """Append-only workflow event.

    ``event_key`` is semantic, never a timestamp or a random value, so a retried
    activity that re-emits the same logical event is collapsed by the unique
    constraint rather than duplicated (mission section 5.1).
    """

    workspace_id: str
    workflow_id: str
    run_key: str
    event_key: str
    event_type: str
    sequence: int
    detail: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class ApprovalDecisionSignal:
    """Payload of the human approval signal."""

    decision: str  # "approved" | "rejected" | "changes_requested"
    approval_id: str | None = None
    decided_by: str | None = None
    reason: str | None = None


@dataclasses.dataclass(frozen=True)
class ResearchStatus:
    """Queryable workflow state, for the UI and for operators."""

    stage: str
    lead_id: str
    outcome: str | None = None
    score: int | None = None
    pitchable_findings: int = 0
    draft_id: str | None = None
    awaiting_approval_since: str | None = None
    blocked_reasons: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class ResearchLeadResult:
    outcome: str
    lead_id: str
    research_run_id: str | None = None
    score: int | None = None
    draft_id: str | None = None
    outbox_id: str | None = None
    detail: str | None = None
    finished_at: str | None = None


# ==========================================================================
# Discovery
# ==========================================================================


@dataclasses.dataclass(frozen=True)
class DiscoverActivityInput:
    workspace_id: str
    campaign_id: str
    #: Stable across retries. Recorded on the lead_sources row so a retry finds
    #: its own prior run instead of paying for a second billable search.
    idempotency_key: str
    #: Ceiling on leads created, not on results requested. Places charges per
    #: request either way, so this bounds the pipeline rather than the bill.
    max_results: int = 20


@dataclasses.dataclass(frozen=True)
class DiscoverActivityResult:
    leads_created: int = 0
    #: What Places returned before any admission rule ran. The gap between this
    #: and ``leads_created`` is the whole story of a disappointing run.
    returned: int = 0
    #: (reason, count) pairs. A tuple rather than a dict: workflow results are
    #: serialised into history, and a stable ordering keeps replays comparable.
    refused_counts: tuple[tuple[str, int], ...] = ()
    spent_usd: float = 0.0
    lead_source_id: str | None = None
    #: Set when the campaign could not be discovered for at all -- no targeting,
    #: no budget left, no API key. Distinct from finding nothing.
    refused_reason: str | None = None
    #: True when a prior attempt on this key had already run.
    duplicate: bool = False
    notified: bool = False

    @property
    def ran(self) -> bool:
        return self.refused_reason is None


# ==========================================================================
# Campaign orchestration
# ==========================================================================


class CycleVerdict(StrEnum):
    """Why a planning cycle produced the work it did -- or produced none."""

    #: Work was planned.
    READY = "ready"
    #: The campaign is paused, archived, or no longer authorized to send.
    NOT_AUTHORIZED = "not_authorized"
    #: Today's send allowance is spent. Normal, and not a fault.
    BUDGET_SPENT = "budget_spent"
    #: Authorized and funded, but no lead qualifies. This is the one worth
    #: telling an operator about: the campaign looks alive and is doing nothing.
    NO_WORK_AVAILABLE = "no_work_available"


@dataclasses.dataclass(frozen=True)
class CampaignCycleInput:
    workspace_id: str
    campaign_id: str
    #: Distinguishes one cycle's activities from the next in idempotency keys.
    cycle_key: str
    #: Ceiling on children started this cycle, independent of the send budget.
    #: Research is expensive whether or not the message is ever sent.
    max_new_research: int = 25


@dataclasses.dataclass(frozen=True)
class PlannedLead:
    lead_id: str
    seed_url: str | None = None
    #: "new" or "followup". Recorded so the orchestrator's own logs explain the
    #: mix without a join, and so follow-ups can be prioritised.
    kind: str = "new"


@dataclasses.dataclass(frozen=True)
class CampaignCyclePlan:
    verdict: str
    #: Leads to research this cycle, already ordered and budget-bounded.
    leads: tuple[PlannedLead, ...] = ()
    #: Sends still allowed today after subtracting what has already gone out.
    remaining_budget: int = 0
    followups_due: int = 0
    #: Populated for every verdict except READY.
    detail: str | None = None
    #: Researchable leads left in the pool after this plan was taken from it.
    #: The orchestrator tops the pool up before it empties, because a campaign
    #: that discovers only once it has stalled has already lost the cycle it
    #: spent stalling.
    pool_remaining: int = 0


@dataclasses.dataclass(frozen=True)
class CampaignOrchestratorInput:
    workspace_id: str
    campaign_id: str
    #: Minutes between planning cycles.
    interval_minutes: int = 60
    max_new_research: int = 25
    #: How many cycles before continue-as-new. A workflow that never
    #: continues-as-new accumulates history until it hits Temporal's limits and
    #: is force-terminated -- for an always-on orchestrator that is weeks away,
    #: not years, and the failure arrives with no warning.
    cycles_before_continue: int = 24
    #: Carried across continue-as-new so a status query still reports lifetime
    #: totals rather than resetting to zero every day.
    cycles_completed: int = 0
    leads_started: int = 0
    #: Carried for the same reason as the others: a lifetime total that resets
    #: on every roll-over is worse than no total, because it reads as one.
    leads_discovered: int = 0
    #: Also carried. Rolling over is an implementation detail of staying alive,
    #: and an operator who paused a campaign would not expect it to quietly
    #: resume itself the next time the workflow's history filled up.
    paused_reason: str | None = None


@dataclasses.dataclass(frozen=True)
class OrchestratorStatus:
    campaign_id: str
    state: str
    cycles_completed: int
    leads_started: int
    leads_discovered: int = 0
    last_verdict: str | None = None
    last_cycle_at: str | None = None
    next_cycle_at: str | None = None
    paused_reason: str | None = None


@dataclasses.dataclass(frozen=True)
class PauseSignal:
    reason: str


# ==========================================================================
# Weekly reporting
# ==========================================================================


@dataclasses.dataclass(frozen=True)
class WeeklyReportInput:
    workspace_id: str


@dataclasses.dataclass(frozen=True)
class WeeklyReportResult:
    workspace_id: str
    headline: str
    body: str
    messages_sent: int = 0
    replies_received: int = 0
    health: str = "insufficient_data"
    needs_attention: int = 0


def utc_iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat()


__all__ = [
    "AnalyseActivityInput",
    "AnalyseActivityResult",
    "ApprovalDecisionSignal",
    "CampaignCycleInput",
    "CampaignCyclePlan",
    "CampaignOrchestratorInput",
    "ContactActivityInput",
    "ContactActivityResult",
    "CrawlActivityInput",
    "CrawlActivityResult",
    "CycleVerdict",
    "DiscoverActivityInput",
    "DiscoverActivityResult",
    "DraftActivityInput",
    "DraftActivityResult",
    "OrchestratorStatus",
    "PauseSignal",
    "PlannedLead",
    "QueueActivityInput",
    "QueueActivityResult",
    "RecordEventInput",
    "ResearchLeadInput",
    "ResearchLeadResult",
    "ResearchOutcome",
    "ResearchStatus",
    "ScoreActivityInput",
    "ScoreActivityResult",
    "WeeklyReportInput",
    "WeeklyReportResult",
    "utc_iso",
]
