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
    #: Which message of the sequence to compose: 0 the opener, 1+ a follow-up.
    #: Passed to ``generate_draft`` unchanged. Defaulted so every existing
    #: caller -- the CLI, the re-draft script, the tests -- keeps asking for an
    #: opener without being edited.
    step_number: int = 0


@dataclasses.dataclass(frozen=True)
class CloseResearchRunInput:
    """Terminal write for a research run that is not going to finish normally.

    ``analyse_evidence`` closes the runs that reach it. Every other exit -- a
    blocked crawl, no pages captured, a score below threshold, no eligible
    contact, a rejected or expired draft, an operator cancellation, an
    unhandled error -- returned a result object and wrote nothing, so the run
    stayed ``running`` for ever and its lead stayed ``RESEARCHING``. 1,597 runs
    were in that state with no crawl ever started against them.
    """

    workspace_id: str
    research_run_id: str
    lead_id: str
    #: A :class:`ResearchOutcome` value. Stored as the run's terminal status
    #: so the reason a run stopped survives the workflow that knew it.
    outcome: str
    detail: str = ""


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
class ReadProfileInput:
    """Read a business's Google listing, for a lead with no website to crawl."""

    workspace_id: str
    lead_id: str
    research_run_id: str
    idempotency_key: str


@dataclasses.dataclass(frozen=True)
class ReadProfileResult:
    status: str
    findings_created: int
    #: How many of those the composer may actually say out loud. The workflow
    #: gates on this, not on findings_created: an absence detected while the
    #: class is still being counted rather than sent is stored at a confidence
    #: below the pitchable floor, and a draft built on nothing sayable is one
    #: the validator rejects after the work is done.
    pitchable_findings: int
    listing_url: str | None
    reason: str | None = None


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
    #: Which sequence step this draft is. 0 is the opener; 1 and above are
    #: follow-ups, and a follow-up must lead with a finding no earlier step has
    #: already cited (mission section 13) -- otherwise it is the first message
    #: again in different words, which is the thing the rule forbids.
    step_number: int = 0


@dataclasses.dataclass(frozen=True)
class DraftActivityResult:
    draft_id: str
    validation_passed: bool
    violation_codes: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class PullOptOutsInput:
    """One pass over the opt-outs the website has collected."""

    workspace_id: str


@dataclasses.dataclass(frozen=True)
class PullOptOutsResult:
    #: Addresses the endpoint holds, whether or not Titan already knew.
    found: int = 0
    #: Of those, the ones this pass suppressed for the first time.
    suppressed: int = 0
    refused_reason: str | None = None


@dataclasses.dataclass(frozen=True)
class CollectRepliesInput:
    """One pass over the carrier campaigns, looking for replies Titan has not
    seen."""

    workspace_id: str


@dataclasses.dataclass(frozen=True)
class CollectRepliesResult:
    carriers: int = 0
    #: Inbound messages found in Smartlead's threads, new or already ingested.
    seen: int = 0
    #: Of those, the ones this pass recorded for the first time.
    ingested: int = 0
    #: Replies from addresses Titan holds no lead for. Not an error -- Smartlead
    #: holds leads that were never imported -- but a rising number means the two
    #: systems have drifted apart.
    unmatched: int = 0
    refused_reason: str | None = None


@dataclasses.dataclass(frozen=True)
class ExpireAlarmsInput:
    workspace_id: str


@dataclasses.dataclass(frozen=True)
class ExpireAlarmsResult:
    expired: int = 0
    #: Everything still open afterwards, human tasks included. The number that
    #: says whether the queue is actually being worked.
    still_open: int = 0


@dataclasses.dataclass(frozen=True)
class RecheckClaimsInput:
    workspace_id: str


@dataclasses.dataclass(frozen=True)
class RecheckClaimsResult:
    checked: int = 0
    #: The defect is still there; the message stands.
    confirmed: int = 0
    #: The business fixed it. The message was withdrawn rather than sent.
    contradicted: int = 0
    #: Nothing was learned, so nothing changed -- see claim_recheck on why an
    #: inconclusive probe must never be read as "fixed".
    inconclusive: int = 0
    withdrawn: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class SweepStaleEvidenceInput:
    workspace_id: str


@dataclasses.dataclass(frozen=True)
class SweepStaleEvidenceResult:
    stale: int = 0
    reopened: int = 0
    #: Of those, the ones already past the send gate's own limit when found.
    already_unsendable: int = 0
    oldest_days: int = 0


@dataclasses.dataclass(frozen=True)
class ExpandMarketsInput:
    workspace_id: str


@dataclasses.dataclass(frozen=True)
class ExpandMarketsResult:
    #: Combinations that have stopped producing new businesses.
    exhausted: int = 0
    #: Markets opened this pass, as "business type / city".
    opened: tuple[str, ...] = ()
    reason: str = ""


