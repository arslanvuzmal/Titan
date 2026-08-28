"""Rewriting mail that was already written.

The message rules changed, and they did not change slightly: **628 of the 745
drafts standing in the queue fail them**. 366 claim a client base that cannot be
named; the rest are outside the length a stranger will read, or claim findings
nobody counted. The send gate refuses all of them now, which stops the wrong
words reaching anybody and leaves 596 leads with nothing to send.

Blocking was the urgent half. This is the other half.

**Composed through the production path, not beside it.** The obvious
implementation is a second gatherer: load the org, the findings, the evidence,
pick a headline, compose. That is the whole of ``generate_draft`` written twice,
and two gatherers agree on the day they are written and diverge the first time a
rule changes on one side -- invisibly, because both still produce a message and
only one of them is right.

So the old draft's idempotency key is moved aside and ``generate_draft`` is
called with the original key. It composes exactly what a new lead would get,
because it *is* what a new lead gets. If it refuses -- no offer matches the
headline finding under today's rules, say -- the rename is rolled back and the
lead keeps the draft it had, blocked but not lost.

**A draft that was actually sent is never touched.** There is no unsending, and
rewriting the record of what left the building would destroy the only account of
what a recipient actually read.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.enums import DraftStatus, OutboxStatus
from titan.db.models import AuditFinding, Message, MessageDraft, OutboxMessage
from titan.db.session import workspace_session, workspace_unit_of_work
from titan.intelligence.message_validator import (
    PITCH_MAX_WORDS,
    PITCH_MIN_WORDS,
    pitch_of,
    prohibited_content,
)
from titan.workflows.types import DraftActivityInput

#: Outbox states whose row still points at a message that has not left.
#:
#: ``sent`` is deliberately absent, and so is ``failed_permanent``: the first
#: has been read by somebody and the second will never be. Repointing either at
#: a new draft would rewrite history rather than the future.
LIVE_OUTBOX: frozenset[OutboxStatus] = frozenset(
    {OutboxStatus.PENDING, OutboxStatus.LEASED, OutboxStatus.DEFERRED}
)

#: Draft states a redraft may replace. A rejected or expired draft is a decision
#: somebody made and is left alone.
#:
#: ``VALIDATION_FAILED`` is here because it is not a decision, it is an attempt
#: that did not land -- and such a draft is unsendable by definition, so
#: replacing it can only improve matters. It earned its place: a first pass run
#: without the sender's mailing address in the environment produced 306 of them
#: with perfectly good copy and no footer, and without this they would have been
#: unreachable by the very command that made them.
REDRAFTABLE: frozenset[DraftStatus] = frozenset(
    {
        DraftStatus.GENERATED,
        DraftStatus.AWAITING_APPROVAL,
        DraftStatus.APPROVED,
        DraftStatus.QUEUED,
        DraftStatus.VALIDATION_FAILED,
    }
)


def why_stale(body: str, owner_name: str) -> str:
    """What today's rules object to, or an empty string if nothing does.

    The same two checks the send gate runs, so a draft this reports on is
    exactly a draft that would be refused at the door. Deliberately not the full
    validator: its footer rules depend on sender configuration, and a footer
    that was right when written is still right.
    """
    violation = prohibited_content(body or "")
    if violation is not None:
        return violation.code.value
    words = len(pitch_of(body or "", owner_name).split())
    if not PITCH_MIN_WORDS <= words <= PITCH_MAX_WORDS:
        return f"outside_the_word_band ({words} words)"
    return ""


def _rendered(
    payload: dict[str, Any] | None, draft: MessageDraft | None
) -> dict[str, Any]:
    """The queued payload, with the rewritten words in place of the old ones.

    Only the three fields a rewrite can change. Everything else in there -- the
    recipient, the sender, the unsubscribe headers -- was resolved against the
    sender identity and the contact channel when the row was queued, and
    re-deriving it here would be a second, differently-wrong answer to a
    question that was already answered correctly.
    """
    updated = dict(payload or {})
    if draft is None:
        return updated
    updated["subject"] = draft.subject
    updated["text_body"] = draft.body_text
    # Absent rather than empty, matching queue_message: a provider reads None as
    # "text only", where an empty string is a blank HTML alternative.
    updated["html_body"] = draft.body_html or None
    return updated


@dataclass(frozen=True, slots=True)
class StaleDraft:
    """One draft to rewrite, and everything needed to rewrite it."""

    draft_id: uuid.UUID
    lead_id: uuid.UUID
    campaign_id: uuid.UUID
    contact_channel_id: uuid.UUID
    idempotency_key: str
    research_run_id: uuid.UUID
    subject: str
    reason: str


@dataclass(slots=True)
class RedraftReport:
    """What was found, what was rewritten, and what refused."""

    stale: int = 0
    sent_already: int = 0
    without_a_research_run: int = 0
    rewritten: int = 0
    refused: dict[str, int] = field(default_factory=dict)
    still_failing: int = 0

    def line(self) -> str:
        return (
            f"{self.stale} stale, {self.rewritten} rewritten, "
            f"{sum(self.refused.values())} refused, "
            f"{self.sent_already} already sent (left alone)"
        )


async def find_stale(
    workspace_id: uuid.UUID, *, owner_name: str, everything: bool = False
) -> list[StaleDraft]:
    """Drafts that would be refused at the door, and can still be replaced.

    ``everything`` widens it to every unsent draft, whatever its copy says.
    Needed because not every improvement to the writing is something a rule
    can refuse: a subject line that named the trade's usual money page while
    the body named the page that was actually broken passed every check and
    still met the reader with a mismatch in the first two seconds.
    """
    out: list[StaleDraft] = []
    async with workspace_session(workspace_id) as session:
        rows = (
            (
                await session.execute(
                    select(MessageDraft).where(MessageDraft.status.in_(REDRAFTABLE))
                )
            )
            .scalars()
            .all()
        )
        for draft in rows:
            if draft.status is DraftStatus.VALIDATION_FAILED:
                codes = [
                    v.get("code", "")
                    for v in (draft.validation_report or {}).get("violations", [])
                ]
                reason = "validation_failed: " + (", ".join(codes) or "unknown")
            else:
                reason = why_stale(draft.body_text, owner_name)
            if not reason and not everything:
                continue
            reason = reason or "rewriting every draft"

            # A message that has left the building is a record, not a draft.
            sent = (
                await session.execute(
                    select(OutboxMessage.id).where(
                        OutboxMessage.draft_id == draft.id,
                        OutboxMessage.status == OutboxStatus.SENT,
                    )
                )
            ).first()
            if sent is not None:
                continue
            run_id = await _research_run_for(session, draft)
            if run_id is None:
                continue
            out.append(
                StaleDraft(
                    draft_id=draft.id,
                    lead_id=draft.lead_id,
                    campaign_id=draft.campaign_id,
                    contact_channel_id=draft.contact_channel_id,
                    idempotency_key=draft.idempotency_key,
                    research_run_id=run_id,
                    subject=draft.subject,
                    reason=reason,
                )
            )
    return out


async def _research_run_for(
    session: AsyncSession, draft: MessageDraft
) -> uuid.UUID | None:
    """Which crawl the replacement should be composed from.

    Taken from the finding the old message actually cited, rather than from the
    lead's most recent run. The recent run may have crawled a different set of
    pages; the claim map is the record of what this message was about, and
    rewriting it from a different crawl would change the subject as well as the
    words.
    """
    for entry in draft.claim_map or []:
        finding_id = entry.get("finding_id")
        if not finding_id:
            continue
        try:
            key = uuid.UUID(str(finding_id))
        except ValueError:
            continue
        run = (
            await session.execute(
                select(AuditFinding.research_run_id).where(AuditFinding.id == key)
            )
        ).scalar_one_or_none()
        if run is not None:
            return run
    return None


async def _free_key(session: AsyncSession, key: str) -> str:
    """A parking place for the old key that nothing else is using.

    A lead can be redrafted twice -- the first pass produced 306 drafts that
    could not send -- and a fixed suffix collides with the draft the first pass
    parked there, which violates the unique constraint and fails the whole
    rewrite for that lead.
    """
    for attempt in range(1, 100):
        candidate = f"{key}#superseded-{attempt}"[:200]
        taken = (
            await session.execute(
                select(MessageDraft.id).where(MessageDraft.idempotency_key == candidate)
            )
        ).first()
        if taken is None:
            return candidate
    raise RuntimeError(f"no free parking key for {key!r} after 99 attempts")


async def redraft_one(workspace_id: uuid.UUID, stale: StaleDraft) -> str:
    """Replace one draft. Returns "" on success, or the refusal code.

    Three steps, and the middle one is the production drafting activity with no
    modification at all.
    """
    from titan.activities.pipeline import generate_draft

    async with workspace_unit_of_work(workspace_id) as session:
        aside = await _free_key(session, stale.idempotency_key)
        await session.execute(
            update(MessageDraft)
            .where(MessageDraft.id == stale.draft_id)
            .values(idempotency_key=aside, status=DraftStatus.SUPERSEDED)
        )

    result = await generate_draft(
        DraftActivityInput(
            workspace_id=str(workspace_id),
            lead_id=str(stale.lead_id),
            campaign_id=str(stale.campaign_id),
            research_run_id=str(stale.research_run_id),
            contact_channel_id=str(stale.contact_channel_id),
            idempotency_key=stale.idempotency_key,
        )
    )

    # A draft row exists but did not pass is not a success. The first pass
    # reported 578 rewritten when 306 of them could not send, because it counted
    # rows written rather than messages that would leave.
    if not result.draft_id:
        # Nothing composed. Put the lead back exactly as it was: a draft the
        # gate refuses is worse than no draft only if it can still be sent, and
        # it cannot.
        async with workspace_unit_of_work(workspace_id) as session:
            await session.execute(
                update(MessageDraft)
                .where(MessageDraft.id == stale.draft_id)
                .values(
                    idempotency_key=stale.idempotency_key,
                    status=DraftStatus.AWAITING_APPROVAL,
                )
            )
        return result.violation_codes[0] if result.violation_codes else "refused"

    new_id = uuid.UUID(result.draft_id)
    # Written, but it still has to pass. Reporting a row as rewritten when it
    # cannot send is how a run says 578 and means 272.
    outcome = (
        ""
        if result.validation_passed
        else "wrote_but_failed_validation: "
        + (", ".join(result.violation_codes) or "unknown")
    )
    async with workspace_unit_of_work(workspace_id) as session:
        await session.execute(
            update(MessageDraft)
            .where(MessageDraft.id == stale.draft_id)
            .values(superseded_by_id=new_id)
        )
        # A queued row keeps its place and picks up the new words. It cannot
        # send on an approval given for the old ones: the new draft is version
        # one with no approval against it, and the gate refuses that.
        #
        # Repointing the foreign key is not enough, and quietly was not. The
        # outbox carries its own rendered copy of the message -- subject, text
        # and HTML, snapshotted when the row was queued -- and that copy is what
        # the worker hands the provider. The send-time gate, meanwhile, re-reads
        # the *draft*. A row repointed and not re-rendered is therefore gated on
        # the new words and sends the old ones, which is the one disagreement in
        # this system that reaches a stranger.
        replacement = await session.get(MessageDraft, new_id)
        live = (
            (
                await session.execute(
                    select(
                        OutboxMessage.id,
                        OutboxMessage.message_id,
                        OutboxMessage.payload,
                    ).where(
                        OutboxMessage.draft_id == stale.draft_id,
                        OutboxMessage.status.in_(LIVE_OUTBOX),
                    )
                )
            )
            .tuples()
            .all()
        )
        for outbox_id, message_id, payload in live:
            await session.execute(
                update(OutboxMessage)
                .where(OutboxMessage.id == outbox_id)
                .values(
                    draft_id=new_id,
                    payload=_rendered(payload, replacement),
                )
            )
            await session.execute(
                update(Message)
                .where(Message.id == message_id)
                .values(
                    draft_id=new_id,
                    subject=replacement.subject if replacement else None,
                )
            )
    return outcome


async def redraft_all(
    workspace_id: uuid.UUID,
    *,
    owner_name: str,
    apply: bool,
    limit: int | None = None,
    everything: bool = False,
) -> tuple[RedraftReport, list[str]]:
    """Rewrite every stale draft. Reports without changing anything unless
    ``apply`` is given."""
    report = RedraftReport()
    lines: list[str] = []

    stale = await find_stale(workspace_id, owner_name=owner_name, everything=everything)
    report.stale = len(stale)
    if limit is not None:
        stale = stale[:limit]

    if not apply:
        for item in stale[:20]:
            lines.append(f"  {item.reason:<44} {item.subject[:60]}")
        if len(stale) > 20:
            lines.append(f"  ... and {len(stale) - 20} more")
        return report, lines

    for item in stale:
        refusal = await redraft_one(workspace_id, item)
        if refusal:
            report.refused[refusal] = report.refused.get(refusal, 0) + 1
        else:
            report.rewritten += 1
    return report, lines


__all__ = [
    "LIVE_OUTBOX",
    "REDRAFTABLE",
    "RedraftReport",
    "StaleDraft",
    "find_stale",
    "redraft_all",
    "redraft_one",
    "why_stale",
]
