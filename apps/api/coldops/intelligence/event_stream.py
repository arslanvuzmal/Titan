"""Every outcome, one table, in order -- projected from the tables that own it.

``events`` holds no fact that is not already somewhere else. Each projection
below is one ``INSERT ... SELECT`` from an owning table, keyed on the source
row, so running it again inserts nothing and running it over a window that
overlaps the last run is harmless. That is why the backfill and the scheduled
run are the same function with a different ``since``.

**Why projected rather than emitted.** The alternative -- every stage calling
``emit()`` as it acts -- is the pattern this codebase has failed at four times
over: a setting added to ``Settings`` and not to compose, a schedule written
and never installed, a table created and never written. An emitter is one more
line every future stage has to remember, and the stage that forgets produces
no error, only a quiet hole in every model trained afterwards. A projection
reads what the stage already had to write to do its job at all, so a stage
cannot do its job and be missing from the stream.

**The window is on when a row was recorded, not when its event happened.** A
bounce reported three days late has an old ``bounced_at`` and a fresh
``updated_at``; filtering on the event time would skip it forever. Each
projection names the column that moves when its fact is written.

**What is never copied:** bodies, addresses, names, IPs, user agents, notes.
The owning tables keep those under their own retention; ``events`` stays
append-only because it has nothing a retention job would need to remove.

**Every query filters on workspace explicitly.** Row-level security is not
what scopes reads in this codebase; raw SQL is unscoped unless it says so.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: The beginning of time for a backfill. Older than any row ColdOps holds.
EPOCH = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)

#: How far back a scheduled run re-reads. Long enough to cover a projector that
#: was down over a weekend; the unique key makes the overlap free.
DEFAULT_LOOKBACK = dt.timedelta(days=3)


@dataclasses.dataclass(frozen=True)
class Projection:
    """One owning table's contribution to the stream."""

    #: The value written to ``events.source``.
    source: str
    #: The kinds this projection can produce, for the coverage test and docs.
    kinds: tuple[str, ...]
    #: A SELECT producing exactly: workspace_id, lead_id, occurred_at, kind,
    #: source_id, payload. ``:ws`` and ``:since`` are bound.
    select: str