@dataclasses.dataclass(frozen=True)
class PingWatchdogInput:
    """Nothing. The watchdog is per-installation, not per-workspace.

    Carried as a dataclass anyway because every other activity on this queue
    takes one, and a signature that is the odd one out is a signature somebody
    has to look up.
    """


@dataclasses.dataclass(frozen=True)
class PingWatchdogResult:
    #: False when no URL is configured, which is the shipped state and not a
    #: failure. Distinguished from a failed ping so the log can say which.
    pinged: bool = False
    reason: str = ""


@dataclasses.dataclass(frozen=True)
class CheckVitalsInput:
    """One pass over a workspace's pulse."""

    workspace_id: str


@dataclasses.dataclass(frozen=True)
class CheckVitalsResult:
    """What the pulse said, and what was raised because of it."""

    leads_in_hand: int
    days_of_fuel: float | None
    sends_today: int
    daily_send_capacity: int
    research_failure_rate: float | None
    #: Alarm codes raised on this pass. Empty is the healthy answer and is
    #: reported as such -- "no alarms" and "the check did not run" are
    #: different facts and the workflow log must be able to tell them apart.
    alarms: tuple[str, ...] = ()
    #: Alarms that were already open, so this pass wrote nothing new.
    suppressed: tuple[str, ...] = ()
    #: The rendered six-number reading, produced by the same pass that raised
    #: the alarms. Carried rather than re-derived: a second render would be a
    #: second reading taken a moment later, and the two could disagree about
    #: the numbers the alarms were justified by.
    reading: str = ""


@dataclasses.dataclass(frozen=True)
class ReopenStaleRunsInput:
    """One pass over the research runs that never closed."""

    workspace_id: str
    #: None takes the module default. Bounded because every lead returned to
    #: DISCOVERED buys another crawl and another analysis.
    limit: int | None = None


@dataclasses.dataclass(frozen=True)
class ReopenStaleRunsResult:
    found: int
    reopened: int
    #: The age of the oldest run reopened, in hours. A sweep that frees leads
    #: stranded for twelve days is reporting a different problem from one
    #: clearing this morning's restart, and without this they read identically.
    oldest_age_hours: int = 0


@dataclasses.dataclass(frozen=True)
class ReadmitLeadsInput:
    """One pass of the gate re-check."""

    workspace_id: str
    #: None takes ``readmission.DEFAULT_BATCH``.
    batch: int | None = None


@dataclasses.dataclass(frozen=True)
class ReadmitLeadsResult:
    #: Leads returned to the pipeline because the bar moved past them.
    promoted: int = 0
    #: Still parked above their own gate. Reported so a backlog of thousands
    #: can be watched draining rather than only noticed when it ends.
    remaining: int = 0


@dataclasses.dataclass(frozen=True)
class EraseExpiredDataInput:
    """One pass of the retention erasure."""

    workspace_id: str
    #: None takes ``retention.DEFAULT_BATCH``. Bounded because this runs beside
    #: work that matters more in the same hourly pass.
    batch: int | None = None


@dataclasses.dataclass(frozen=True)
class EraseExpiredDataResult:
    #: Leads past the window with no reply, and what was emptied for them.
    leads_examined: int = 0
    drafts_erased: int = 0
    pages_erased: int = 0
    #: Leads deliberately left alone because somebody there wrote back.
    #: Reported rather than silently skipped: "we erase everyone who ignored
    #: us" and "we erase everyone" are different policies, and the difference
    #: belongs in the numbers.
    kept_for_reply: int = 0


@dataclasses.dataclass(frozen=True)
class ReverifyContactsInput:
    """One pass of the stored-address re-check."""

    workspace_id: str
    #: None takes ``reverification.HOURLY_BATCH``. Small, because every one of
    #: these is a connection to somebody else's mail server, and housekeeping
    #: runs every hour.
    batch: int | None = None


@dataclasses.dataclass(frozen=True)
class ReverifyContactsResult:
    #: Addresses looked at, and how many got an answer from a verifier.
    examined: int = 0
    checked: int = 0
    #: Statuses that moved.
    changed: int = 0
    #: The number that matters: addresses that stopped being sendable. Each is
    #: a hard bounce that will not now happen.
    downgraded: int = 0
    reason: str = ""


@dataclasses.dataclass(frozen=True)
class ReleaseHeldInput:
    """One pass of the higher-risk address release."""

    workspace_id: str
    #: None takes ``trickle.DAILY_RELEASE_BUDGET``. The budget is per day, not
    #: per invocation: housekeeping is hourly and this machine sleeps, so the
    #: activity counts what today has already spent rather than assuming how
    #: often it ran.
    daily_budget: int | None = None


@dataclasses.dataclass(frozen=True)
class ReleaseHeldResult:
    #: Still held after this pass.
    held: int
    released: int
    #: Why nothing more was released, in words an operator can act on.
    reason: str = ""


@dataclasses.dataclass(frozen=True)
class DailyReportInput:
    """One check of whether the day is over and needs reporting."""

    workspace_id: str


