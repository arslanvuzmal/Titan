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


def utc_iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat()


__all__ = [
    "AnalyseActivityInput",
    "AnalyseActivityResult",
    "ApprovalDecisionSignal",
    "ContactActivityInput",
    "ContactActivityResult",
    "CrawlActivityInput",
    "CrawlActivityResult",
    "DraftActivityInput",
    "DraftActivityResult",
    "QueueActivityInput",
    "QueueActivityResult",
    "RecordEventInput",
    "ResearchLeadInput",
    "ResearchLeadResult",
    "ResearchOutcome",
    "ResearchStatus",
    "ScoreActivityInput",
    "ScoreActivityResult",
    "utc_iso",
]
