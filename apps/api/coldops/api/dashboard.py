"""The operator dashboard's read surface: the whole pipeline, every mailbox, every event.

Three views the CRM did not have, assembled from tables that already exist:

* **Pipeline** -- thirteen stages from "discovered" to "meeting", each a count
  of *distinct businesses* that reached it, and the businesses behind any one
  stage. Every stage is a query over the table that owns the fact, never a
  status column somebody has to remember to move.
* **Mailboxes** -- per sending mailbox: carrier, whether it may send cold
  mail, warm-up day and today's warm-up volume, mail sent this week, and the
  latest inbox test; plus the warm-up partners and test inboxes by address.
* **Activity** -- the ``events`` stream, newest first, with the business name.

Read-only, behind the same session auth as the rest of the CRM, and every
query names its workspace explicitly (row-level security is not what scopes
raw SQL here).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coldops.api.security import Principal, require
from coldops.config import get_settings
from coldops.db.enums import HUMAN_REPLY_CLASSES
from coldops.db.session import workspace_session
from coldops.intelligence.intent import POSITIVE_CLASSES

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


# --------------------------------------------------------------------- pipeline
@dataclasses.dataclass(frozen=True)
class Stage:
    key: str
    label: str
    hint: str
    #: SELECT producing ``lead_id, at`` for every lead that reached the stage;
    #: ``:ws`` is bound. ``at`` is when it got there, for ordering the drill-down.
    leads: str


def _classes(classes: frozenset) -> str:  # type: ignore[type-arg]
    return ", ".join(f"'{c.value}'" for c in sorted(classes, key=lambda c: c.value))


STAGES: tuple[Stage, ...] = (
    Stage(
        "discovered",
        "Discovered",
        "found on Google Maps",
        "SELECT l.id AS lead_id, l.created_at AS at FROM leads l WHERE l.workspace_id = :ws",
    ),
    Stage(
        "researched",
        "Researched",
        "website crawled and checked",
        "SELECT r.lead_id, max(r.finished_at) AS at FROM research_runs r "
        "WHERE r.workspace_id = :ws AND r.finished_at IS NOT NULL GROUP BY r.lead_id",
    ),
    Stage(
        "scored",
        "Scored",
        "given a score",
        "SELECT s.lead_id, max(s.created_at) AS at FROM lead_scores s "
        "WHERE s.workspace_id = :ws GROUP BY s.lead_id",
    ),
    Stage(
        "qualified",
        "Qualified",
        "score passed the bar",
        "SELECT s.lead_id, max(s.created_at) AS at FROM lead_scores s "
        "WHERE s.workspace_id = :ws AND s.passed_threshold GROUP BY s.lead_id",
    ),
    Stage(
        "contactable",
        "Contactable",
        "a usable email address",
        "SELECT l.id AS lead_id, l.updated_at AS at FROM leads l "
        "WHERE l.workspace_id = :ws AND l.primary_contact_channel_id IS NOT NULL",
    ),
    Stage(
        "drafted",
        "Drafted",
        "an email written and validated",
        "SELECT d.lead_id, max(d.created_at) AS at FROM message_drafts d "
        "WHERE d.workspace_id = :ws AND d.validation_passed GROUP BY d.lead_id",
    ),
    Stage(
        "approved",
        "Approved",
        "a person said yes",
        "SELECT d.lead_id, max(a.decided_at) AS at FROM message_approvals a "
        "JOIN message_drafts d ON d.id = a.draft_id "
        "WHERE a.workspace_id = :ws AND a.decision = 'approved' GROUP BY d.lead_id",
    ),
    Stage(
        "sent",
        "Sent",
        "left a mailbox",
        "SELECT m.lead_id, max(m.sent_at) AS at FROM messages m "
        "WHERE m.workspace_id = :ws AND m.sent_at IS NOT NULL GROUP BY m.lead_id",
    ),
    Stage(
        "delivered",
        "Delivered",
        "sent and not bounced",
        "SELECT m.lead_id, max(m.sent_at) AS at FROM messages m "
        "WHERE m.workspace_id = :ws AND m.sent_at IS NOT NULL AND m.bounced_at IS NULL "
        "GROUP BY m.lead_id",
    ),
    Stage(
        "seen",
        "Seen",
        "a person opened the email's evidence page (measured from 3 Oct)",
        "SELECT g.lead_id, max(g.occurred_at) AS at FROM engagement_events g "
        "WHERE g.workspace_id = :ws AND g.lead_id IS NOT NULL "
        "AND g.grade IN ('confirmed', 'likely') GROUP BY g.lead_id",
    ),
    Stage(
        "replied",
        "Replied",
        "a person wrote back",
        "SELECT i.lead_id, max(i.received_at) AS at FROM inbound_messages i "  # noqa: S608 -- module constants only
        "JOIN reply_classifications c ON c.inbound_message_id = i.id "
        "WHERE i.workspace_id = :ws AND i.lead_id IS NOT NULL "
        f"AND c.reply_class::text IN ({_classes(HUMAN_REPLY_CLASSES)}) GROUP BY i.lead_id",
    ),
    Stage(
        "positive",
        "Positive",
        "interested, wants a call, info or price",
        "SELECT i.lead_id, max(i.received_at) AS at FROM inbound_messages i "  # noqa: S608 -- module constants only
        "JOIN reply_classifications c ON c.inbound_message_id = i.id "
        "WHERE i.workspace_id = :ws AND i.lead_id IS NOT NULL "
        f"AND c.reply_class::text IN ({_classes(POSITIVE_CLASSES)}) GROUP BY i.lead_id",
    ),
    Stage(
        "meeting",
        "Meeting",
        "a call or meeting on record",
        "SELECT t.lead_id, max(t.created_at) AS at FROM meetings t "
        "WHERE t.workspace_id = :ws GROUP BY t.lead_id",
    ),
)
_BY_KEY = {stage.key: stage for stage in STAGES}


class StageOut(BaseModel):
    key: str
    label: str
    hint: str
    count: int
    #: Share of all discovered businesses that reached this stage. Not "of the
    #: previous stage": the history is not strictly nested -- a business can
    #: be contactable without qualifying, and Smartlead-era mail went out with
    #: no approval on record -- so a stage-to-stage ratio can exceed 100%.
    of_discovered: float | None


class PipelineOut(BaseModel):
    stages: list[StageOut]
    computed_at: dt.datetime


class StageLeadOut(BaseModel):
    lead_id: uuid.UUID
    business_name: str | None
    domain: str | None
    at: dt.datetime | None
    latest_score: int | None


async def pipeline_counts(
    session: AsyncSession, workspace_id: uuid.UUID
) -> list[StageOut]:
    out: list[StageOut] = []
    base: int | None = None
    for stage in STAGES:
        count = int(
            (
                await session.execute(
                    text(f"SELECT count(DISTINCT q.lead_id) FROM ({stage.leads}) AS q"),  # noqa: S608
                    {"ws": workspace_id},
                )
            ).scalar_one()
        )
        out.append(
            StageOut(
                key=stage.key,
                label=stage.label,
                hint=stage.hint,
                count=count,
                of_discovered=(count / base) if base else None,
            )
        )
        if base is None:
            base = count
    return out


@router.get("/pipeline", response_model=PipelineOut)
async def pipeline(
    principal: Principal = Depends(require("research:read")),
) -> PipelineOut:
    async with workspace_session(principal.workspace_id) as session:
        stages = await pipeline_counts(session, principal.workspace_id)
    return PipelineOut(stages=stages, computed_at=dt.datetime.now(dt.UTC))


@router.get("/pipeline/{stage}", response_model=list[StageLeadOut])
async def pipeline_stage(
    stage: str,
    limit: int = Query(default=50, ge=1, le=500),
    principal: Principal = Depends(require("research:read")),
) -> list[StageLeadOut]:
    """The businesses that reached one stage, most recent first."""
    found = _BY_KEY.get(stage)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no stage called {stage!r}")
    async with workspace_session(principal.workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    f"SELECT q.lead_id, max(q.at) AS at, o.display_name, o.canonical_domain, "  # noqa: S608
                    f"l.latest_score FROM ({found.leads}) AS q "
                    "JOIN leads l ON l.id = q.lead_id AND l.workspace_id = :ws "
                    "JOIN organizations o ON o.id = l.organization_id "
                    "GROUP BY q.lead_id, o.display_name, o.canonical_domain, l.latest_score "
                    "ORDER BY max(q.at) DESC NULLS LAST LIMIT :limit"
                ),
                {"ws": principal.workspace_id, "limit": limit},
            )
        ).all()
    return [
        StageLeadOut(
            lead_id=r.lead_id,
            business_name=r.display_name,
            domain=r.canonical_domain,
            at=r.at,
            latest_score=r.latest_score,
        )
        for r in rows
    ]


# -------------------------------------------------------------------- mailboxes
class InboxTestOut(BaseModel):
    provider: str
    folder: str | None
    sent_at: dt.datetime
    checked_at: dt.datetime | None


class MailboxOut(BaseModel):
    address: str
    carrier: str | None
    in_mailbox_file: bool
    enabled: bool
    #: Whether it may send cold mail at all (sender identity active).
    sender_active: bool
    warmup_day: int
    warmup_today: int
    cold_sent_7d: int
    inbox_tests_7d: int
    inbox_landed_7d: int
    latest_tests: list[InboxTestOut]


class MailboxesOut(BaseModel):
    mailboxes: list[MailboxOut]
    warmup_enabled: bool
    warmup_hours_utc: list[int]
    partners: list[str]
    seeds: list[str]


def _file_accounts(path: str | None) -> dict[str, Any]:
    """address -> raw entry, including switched-off ones, read directly."""
    import json
    import pathlib

    if not path:
        return {}
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        str(e.get("from_email", "")).lower(): e
        for e in data.get("mailboxes", [])
        if isinstance(e, dict) and e.get("from_email")
    }


def _addresses(loader, path: str | None) -> list[str]:  # type: ignore[no-untyped-def]
    if not path:
        return []
    try:
        return sorted(loader(path))
    except Exception:  # a broken file shows as none rather than failing the page
        return []


@router.get("/mailboxes", response_model=MailboxesOut)
async def mailboxes(
    principal: Principal = Depends(require("research:read")),
) -> MailboxesOut:
    from coldops.cli import _warmup_days
    from coldops.delivery.mailboxes import load_mailboxes
    from coldops.delivery.seeds import load_seeds
    from coldops.delivery.warmup import DAILY_VOLUME, SEND_HOURS

    settings = get_settings()
    ws = principal.workspace_id
    accounts = _file_accounts(settings.mailbox_file)
    days = await _warmup_days(ws)
    async with workspace_session(ws) as session:
        identities = {
            r.from_email.lower(): r
            for r in (
                await session.execute(
                    text(
                        "SELECT from_email, is_active, id FROM sender_identities "
                        "WHERE workspace_id = :ws"
                    ),
                    {"ws": ws},
                )
            ).all()
        }
        sent = {
            r.from_email.lower(): r.n
            for r in (
                await session.execute(
                    text(
                        "SELECT from_email, count(*) AS n FROM messages WHERE workspace_id = :ws "
                        "AND sent_at > now() - interval '7 days' GROUP BY from_email"
                    ),
                    {"ws": ws},
                )
            ).all()
        }
        tests = (
            await session.execute(
                text(
                    "SELECT lower(from_email) AS address, provider, folder, sent_at, checked_at "
                    "FROM placement_checks WHERE workspace_id = :ws "
                    "AND sent_at > now() - interval '7 days' ORDER BY sent_at DESC"
                ),
                {"ws": ws},
            )
        ).all()

    out: list[MailboxOut] = []
    for address in sorted(set(accounts) | set(identities)):
        entry = accounts.get(address, {})
        identity = identities.get(address)
        day = int(days.get(address, 0))
        mine = [t for t in tests if t.address == address]
        checked = [t for t in mine if t.folder]
        out.append(
            MailboxOut(
                address=address,
                carrier=(entry.get("smtp") or {}).get("host"),
                in_mailbox_file=bool(entry),
                enabled=bool(entry) and entry.get("enabled") is not False,
                sender_active=bool(identity and identity.is_active),
                warmup_day=day,
                warmup_today=DAILY_VOLUME[min(max(day, 0), len(DAILY_VOLUME) - 1)],
                cold_sent_7d=int(sent.get(address, 0)),
                inbox_tests_7d=len(checked),
                inbox_landed_7d=sum(
                    1 for t in checked if (t.folder or "").lower() == "inbox"
                ),
                latest_tests=[
                    InboxTestOut(
                        provider=t.provider,
                        folder=t.folder,
                        sent_at=t.sent_at,
                        checked_at=t.checked_at,
                    )
                    for t in mine[:5]
                ],
            )
        )
    return MailboxesOut(
        mailboxes=out,
        warmup_enabled=settings.warmup_enabled,
        warmup_hours_utc=list(SEND_HOURS),
        partners=_addresses(
            lambda p: load_mailboxes(p).addresses(), settings.warmup_partner_file
        ),
        seeds=_addresses(
            lambda p: [s.address for s in load_seeds(p).all()], settings.seed_file
        ),
    )


# --------------------------------------------------------------------- activity
class ActivityOut(BaseModel):
    id: int
    occurred_at: dt.datetime
    kind: str
    lead_id: uuid.UUID | None
    business_name: str | None
    payload: dict[str, Any]


@router.get("/activity", response_model=list[ActivityOut])
async def activity(
    limit: int = Query(default=100, ge=1, le=500),
    kind: str | None = Query(default=None, max_length=48),
    before: dt.datetime | None = Query(default=None),
    principal: Principal = Depends(require("research:read")),
) -> list[ActivityOut]:
    """The event stream, newest first. ``kind`` filters by prefix (``message.``);
    ``before`` pages back in time (pass the oldest ``occurred_at`` already shown)."""
    async with workspace_session(principal.workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT e.id, e.occurred_at, e.kind, e.lead_id, e.payload, o.display_name "
                    "FROM events e "
                    "LEFT JOIN leads l ON l.id = e.lead_id AND l.workspace_id = :ws "
                    "LEFT JOIN organizations o ON o.id = l.organization_id "
                    "WHERE e.workspace_id = :ws "
                    "AND (CAST(:kind AS text) IS NULL OR e.kind LIKE :kind_like) "
                    "AND (CAST(:before AS timestamptz) IS NULL OR e.occurred_at < :before) "
                    "ORDER BY e.occurred_at DESC, e.id DESC LIMIT :limit"
                ),
                {
                    "ws": principal.workspace_id,
                    "kind": kind,
                    "kind_like": f"{kind}%" if kind else None,
                    "before": before,
                    "limit": limit,
                },
            )
        ).all()
    return [
        ActivityOut(
            id=r.id,
            occurred_at=r.occurred_at,
            kind=r.kind,
            lead_id=r.lead_id,
            business_name=r.display_name,
            payload=r.payload or {},
        )
        for r in rows
    ]


__all__ = ["STAGES", "pipeline_counts", "router"]