@dataclasses.dataclass(frozen=True)
class HealSchedulesInput:
    """One pass over the schedules, looking for one whose clock has stopped."""

    workspace_id: str


@dataclasses.dataclass(frozen=True)
class SweepStrandedInput:
    """One pass over the approved drafts nothing ever queued."""

    workspace_id: str
    #: None takes the module default. Bounded because the sweeper competes with
    #: live sending for the same mailbox quota.
    limit: int | None = None


@dataclasses.dataclass(frozen=True)
class SweepStrandedResult:
    found: int
    queued: int
    refused: int
    #: Why the refused ones were refused, counted. A sweep that finds a hundred
    #: and queues none is a different problem from one that finds none, and
    #: without this they report identically.
    refused_reasons: tuple[tuple[str, int], ...] = ()


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
    #: Which message this is for the lead: 0 the opener, 1+ a follow-up.
    #:
    #: Carried rather than re-derived downstream. ``kind`` alone said a
    #: follow-up was owed without saying *which*, so the orchestrator sorted
    #: follow-ups to the front of the cycle and then started them as openers --
    #: the tag was written in one place and read in none.
    step_number: int = 0


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
# Sender verification
# ==========================================================================


@dataclasses.dataclass(frozen=True)
class VerifySendersInput:
    workspace_id: str


@dataclasses.dataclass(frozen=True)
class RampMailboxesInput:
    workspace_id: str
    #: When false the ramp computes and reports but writes nothing to the
    #: provider. The decision stays visible either way, so a dry run is a way to
    #: read what would happen rather than a way to disable the feature quietly.
    apply: bool = True


@dataclasses.dataclass(frozen=True)
class RampMailboxesResult:
    considered: int = 0
    #: Mailboxes whose daily limit actually moved.
    changed: int = 0
    raised: int = 0
    lowered: int = 0
    #: One line per mailbox, in the terms the decision was made on.
    detail: tuple[str, ...] = ()
    #: Set when the provider could not be reached at all. The ramp then leaves
    #: every limit exactly as it found it.
    unavailable: str | None = None


@dataclasses.dataclass(frozen=True)
class PollDeliveryEventsInput:
    workspace_id: str


@dataclasses.dataclass(frozen=True)
class PollDeliveryEventsResult:
    #: Statistics rows examined. One row can carry several events.
    rows_read: int = 0
    #: Events recorded for the first time. A run that reads a thousand rows and
    #: records nothing is the normal steady state, not a failure.
    recorded: int = 0
    #: Sends given a `messages` row for the first time. This is what makes the
    #: CRM, the bounce counting and every outcome query see Smartlead's mail.
    reconciled: int = 0
    #: Bounces whose consequence was applied late, because the send they refer
    #: to did not exist in `messages` when the event was first seen.
    healed: int = 0
    #: Recorded, but matching no lead. Worth watching: a number that climbs is
    #: attribution breaking, not sending stopping.
    unattributed: int = 0
    detail: tuple[str, ...] = ()
    unavailable: str | None = None


@dataclasses.dataclass(frozen=True)
class CaptureSenderHealthInput:
    workspace_id: str


@dataclasses.dataclass(frozen=True)
class CaptureSenderHealthResult:
    #: Identities given a point today. One row each, upserted, so this is the
    #: number of senders measured rather than the number of rows written.
    captured: int = 0
    detail: tuple[str, ...] = ()
    unavailable: str | None = None


@dataclasses.dataclass(frozen=True)
class VerifySendersResult:
    checked: int = 0
    #: Distinct domains looked up. Lower than ``checked`` whenever several
    #: identities share a domain, which is the normal case.
    domains_resolved: int = 0
    passing: int = 0
    failing: int = 0
    #: Identities that could send before this run and cannot after it. The only
    #: field here worth waking somebody for.
    newly_broken: tuple[str, ...] = ()


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
    "CollectRepliesInput",
    "CollectRepliesResult",
    "ContactActivityInput",
    "ContactActivityResult",
    "CrawlActivityInput",
    "CrawlActivityResult",
    "CycleVerdict",
    "DailyReportInput",
    "DiscoverActivityInput",
    "DiscoverActivityResult",
    "DraftActivityInput",
    "DraftActivityResult",
    "HealSchedulesInput",
    "OrchestratorStatus",
    "PauseSignal",
    "PlannedLead",
    "PullOptOutsInput",
    "PullOptOutsResult",
    "QueueActivityInput",
    "QueueActivityResult",
    "RecordEventInput",
    "ReopenStaleRunsInput",
    "ReopenStaleRunsResult",
    "ResearchLeadInput",
    "ResearchLeadResult",
    "ResearchOutcome",
    "ResearchStatus",
    "ScoreActivityInput",
    "ScoreActivityResult",
    "SweepStrandedInput",
    "SweepStrandedResult",
    "VerifySendersInput",
    "VerifySendersResult",
    "WeeklyReportInput",
    "WeeklyReportResult",
    "utc_iso",
]