PROJECTIONS: tuple[Projection, ...] = (
    Projection(
        source="leads",
        kinds=("lead.discovered",),
        select="""
            SELECT l.workspace_id, l.id, l.created_at, 'lead.discovered', l.id::text,
                   jsonb_build_object(
                       'campaign_id', l.campaign_id,
                       'organization_id', l.organization_id,
                       'lead_source_id', l.lead_source_id)
            FROM leads l
            WHERE l.workspace_id = :ws AND l.created_at >= :since
        """,
    ),
    Projection(
        source="research_runs",
        kinds=("research.finished",),
        select="""
            SELECT r.workspace_id, r.lead_id, r.finished_at, 'research.finished', r.id::text,
                   jsonb_build_object(
                       'status', r.status::text,
                       'pages_crawled', r.pages_crawled,
                       'findings_count', r.findings_count,
                       'failure_reason', r.failure_reason,
                       'cost_usd', r.cost_usd)
            FROM research_runs r
            WHERE r.workspace_id = :ws AND r.finished_at IS NOT NULL
              AND r.updated_at >= :since
        """,
    ),
    Projection(
        source="lead_scores",
        kinds=("lead.scored",),
        select="""
            SELECT s.workspace_id, s.lead_id, s.created_at, 'lead.scored', s.id::text,
                   jsonb_build_object(
                       'total', s.total,
                       'band', s.band::text,
                       'passed_threshold', s.passed_threshold,
                       'threshold_applied', s.threshold_applied,
                       'policy_version', s.policy_version)
            FROM lead_scores s
            WHERE s.workspace_id = :ws AND s.created_at >= :since
        """,
    ),
    Projection(
        source="message_drafts",
        kinds=("draft.created",),
        select="""
            SELECT d.workspace_id, d.lead_id, d.created_at, 'draft.created', d.id::text,
                   jsonb_build_object(
                       'draft_id', d.id,
                       'campaign_id', d.campaign_id,
                       'sequence_step_id', d.sequence_step_id,
                       'template_key', d.template_key,
                       'variant', d.variant,
                       'validation_passed', d.validation_passed)
            FROM message_drafts d
            WHERE d.workspace_id = :ws AND d.created_at >= :since
        """,
    ),
    Projection(
        source="message_approvals",
        kinds=("draft.decided",),
        select="""
            SELECT a.workspace_id, d.lead_id, a.decided_at, 'draft.decided', a.id::text,
                   jsonb_build_object(
                       'draft_id', a.draft_id,
                       'decision', a.decision::text,
                       'draft_version', a.draft_version)
            FROM message_approvals a
            JOIN message_drafts d ON d.id = a.draft_id
            WHERE a.workspace_id = :ws AND a.created_at >= :since
        """,
    ),
    # One source row, up to four facts: each timestamp column is its own event,
    # and a row's ``updated_at`` moves whenever one of them is filled in.
    Projection(
        source="messages",
        kinds=(
            "message.sent",
            "message.delivered",
            "message.bounced",
            "message.complained",
        ),
        select="""
            SELECT m.workspace_id, m.lead_id, e.at, e.kind, m.id::text,
                   jsonb_build_object(
                       'message_id', m.id,
                       'draft_id', m.draft_id,
                       'campaign_id', m.campaign_id,
                       'sender_identity_id', m.sender_identity_id,
                       'to_domain', m.to_domain,
                       'provider', m.provider,
                       'local_sent_hour', m.local_sent_hour,
                       'local_sent_weekday', m.local_sent_weekday,
                       'one_pager_attached', m.one_pager_attached,
                       'bounce_kind', CASE WHEN e.kind = 'message.bounced'
                                           THEN m.bounce_kind::text END)
            FROM messages m
            CROSS JOIN LATERAL (VALUES
                ('message.sent', m.sent_at),
                ('message.delivered', m.delivered_at),
                ('message.bounced', m.bounced_at),
                ('message.complained', m.complained_at)
            ) AS e(kind, at)
            WHERE m.workspace_id = :ws AND e.at IS NOT NULL AND m.updated_at >= :since
        """,
    ),
    Projection(
        source="engagement_events",
        kinds=("seen.confirmed", "seen.likely", "seen.delivered", "seen.machine"),
        select="""
            SELECT g.workspace_id, g.lead_id, g.occurred_at, 'seen.' || g.grade, g.id::text,
                   jsonb_build_object(
                       'message_id', g.message_id,
                       'signal', g.kind,
                       'reason', g.reason)
            FROM engagement_events g
            WHERE g.workspace_id = :ws AND g.created_at >= :since
        """,
    ),
    Projection(
        source="inbound_messages",
        kinds=("reply.received",),
        select="""
            SELECT i.workspace_id, i.lead_id, i.received_at, 'reply.received', i.id::text,
                   jsonb_build_object(
                       'inbound_id', i.id,
                       'in_reply_to_message_id', i.in_reply_to_message_id,
                       'provider', i.provider)
            FROM inbound_messages i
            WHERE i.workspace_id = :ws AND i.created_at >= :since
        """,
    ),
    Projection(
        source="reply_classifications",
        kinds=("reply.classified",),
        select="""
            SELECT c.workspace_id, i.lead_id, c.created_at, 'reply.classified', c.id::text,
                   jsonb_build_object(
                       'inbound_id', c.inbound_message_id,
                       'reply_class', c.reply_class::text,
                       'confidence', c.confidence,
                       'decided_by', c.decided_by::text)
            FROM reply_classifications c
            JOIN inbound_messages i ON i.id = c.inbound_message_id
            WHERE c.workspace_id = :ws AND c.created_at >= :since
        """,
    ),
    Projection(
        source="call_outcomes",
        kinds=("call.logged",),
        select="""
            SELECT o.workspace_id, o.lead_id, o.called_at, 'call.logged', o.id::text,
                   jsonb_build_object(
                       'stage', o.stage,
                       'outcome', o.outcome,
                       'duration_seconds', o.duration_seconds,
                       'consent_to_email', o.consent_to_email,
                       'knew_about_defect', o.knew_about_defect,
                       'bant_budget', o.bant_budget,
                       'bant_authority', o.bant_authority,
                       'bant_need', o.bant_need,
                       'bant_timing', o.bant_timing,
                       'callback_set', o.callback_at IS NOT NULL)
            FROM call_outcomes o
            WHERE o.workspace_id = :ws AND o.created_at >= :since
        """,
    ),
    Projection(
        source="meetings",
        kinds=("meeting.recorded",),
        select="""
            SELECT t.workspace_id, t.lead_id, t.created_at, 'meeting.recorded', t.id::text,
                   jsonb_build_object(
                       'status', t.status::text,
                       'scheduled_at', t.scheduled_at,
                       'duration_minutes', t.duration_minutes)
            FROM meetings t
            WHERE t.workspace_id = :ws AND t.created_at >= :since
        """,
    ),
    # Keyed by address, not lead, so it carries no lead and no address: the
    # count and the reason are what a model or a dashboard can use.
    Projection(
        source="suppression_entries",
        kinds=("suppression.added",),
        select="""
            SELECT s.workspace_id, NULL::uuid, s.suppressed_at, 'suppression.added', s.id::text,
                   jsonb_build_object(
                       'scope', s.scope::text,
                       'reason', s.reason::text,
                       'source', s.source::text)
            FROM suppression_entries s
            WHERE s.workspace_id = :ws AND s.created_at >= :since
        """,
    ),
    Projection(
        source="autonomy_decisions",
        kinds=("manager.decided",),
        select="""
            SELECT a.workspace_id, NULL::uuid, a.decided_at, 'manager.decided', a.id::text,
                   jsonb_build_object(
                       'campaign_id', a.campaign_id,
                       'actuation', a.actuation::text,
                       'applied', a.applied,
                       'previous_value', a.previous_value,
                       'proposed_value', a.proposed_value,
                       'applied_value', a.applied_value,
                       'reason', a.reason)
            FROM autonomy_decisions a
            WHERE a.workspace_id = :ws AND a.created_at >= :since
              -- Only proposals that reached for a change, applied or refused.
              -- "Keep it as it is" every cycle is the manager's heartbeat, not
              -- an event: 282,944 of them were 92% of the first backfill.
              AND (a.applied OR a.proposed_value IS DISTINCT FROM a.previous_value)
        """,
    ),
    # A probe is two facts: it left, and later someone found where it landed.
    # ``checked_at`` is the only column that moves when the second is written.
    Projection(
        source="placement_checks",
        kinds=("placement.sent", "placement.checked"),
        select="""
            SELECT p.workspace_id, NULL::uuid, e.at, e.kind, p.id::text,
                   jsonb_build_object(
                       'provider', p.provider,
                       'had_attachment', p.had_attachment,
                       'folder', CASE WHEN e.kind = 'placement.checked' THEN p.folder END,
                       'checked_by', CASE WHEN e.kind = 'placement.checked'
                                          THEN p.checked_by END)
            FROM placement_checks p
            CROSS JOIN LATERAL (VALUES
                ('placement.sent', p.sent_at),
                ('placement.checked', p.checked_at)
            ) AS e(kind, at)
            WHERE p.workspace_id = :ws AND e.at IS NOT NULL
              AND GREATEST(p.created_at, COALESCE(p.checked_at, p.created_at)) >= :since
        """,
    ),
)


def _insert(projection: Projection) -> str:
    # Built from module constants only -- never from input -- and the values
    # that do vary (workspace, window) are bound parameters.
    return (
        "INSERT INTO events "  # noqa: S608
        "(workspace_id, lead_id, occurred_at, kind, source_id, payload, source) "
        f"SELECT q.*, '{projection.source}' FROM ({projection.select}) AS q "
        "ON CONFLICT (source, source_id, kind) DO NOTHING"
    )


@dataclasses.dataclass(frozen=True)
class ProjectionReport:
    #: Rows newly inserted, per source. A source at zero on a scheduled run is
    #: normal; at zero on a backfill it means the table is empty.
    inserted: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.inserted.values())


async def project(
    session: AsyncSession, *, workspace_id: uuid.UUID, since: dt.datetime
) -> ProjectionReport:
    """Bring ``events`` up to date for one workspace from ``since`` onward."""
    inserted: dict[str, int] = {}
    for projection in PROJECTIONS:
        result = await session.execute(
            text(_insert(projection)), {"ws": workspace_id, "since": since}
        )
        inserted[projection.source] = int(result.rowcount or 0)  # type: ignore[attr-defined]
    return ProjectionReport(inserted=inserted)


async def lead_history(
    session: AsyncSession, *, workspace_id: uuid.UUID, lead_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Everything that happened to one business, oldest first."""
    rows = (
        await session.execute(
            text(
                "SELECT occurred_at, kind, source, source_id, payload FROM events "
                "WHERE workspace_id = :ws AND lead_id = :lead "
                "ORDER BY occurred_at, id"
            ),
            {"ws": workspace_id, "lead": lead_id},
        )
    ).mappings()
    return [dict(r) for r in rows]


__all__ = [
    "DEFAULT_LOOKBACK",
    "EPOCH",
    "PROJECTIONS",
    "Projection",
    "ProjectionReport",
    "lead_history",
    "project",
]
