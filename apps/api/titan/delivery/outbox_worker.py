"""The transactional outbox worker.

The only place in Titan that holds an email provider client, and the only path
by which a message reaches a real inbox (invariants 1, 4, 11).

Protocol per row:

  1. **Lease** with ``SELECT ... FOR UPDATE SKIP LOCKED``, so N workers divide
     the queue with no coordination and no double-claim.
  2. **Re-evaluate the whole authorization chain.** Nothing decided when the row
     was created is trusted: between queueing and sending, the campaign may have
     been paused, the recipient may have replied or unsubscribed, the sender
     identity may have been revoked. This second evaluation is the one that
     actually governs delivery.
  3. **Reserve quota atomically** in the same transaction.
  4. **Send** with a provider idempotency key.
  5. **Record** the provider message id and mark SENT.

Crash safety: a crash between (4) and (5) leaves the row LEASED. The lease
expires and another worker retries -- and because the idempotency key is
identical, the provider collapses the duplicate rather than sending twice. This
is why the key is stored on the row before the first attempt rather than
generated per attempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import hashlib
import logging
import os
import pathlib
import random
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from html import escape as _html_escape
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from titan.config import Settings, get_settings
from titan.db.enums import (
    ContactSource,
    DraftStatus,
    LeadStatus,
    MessageState,
    OutboxStatus,
    SuppressionReason,
)
from titan.db.models import (
    Campaign,
    CampaignPolicy,
    Contact,
    ContactChannel,
    FindingEvidence,
    Lead,
    Message,
    MessageApproval,
    MessageDraft,
    OrganizationLocation,
    OutboxMessage,
    SenderHealthSnapshot,
    SenderIdentity,
    Workspace,
)
from titan.db.session import get_sessionmaker
from titan.delivery import (
    adaptive_limits,
    deliverability,
    quotas,
    sender_health,
    sender_pool,
)
from titan.delivery.carrier_routing import carriers_for_workspace, route_for_market
from titan.delivery.providers.base import (
    Attachment,
    EmailProvider,
    OutboundEmail,
    SendResult,
)
from titan.delivery.suppression import is_suppressed, suppress
from titan.intelligence import domain_health
from titan.intelligence.domain_health import DomainHealth, DomainWindow
from titan.intelligence.greeting import retimed_pair
from titan.intelligence.message_validator import (
    PITCH_MAX_WORDS,
    PITCH_MIN_WORDS,
    pitch_of,
    prohibited_content,
)
from titan.intelligence.sender_auth import is_stale
from titan.notify.operator import NotificationKind, record_notification
from titan.policy.calendars import holiday_on, resolve_country
from titan.policy.engine import Decision, SendContext, evaluate_send
from titan.policy.schedule import SendWindow, local_time, resolve_timezone
from titan.policy.subregions import subregion_for_location

logger = logging.getLogger(__name__)

#: Retry backoff in seconds, indexed by attempt. Jittered at use.
BACKOFF_SCHEDULE = (30, 120, 600, 1800, 7200, 21600)


def _local_frame(ctx: SendContext | None, now: dt.datetime) -> dict[str, object]:
    """When this send landed in the recipient's own day.

    Stamped here because it cannot be recovered later: the clock depends on the
    recipient's timezone, the band their address falls in and the campaign's
    market, and all three can change afterwards. See the migration.

    Every field is None when the clock could not be resolved. Null reads as
    "unknown" to the learning query; a default of midnight would read as a
    thousand messages sent at 3am and would be acted on.
    """
    empty: dict[str, object] = {
        "local_sent_hour": None,
        "local_sent_weekday": None,
        "sent_timezone": None,
    }
    if ctx is None:
        return empty
    timezone = resolve_timezone(
        ctx.recipient_timezone,
        ctx.campaign_region,
        recipient_subregion=ctx.recipient_subregion,
        campaign_subregion=ctx.campaign_subregion,
    )
    local = local_time(now, timezone)
    if local is None:
        return empty
    return {
        "local_sent_hour": local.hour,
        "local_sent_weekday": local.weekday(),
        "sent_timezone": timezone,
    }


def with_compliant_footer(
    email: OutboundEmail, mailing_address: str | None
) -> OutboundEmail:
    """Ensure the postal address is in the body of the message being sent.

    The composer writes the footer when the *draft* is made, from the sender
    identity the campaign named. Two things then drift apart. The sender pool
    chooses the mailbox at send time by remaining headroom, so it need not be
    the one the footer was written from; and an address added to the identities
    afterwards does not reach into bodies already composed.

    Either way the send-time check finds a mailing address configured and
    absent from the text, and blocks -- permanently, because a body is not
    going to change on its own. That is 101 validated, approved messages
    cancelled on the live workspace over one setting being filled in late, plus
    28 more on the unsubscribe header.

    The check itself is right and stays: CAN-SPAM requires the address, and a
    message without one should never leave. What was wrong is having no way to
    *fix* it at the only moment the answer is known. So the footer is repaired
    here against the mailbox actually chosen, immediately before the check runs.

    Absent an address this returns the message untouched, so the block still
    fires when nothing is configured at all -- the repair closes the drift, not
    the requirement.
    """
    address = (mailing_address or "").strip()
    if not address or address in email.text_body:
        return email

    text_body = email.text_body.rstrip("\n") + "\n\n" + address + "\n"
    html_body = email.html_body
    if html_body and address not in html_body:
        block = f'\n    <p style="margin:12px 0 0;font-size:12px;color:#888;">{_html_escape(address)}</p>\n'
        html_body = (
            html_body[: html_body.rfind("</div>")] + block + "</div>"
            if "</div>" in html_body
            else html_body + block
        )

    return dataclasses.replace(email, text_body=text_body, html_body=html_body)


def with_local_greeting(
    email: OutboundEmail, local: dt.datetime | None
) -> OutboundEmail:
    """Set the salutation to the recipient's time of day, at the wire.

    The third instance of the same drift the two functions around this one
    exist for: something the draft could only guess at, known for certain at
    send time. A draft composed on Monday can leave on Thursday, and the send
    window that releases it spans 08:00 to 17:00 local -- so "Good morning"
    chosen at compose time is a coin flip, and a message that greets somebody's
    afternoon as their morning has announced that nobody was there when it was
    sent.

    Unlike the footer repairs this one is cosmetic, so it fails open in every
    direction: an unresolvable timezone, an unrecognised opening line, or a
    salutation that is already correct all return the message untouched. It
    never blocks a send.
    """
    pair = retimed_pair(email.text_body, local)
    if pair is None:
        return email
    existing, replacement = pair
    text_body = email.text_body.replace(existing, replacement, 1)
    html_body = email.html_body
    if html_body:
        # The HTML opens with markup, so the salutation is substituted by exact
        # string rather than matched from the front. Escaped on both sides: the
        # composer writes the greeting through the same escaper, so a name with
        # an ampersand in it is "&amp;" in the markup and would not match raw.
        html_body = html_body.replace(
            _html_escape(existing), _html_escape(replacement), 1
        )
    return dataclasses.replace(email, text_body=text_body, html_body=html_body)


#: How the attachment is introduced in the body.
#:
#: One sentence, and it names the file so a reader whose client hides
#: attachments knows what to look for. Added by the same function that attaches
#: the document, so the message cannot claim one that is not there.
ONE_PAGER_NOTE = (
    "I have attached a one-page summary of how the assessment works, "
    "with references."
)


def carries_one_pager(message_id: uuid.UUID, percent: int) -> bool:
    """Whether this message is in the attachment trial.

    Deterministic on the message's own id. Random sampling would decide again
    on every retry, so a message that failed carrying the PDF could retry
    without it -- two different documents under one idempotency key, and a
    reply-rate comparison measuring the retry path rather than the attachment.

    Hashing the id also makes the cohort stable under a change of percentage:
    the same message always lands on the same number, so raising 10 to 25 adds
    messages to the treated set without moving any out of it, and the trial can
    be widened without discarding what it has already measured.
    """
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    digest = hashlib.sha256(str(message_id).encode()).digest()
    return (int.from_bytes(digest[:4], "big") % 100) < percent


def with_one_pager(email: OutboundEmail, path: str | None) -> OutboundEmail:
    """Attach the one-page brief, and mention it in the body.

    Both, or neither. The words are written at compose time and the file is
    read here, days apart -- so doing them together is what makes "attached"
    true rather than hopeful.

    Fails open: an unset path, a missing file, or an unreadable one returns the
    message untouched and unattached. A brief is not worth failing a send over,
    and a message that goes without it is a working message.
    """
    if not path or not email.text_body:
        return email
    document = pathlib.Path(path)
    try:
        content = document.read_bytes()
    except OSError as error:
        logger.warning("one-pager not attached (%s): %s", path, error)
        return email
    if not content:
        logger.warning("one-pager not attached: %s is empty", path)
        return email

    attachment = Attachment(
        filename=document.name,
        content=content,
        maintype="application",
        subtype="pdf",
    )
    # Above the signature, at the end of the pitch: the reader has finished the
    # argument and this is the offer of more, which is where a supporting
    # document belongs. Below the signature it reads as a footer artefact.
    text_body = _insert_before_signature(
        email.text_body, ONE_PAGER_NOTE, email.from_name
    )
    html_body = email.html_body
    if html_body:
        block = (
            f'\n    <p style="margin:0 0 16px;">{_html_escape(ONE_PAGER_NOTE)}</p>\n'
        )
        marker = '<p style="margin:24px 0 0;color:#444;">'
        html_body = (
            html_body.replace(marker, block + "    " + marker, 1)
            if marker in html_body
            else html_body
        )
    return dataclasses.replace(
        email,
        text_body=text_body,
        html_body=html_body,
        attachments=(*email.attachments, attachment),
    )


def _insert_before_signature(body: str, sentence: str, sender_name: str) -> str:
    """Put ``sentence`` at the end of the pitch, above the signature.

    The signature starts at the sender's own name on its own line -- the same
    boundary ``message_validator.pitch_of`` uses to measure the pitch, so the
    two agree on where the pitch ends by sharing the definition rather than by
    both guessing.
    """
    name = (sender_name or "").strip()
    index = body.find("\n" + name) if name else -1
    if index < 0:
        return body.rstrip("\n") + "\n\n" + sentence + "\n"
    return body[:index].rstrip("\n") + "\n\n" + sentence + body[index:]


def with_one_click_unsubscribe(email: OutboundEmail) -> OutboundEmail:
    """Add the RFC 8058 header when the message already carries an https target.

    The other half of the same drift, and 28 more cancelled messages. A draft
    composed before the sender identity was marked as supporting one-click
    carries a ``List-Unsubscribe`` with no ``List-Unsubscribe-Post`` beside it,
    and the send-time check refuses it -- correctly, because Gmail and Yahoo's
    bulk-sender rules make one-click mandatory and a reader who cannot unsubscribe
    in one click reports spam instead, which is the fastest way to lose a domain.

    Only ever *added*, and only when an ``https`` target is already present:
    the header is meaningless beside a bare ``mailto:``, and inventing a target
    would be inventing an unsubscribe endpoint that does not exist. So this
    cannot manufacture consent to send -- it states a capability the message's
    own existing link already provides.
    """
    if email.list_unsubscribe_post or not email.list_unsubscribe:
        return email
    if "https://" not in email.list_unsubscribe:
        return email
    return dataclasses.replace(email, list_unsubscribe_post="List-Unsubscribe=One-Click")


def worker_identity() -> str:
    """Stable-per-process lease owner, so a crashed worker is identifiable."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True, slots=True)
class ProcessResult:
    outbox_id: uuid.UUID
    outcome: str  # sent | deferred | blocked | retried | failed_permanent
    detail: str | None = None


#: How far back to look for the mailbox's busiest day when bounding today's
#: step up. A week spans a full send pattern including the weekend gap, and is
#: short enough that a mailbox which genuinely stopped is not credited for
#: volume it sent a fortnight ago.
WARMUP_PEAK_WINDOW_DAYS = 7


def _earliest(*moments: dt.datetime | None) -> dt.datetime | None:
    """The earliest of several timestamps, ignoring the ones that are absent.

    Used for warm-up position, where the inputs are Titan's first send through
    a mailbox and the date the provider says it began warming. Neither is
    authoritative alone: Titan's history is a lower bound on a mailbox's age,
    and the provider's record says nothing about whether Titan ever used it.
    Taking the earlier of the two can move a mailbox forward in the ramp and
    never back, which is the safe direction for a value that decides volume.
    """
    known = [m for m in moments if m is not None]
    return min(known) if known else None


#: Rules re-checked at send time rather than trusted from the day of writing.
#:
#: ``draft.validation_passed`` is a stamp, and a draft can stand in the queue
#: for weeks. The same reasoning already governs the recipient address, which is
#: re-checked here rather than trusted from discovery "because the two happen
#: days apart and the list can change in between".
#:
#: The message rules change too, and when they last changed it was not a small
#: difference: **628 of the 745 drafts standing in the queue fail the rules as
#: they now are** -- 366 of them claiming a client base that cannot be named, in
#: three phrasings that were written, approved and sent. Thirty-nine were armed
#: in the outbox waiting only on a carrier plan being paid.
#:
#: Re-checking makes that class of problem impossible instead of fixing one
#: instance: a rule tightened today applies to every message not yet sent,
#: without anybody remembering to go back for the queue.
#:
#: Deliberately narrower than the full validator. The footer rules depend on
#: sender configuration that is re-derived elsewhere in this function, and
#: re-deriving it here to re-check something that has not changed would invent
#: failures rather than find them. What is checked is the content: the rhetoric
#: nothing may contain, and the length a stranger will actually read.
def _payload_is_the_gated_draft(row: OutboxMessage, draft: MessageDraft) -> bool:
    """Whether the words about to be sent are the words just checked.

    Every gate in this worker reads the draft. The provider is handed
    ``row.payload``, a copy rendered when the row was queued. The two are the
    same thing right up until something rewrites one of them -- and then the
    gate is reading one message while a different one goes out.

    Fails *closed*, unlike the content check below. A mismatch is not an error
    that might be a bug in the check; it is two records that definitely
    disagree, and the safe reading of that is that nobody has approved what is
    sitting in the payload.
    """
    payload = row.payload or {}
    if payload.get("text_body") == (draft.body_text or ""):
        return True
    logger.error(
        "queued copy does not match the draft it was gated on; not sending",
        extra={
            "outbox_id": str(row.id),
            "draft_id": str(draft.id),
            "draft_version": draft.version,
        },
    )
    return False


def _still_passes_todays_rules(draft: MessageDraft) -> bool:
    """Whether this body would pass the content rules as they stand now.

    Fails open on an unexpected error, deliberately. This check stops sends, and
    a bug in it must not stop every send -- the stored stamp is the fallback,
    which is exactly where the gate stood before.
    """
    if not draft.validation_passed:
        return False
    body = draft.body_text or ""
    try:
        violation = prohibited_content(body)
        if violation is not None:
            logger.info(
                "draft no longer passes the message rules; not sending",
                extra={
                    "draft_id": str(draft.id),
                    "violation": violation.code.value,
                },
            )
            return False
        words = len(pitch_of(body, get_settings().owner_name).split())
        if not PITCH_MIN_WORDS <= words <= PITCH_MAX_WORDS:
            logger.info(
                "draft is outside the message length band; not sending",
                extra={"draft_id": str(draft.id), "pitch_words": words},
            )
            return False
    except Exception:
        logger.warning(
            "could not re-check a draft at send time; using the stored result",
            extra={"draft_id": str(draft.id)},
        )
        return draft.validation_passed
    return True


class OutboxWorker:
    def __init__(
        self,
        provider: EmailProvider,
        settings: Settings | None = None,
        *,
        owner: str | None = None,
        now_fn: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._settings = settings or get_settings()
        self._owner = owner or worker_identity()
        self._now = now_fn or (lambda: dt.datetime.now(dt.UTC))

    # ------------------------------------------------------------- claiming
    async def claim_batch(self, session: AsyncSession, limit: int) -> list[OutboxMessage]:
        """Atomically claim up to ``limit`` due rows, best lead first.

        SKIP LOCKED is what makes this safe under concurrency: a row already
        locked by another worker is passed over rather than blocking, so
        throughput scales with worker count instead of serialising.

        **Order is by lead score, not arrival.** A mailbox has a daily ceiling,
        so on any day the queue is longer than the ceiling the order decides
        which leads get written to and which wait -- and pure FIFO decides that
        by when discovery happened to crawl them, which is a fact about the
        crawler rather than about the business. Ordering by
        ``leads.latest_score`` spends a scarce ceiling on the best prospects
        first.

        The score is the right and only key to use here.
        :mod:`titan.intelligence.scoring` already folds ``SEVERITY_WEIGHT`` into
        it, so a lead whose findings are severe already scores higher; adding
        severity again on top would count the same evidence twice.

        ``FOR UPDATE OF o`` rather than a bare ``FOR UPDATE``: the join exists
        only to read a score, and locking the lead row would make two workers
        contend over a lead neither of them is changing.

        The join names ``workspace_id`` on both sides. This claim is one of the
        few queries that is legitimately cross-workspace -- one worker drains
        every tenant, so it cannot filter to a single one -- and that is exactly
        why the join has to assert the lead belongs to the same tenant as the
        row. Without it a stale or mistaken ``lead_id`` would read another
        workspace's score and reorder this one's queue by it.
        """
        now = self._now()
        lease_until = now + dt.timedelta(seconds=self._settings.outbox_lease_seconds)

        claim = text(
            """
            WITH claimable AS (
                SELECT o.id
                  FROM outbox_messages o
                  LEFT JOIN leads l
                         ON l.id = o.lead_id
                        AND l.workspace_id = o.workspace_id
                 WHERE (
                        o.status IN ('pending', 'deferred')
                        OR (o.status = 'leased' AND o.leased_until < :now)
                       )
                   AND o.next_attempt_at <= :now
                 ORDER BY l.latest_score DESC NULLS LAST, o.next_attempt_at
                 FOR UPDATE OF o SKIP LOCKED
                 LIMIT :limit
            )
            UPDATE outbox_messages o
               SET status = 'leased',
                   lease_owner = :owner,
                   leased_until = :lease_until,
                   updated_at = now()
              FROM claimable c
             WHERE o.id = c.id
            RETURNING o.id
            """
        )
        rows = await session.execute(
            claim,
            {
                "now": now,
                "limit": limit,
                "owner": self._owner,
                "lease_until": lease_until,
            },
        )
        claimed_ids = [r[0] for r in rows]
        if not claimed_ids:
            return []
        return list(
            (
                await session.execute(
                    select(OutboxMessage).where(OutboxMessage.id.in_(claimed_ids))
                )
            )
            .scalars()
            .all()
        )

    # ------------------------------------------------------------ authorizing
    async def build_context(
        self, session: AsyncSession, row: OutboxMessage
    ) -> tuple[SendContext | None, str | None]:
        """Assemble the policy input by re-reading current state.

        Returns (None, reason) when a referenced record has vanished, which is
        itself a refusal -- a message whose campaign or sender no longer exists
        must not be sent on the strength of what was true yesterday.
        """
        workspace = await session.get(Workspace, row.workspace_id)
        campaign = await session.get(Campaign, row.campaign_id)
        lead = await session.get(Lead, row.lead_id)
        draft = await session.get(MessageDraft, row.draft_id)
        sender = await session.get(SenderIdentity, row.sender_identity_id)
        for label, record in (
            ("workspace", workspace),
            ("campaign", campaign),
            ("lead", lead),
            ("draft", draft),
            ("sender identity", sender),
        ):
            if record is None:
                return None, f"{label} no longer exists"
        assert workspace is not None
        assert campaign is not None
        assert lead is not None
        assert draft is not None
        assert sender is not None

        policy = (
            await session.execute(
                select(CampaignPolicy).where(CampaignPolicy.campaign_id == campaign.id)
            )
        ).scalar_one_or_none()
        if policy is None:
            return None, "campaign has no policy row"

        channel = await session.get(ContactChannel, draft.contact_channel_id)
        if channel is None:
            return None, "contact channel no longer exists"
        contact = await session.get(Contact, channel.contact_id)

        approval = (
            await session.execute(
                select(MessageApproval)
                .where(
                    MessageApproval.draft_id == draft.id,
                    MessageApproval.decision == "approved",
                )
                .order_by(MessageApproval.decided_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        suppression = await is_suppressed(
            session,
            workspace_id=row.workspace_id,
            email=row.to_email_normalized,
            now=self._now(),
        )

        location = (
            await session.execute(
                select(OrganizationLocation)
                .where(OrganizationLocation.organization_id == lead.organization_id)
                .order_by(OrganizationLocation.is_primary.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        domain_health = await self._recipient_domain_health(session, row)

        # Which carrier campaign, and therefore on whose clock. Routed by the
        # recipient's own country rather than by the campaign's, because a
        # campaign that is a business type rather than a city has leads in six
        # markets and one recorded id can serve exactly one of them.
        carriers = await carriers_for_workspace(
            session,
            workspace_id=row.workspace_id,
            provider=self._settings.email_provider,
        )
        route = route_for_market(
            carriers=carriers,
            recipient_country_code=location.country_code if location else None,
            recipient_timezone=location.timezone if location else None,
            campaign_carrier_id=campaign.smartlead_campaign_id,
        )
        if not route.routed_by_market:
            # Not a failure -- it is what every message did before markets had
            # their own carriers -- but it is the case where a lead rides a
            # clock nobody chose for it, so it is said out loud rather than
            # inferred later from where the message ended up.
            logger.info(
                "carrier not chosen by recipient market",
                extra={
                    "outbox_message_id": str(row.id),
                    "reason": route.reason,
                    "carrier_campaign_id": route.campaign_id,
                },
            )

        ctx = SendContext(
            settings=self._settings,
            now=self._now(),
            workspace_mode=workspace.operating_mode,
            workspace_sending_authorized=workspace.sending_authorized,
            campaign_mode=policy.operating_mode,
            campaign_status=campaign.status,
            campaign_sending_authorized=policy.sending_authorized,
            campaign_auto_approve=policy.auto_approve,
            carrier_campaign_id=route.campaign_id,
            min_lead_score=policy.min_lead_score,
            require_verified_email=policy.require_verified_email,
            require_evidence_backed_claims=policy.require_evidence_backed_claims,
            min_evidence_per_message=policy.min_evidence_per_message,
            max_followups=policy.max_followups,
            allowed_contact_sources=frozenset(
                ContactSource(s)
                for s in (policy.allowed_contact_sources or [])
                if s in ContactSource.__members__.values() or _is_member(s)
            ),
            respect_quiet_hours=policy.respect_quiet_hours,
            sender_authorization_errors=tuple(sender.authorization_errors()),
            lead_status=lead.status,
            lead_score=lead.latest_score,
            lead_replied_at=lead.replied_at,
            followups_sent=lead.followups_sent,
            last_contacted_at=lead.last_contacted_at,
            contact_source=channel.source,
            contact_verification=channel.verification_status,
            recipient_email=row.to_email_normalized,
            contact_is_active=channel.is_active,
            recipient_timezone=location.timezone if location else None,
            recipient_domain_health=domain_health,
            recipient_subregion=subregion_for_location(
                location.country_code if location else None,
                location.region if location else None,
                location.longitude if location else None,
            ),
            campaign_subregion=campaign.sub_region,
            recipient_country=location.country_code if location else None,
            recipient_admin_area=location.region if location else None,
            send_window=SendWindow(
                start_hour=policy.send_window_start_hour,
                end_hour=policy.send_window_end_hour,
                days=tuple(int(d) for d in (policy.send_days or ())),
            ),
            campaign_region=campaign.region,
            evidence_count=_evidence_count(draft),
            evidence_captured_at=await _newest_evidence_captured_at(session, draft),
            validation_passed=(
                _still_passes_todays_rules(draft)
                and _payload_is_the_gated_draft(row, draft)
            ),
            provider_idempotency_key=row.provider_idempotency_key,
            approval_decision=approval.decision if approval else None,
            approval_draft_version=approval.draft_version if approval else None,
            draft_version=draft.version,
            approval_expires_at=approval.expires_at if approval else None,
            is_suppressed=suppression is not None,
            suppression_reason=suppression.reason.value if suppression else None,
        )
        # Unused but fetched for the audit trail; keeps the read in one place.
        _ = contact
        return ctx, None

    # ------------------------------------------------------------ processing
    async def process_one(
        self, session: AsyncSession, row: OutboxMessage
    ) -> ProcessResult:
        """Authorize, reserve quota, send, and record -- in one transaction."""
        ctx, missing = await self.build_context(session, row)
        if ctx is None:
            await self._block(session, row, missing or "context unavailable")
            return ProcessResult(row.id, "blocked", missing)

        # Before the decision, not after it. A sender whose authentication has
        # lapsed is refused by evaluate_send below and never reaches the
        # deliverability check -- so capturing there recorded health for exactly
        # the senders that had none of it, and left the broken ones invisible.
        # The mailbox most worth monitoring is the one that has stopped working.
        #
        # It also returns today's adapted ceiling, which the quota reservation
        # below uses in place of the sender's configured limit.
        limit = await self._capture_sender_health(session, row)

        decision = evaluate_send(ctx)
        if not decision.allowed:
            # Quota and quiet hours are temporary; everything else is a block.
            if self._is_temporary(decision):
                await self._defer(
                    session,
                    row,
                    decision.reason_text(),
                    retry_at=self._next_local_opening(ctx),
                )
                return ProcessResult(row.id, "deferred", decision.reason_text())
            await self._block(session, row, decision.reason_text())
            return ProcessResult(row.id, "blocked", decision.reason_text())

        email = self._render(row, decision, ctx)

        # Repaired against the mailbox the pool actually chose, not the one the
        # draft was written from. See with_compliant_footer.
        sender_row = await session.get(SenderIdentity, row.sender_identity_id)
        email = with_compliant_footer(
            email, sender_row.mailing_address if sender_row else None
        )
        email = with_one_click_unsubscribe(email)
        # The brief, for the share of messages in the trial. Before the
        # greeting repair so the greeting is decided on the body that is
        # actually going out.
        if carries_one_pager(row.id, self._settings.one_pager_sample_percent):
            email = with_one_pager(email, self._settings.one_pager_attachment_path)
        # Last, and after the footer repairs, so the greeting is decided on the
        # body that is actually going out.
        email = with_local_greeting(email, self._recipient_local_time(ctx))

        # Deliverability is checked at the send boundary, alongside policy.
        # A message that would be filtered is not "sent with a warning" -- it
        # is a message that damages the domain for every later message, so it
        # is stopped here.
        #
        # This runs *before* the quota reservation on purpose. Quota counts
        # sends, and a message stopped here is not one; reserving first meant a
        # blocked message still spent a unit of the workspace, campaign, sender
        # and recipient-domain allowance for the day.
        placement = await self._check_deliverability(session, row, email)
        if not placement.ok:
            reasons = "; ".join(s.detail for s in placement.blocking)
            if any(
                s.code
                in {
                    "warmup_limit_reached",
                    "complaint_rate_exceeded",
                    "bounce_rate_exceeded",
                }
                for s in placement.blocking
            ):
                # Temporary: volume or reputation. Defer rather than discard --
                # and all three of these are facts about *this mailbox*, not
                # about the recipient or the hour, so another mailbox in the
                # pool may be able to carry it today.
                recorded = await self._defer_or_repin(
                    session, row, f"deliverability: {reasons}"
                )
                return ProcessResult(row.id, "deferred", recorded)
            await self._block(session, row, f"deliverability: {reasons}")
            return ProcessResult(row.id, "blocked", reasons)

        # Last thing before the provider call, so every refusal above this line
        # costs nothing from the day's allowance.
        outcome = await self._reserve_quota(session, row, limit)
        if not outcome.granted:
            reason = outcome.reason or "quota exhausted"
            # Only the sender scope is worth moving for. A workspace or campaign
            # budget is spent from every mailbox equally, and a recipient-domain
            # limit follows the recipient -- re-pinning against either would just
            # be the same refusal from a different address.
            if outcome.exhausted_scope is quotas.QuotaScope.SENDER:
                reason = await self._defer_or_repin(session, row, reason)
            else:
                await self._defer(session, row, reason)
            return ProcessResult(row.id, "deferred", reason)

        try:
            result = await self._provider.send(email)
        except Exception as exc:  # provider client raised, e.g. process crash
            # The quota reservation is deliberately NOT released here. A client
            # exception can be raised after the provider accepted the message
            # (a lost response, a timeout on the read), so this outcome is
            # ambiguous. Counting a send that happened costs one message of
            # headroom; not counting one puts real mail over the daily cap.
            logger.warning(
                "provider raised during send", extra={"outbox_id": str(row.id)}
            )
            await self._schedule_retry(session, row, f"{type(exc).__name__}: {exc}")
            return ProcessResult(row.id, "retried", str(exc))

        # Read from the envelope that actually left, not by re-running the
        # sampling decision: recomputing would ask today's percentage about a
        # message already sent, and answer wrong either side of a change.
        return await self._record(
            session, row, result, ctx, one_pager_attached=bool(email.attachments)
        )

    async def _recipient_domain_health(
        self, session: AsyncSession, row: OutboxMessage
    ) -> DomainHealth:
        """How this recipient's domain has behaved, read now rather than at discovery.

        The bounce engine classifies a domain when a contact is first found and
        stores the verdict on the contact channel. That is the right place for
        it -- it stops a bad address being kept at all -- but the stored verdict
        is a snapshot, and this message may have been drafted, approved and
        queued weeks later. A complaint that arrived this morning has to stop the
        mail waiting for that domain today, and only a live read does that.

        The same shape as the campaign policy re-read a few lines up, and for the
        same reason: pausing a campaign stops mail already queued, and so should
        a domain going bad.

        A failure returns UNKNOWN, which denies nothing. This is one check among
        several and losing it degrades the decision; raising here would strand a
        message the other gates had already cleared.
        """
        window = dt.timedelta(days=domain_health.WINDOW_DAYS)
        try:
            stats = (
                await session.execute(
                    text(
                        """
                        SELECT
                          count(*) FILTER (WHERE sent_at IS NOT NULL)       AS sent,
                          count(*) FILTER (WHERE delivered_at IS NOT NULL)  AS delivered,
                          count(*) FILTER (WHERE bounced_at IS NOT NULL)    AS bounced,
                          count(*) FILTER (WHERE complained_at IS NOT NULL) AS complained
                          FROM messages
                         WHERE workspace_id = :workspace
                           AND to_domain = :domain
                           AND created_at >= :since
                        """
                    ),
                    {
                        "workspace": row.workspace_id,
                        "domain": row.to_domain,
                        "since": self._now() - window,
                    },
                )
            ).one()
        except Exception:
            logger.warning(
                "recipient domain health unavailable; the check is skipped",
                extra={"outbox_id": str(row.id), "domain": row.to_domain},
            )
            return DomainHealth.UNKNOWN

        return domain_health.classify(
            DomainWindow(
                domain=row.to_domain,
                sent=int(stats.sent or 0),
                delivered=int(stats.delivered or 0),
                bounced=int(stats.bounced or 0),
                complained=int(stats.complained or 0),
            )
        )

    async def _capture_sender_health(
        self, session: AsyncSession, row: OutboxMessage
    ) -> adaptive_limits.LimitDecision | None:
        """Classify this mailbox, record the day's snapshot, and set today's ceiling.

        One method because it is one set of facts. Splitting the classification
        from the persistence would gather the same aggregates twice and let the
        throttle and the history disagree about what health the mailbox was in
        when the message went out.

        Reads and classification happen here; only the write is inside a
        savepoint. That ordering matters: the returned ceiling governs how much
        this mailbox may send today, and it has to survive a failure to write
        history. Losing the audit trail is a nuisance; losing the throttle would
        let a degraded mailbox send at full volume.

        Returns None only when the sender has vanished, in which case the caller
        falls back to the configured limit -- the number a human chose, which is
        the right answer when Titan knows nothing.
        """
        sender = await session.get(SenderIdentity, row.sender_identity_id)
        if sender is None:
            return None
        now = self._now()
        since = now - dt.timedelta(days=30)
        day_start = dt.datetime.combine(now.date(), dt.time.min, tzinfo=dt.UTC)

        stats = (
            await session.execute(
                text(
                    """
                    SELECT
                      count(*) FILTER (WHERE sent_at IS NOT NULL)       AS sent,
                      count(*) FILTER (WHERE delivered_at IS NOT NULL)  AS delivered,
                      -- Hard and unknown, never soft. See
                      -- titan.delivery.bounces.COUNTS_AGAINST_REPUTATION
                      -- for why, and an invariant test that keeps every
                      -- copy of this predicate saying the same thing.
                      count(*) FILTER (WHERE bounced_at IS NOT NULL AND bounce_kind IS DISTINCT FROM 'soft')
                                                                        AS bounced,
                      count(*) FILTER (WHERE complained_at IS NOT NULL) AS complained,
                      min(sent_at)                                      AS first_send_at,
                      count(*) FILTER (WHERE sent_at >= :day_start)     AS sent_today
                      FROM messages
                     WHERE workspace_id = :workspace
                       AND sender_identity_id = :sender
                       AND created_at >= :since
                    """
                ),
                {
                    "workspace": row.workspace_id,
                    "sender": row.sender_identity_id,
                    "since": since,
                    "day_start": day_start,
                },
            )
        ).one()

        throughput = (
            await session.execute(
                text(
                    """
                    SELECT
                      count(*) FILTER (
                        WHERE sent_at IS NOT NULL OR attempt_count > 0
                      )                             AS attempted,
                      coalesce(sum(attempt_count), 0) AS retries,
                      count(*) FILTER (WHERE status = 'deferred') AS deferred
                      FROM outbox_messages
                     WHERE workspace_id = :workspace
                       AND sender_identity_id = :sender
                       AND created_at >= :since
                    """
                ),
                {
                    "workspace": row.workspace_id,
                    "sender": row.sender_identity_id,
                    "since": since,
                },
            )
        ).one()

        # Same rule as the pool: whichever is earlier, Titan's first send or
        # the provider's warm-up start. Reading it differently here than in
        # selection is how a mailbox gets chosen for a batch it is then refused
        # at the gate.
        first_send_at = _earliest(stats.first_send_at, sender.warmup_started_at)
        attempted = int(throughput.attempted or 0)
        retries = int(throughput.retries or 0)
        warmup_limit = deliverability.warmup_limit(
            first_send_at=first_send_at, now=now, target=sender.daily_send_limit
        )
        warmup_day = (
            None
            if warmup_limit is None
            else deliverability.warmup_day(first_send_at, now)
        )

        snapshot = sender_health.SenderSnapshot(
            sender_identity_id=str(sender.id),
            sending_domain=sender.sending_domain,
            captured_on=now.date(),
            domain_verified=sender.domain_verified,
            spf_ok=sender.spf_ok,
            dkim_ok=sender.dkim_ok,
            dmarc_ok=sender.dmarc_ok,
            auth_stale=is_stale(sender.last_verified_at),
            # ``days_since_bounce`` is deliberately left unset here, unlike the
            # send-time gate in ``_check_deliverability``. The health snapshot
            # should keep saying BLOCKED: the mailbox really did bounce 5.32% of
            # its list, and that is the true state of it. Recovery is expressed
            # as an allowance on top of a blocked mailbox -- adaptive_limits'
            # probation, five a day -- not by relabelling it healthy. Feeding
            # this would soften the verdict to WATCH, whose 0.6 factor would
            # hand a recovering mailbox 30 sends a day instead of five.
            window=deliverability.ReputationWindow(
                sent=int(stats.sent or 0),
                delivered=int(stats.delivered or 0),
                hard_bounced=int(stats.bounced or 0),
                complained=int(stats.complained or 0),
            ),
            attempts=attempted + retries,
            retries=retries,
            deferred=int(throughput.deferred or 0),
            sent_today=int(stats.sent_today or 0),
            warmup_day=warmup_day,
            warmup_limit=warmup_limit,
        )
        status = sender_health.classify(snapshot)

        # Earlier days only, newest first. Read before the upsert, or today's own
        # row is the most recent and every comparison is against itself.
        history = tuple(
            sender_health.SenderHealth(value)
            for value in (
                await session.execute(
                    text(
                        """
                        SELECT status FROM sender_health_snapshots
                         WHERE workspace_id = :workspace
                           AND sender_identity_id = :sender
                           AND captured_on < :today
                         ORDER BY captured_on DESC
                         LIMIT :lookback
                        """
                    ),
                    {
                        "workspace": row.workspace_id,
                        "sender": row.sender_identity_id,
                        "today": now.date(),
                        "lookback": adaptive_limits.RECOVERY_LOOKBACK_DAYS,
                    },
                )
            ).scalars()
        )

        # How long since this mailbox last hard-bounced. None means never, which
        # is not the same as "recently" and must not be read as it -- see
        # adaptive_limits.PROBATION_VOLUME for what this governs.
        last_bounce = await session.scalar(
            text(
                """
                SELECT max(bounced_at) FROM messages
                 WHERE workspace_id = :workspace
                   AND sender_identity_id = :sender
                   AND bounced_at IS NOT NULL
                   AND bounce_kind IS DISTINCT FROM 'soft'
                """
            ),
            {"workspace": row.workspace_id, "sender": sender.id},
        )
        days_since_bounce = (
            None if last_bounce is None else max(0, (now - last_bounce).days)
        )

        # Today's own numbers, for the same-day breaker. The thirty-day window
        # above judges the mailbox; this protects it. Every block this estate
        # has taken was one bad afternoon that the window then carried for a
        # month, so the day has to be able to stop itself.
        today_row = (
            await session.execute(
                text(
                    """
                    SELECT
                      count(*) FILTER (WHERE sent_at >= :midnight) AS sent_today,
                      count(*) FILTER (
                        WHERE bounced_at >= :midnight
                          AND bounced_at IS NOT NULL
                          AND bounce_kind IS DISTINCT FROM 'soft'
                      ) AS bounced_today
                    FROM messages
                     WHERE workspace_id = :workspace
                       AND sender_identity_id = :sender
                    """
                ),
                {
                    "workspace": row.workspace_id,
                    "sender": row.sender_identity_id,
                    "midnight": now.replace(hour=0, minute=0, second=0, microsecond=0),
                },
            )
        ).one()

        decision = adaptive_limits.daily_limit(
            sender.daily_send_limit,
            recent=(status, *history),
            warmup_limit=warmup_limit,
            days_since_bounce=days_since_bounce,
            sent_today=int(today_row.sent_today or 0),
            bounced_today=int(today_row.bounced_today or 0),
        )
        if decision.same_day_stop:
            logger.warning(
                "mailbox stopped for the day by its own bounces",
                extra={
                    "sender": str(sender.id),
                    "sent_today": int(today_row.sent_today or 0),
                    "bounced_today": int(today_row.bounced_today or 0),
                },
            )
        if decision.reduced:
            logger.info(
                "sender daily limit adapted",
                extra={
                    "sender_id": str(sender.id),
                    "effective_limit": decision.effective,
                    "configured_limit": decision.configured,
                    "health": status.value,
                },
            )

        # A SAVEPOINT, not just a try/except. This shares the caller's
        # transaction, and PostgreSQL aborts the whole transaction on any failed
        # statement -- so catching the exception would leave the session
        # poisoned and every statement after it, including the send bookkeeping,
        # would fail. Catching without this would make the send *more* fragile
        # than not recording health at all, which is the opposite of the intent.
        try:
            async with session.begin_nested():
                await self._write_sender_health(
                    session,
                    row,
                    sender=sender,
                    snapshot=snapshot,
                    status=status,
                    previous=history[0] if history else None,
                    now=now,
                )
        except Exception:
            logger.warning(
                "could not record sender health; the send decision is unaffected",
                extra={"outbox_id": str(row.id), "sender_id": str(sender.id)},
            )
        return decision

    async def _write_sender_health(
        self,
        session: AsyncSession,
        row: OutboxMessage,
        *,
        sender: SenderIdentity,
        snapshot: sender_health.SenderSnapshot,
        status: sender_health.SenderHealth,
        previous: sender_health.SenderHealth | None,
        now: dt.datetime,
    ) -> None:
        """The snapshot write and its alert. Always called inside a savepoint."""
        values: dict[str, Any] = {
            "status": status.value,
            "domain_verified": snapshot.domain_verified,
            "spf_ok": snapshot.spf_ok,
            "dkim_ok": snapshot.dkim_ok,
            "dmarc_ok": snapshot.dmarc_ok,
            "auth_stale": snapshot.auth_stale,
            "window_sent": snapshot.window.sent,
            "window_delivered": snapshot.window.delivered,
            "window_bounced": snapshot.window.hard_bounced,
            "window_complained": snapshot.window.complained,
            "attempts": snapshot.attempts,
            "retries": snapshot.retries,
            "deferred": snapshot.deferred,
            "sent_today": snapshot.sent_today,
            "warmup_day": snapshot.warmup_day,
            "warmup_limit": snapshot.warmup_limit,
            "reasons": list(sender_health.reasons(snapshot)),
        }
        await session.execute(
            pg_insert(SenderHealthSnapshot.__table__)  # type: ignore[arg-type]
            .values(
                workspace_id=row.workspace_id,
                sender_identity_id=sender.id,
                sending_domain=sender.sending_domain,
                captured_on=snapshot.captured_on,
                **values,
            )
            .on_conflict_do_update(
                constraint="uq_sender_health_day",
                set_={**values, "updated_at": now},
            )
        )

        if sender_health.should_alert(status, previous):
            await record_notification(
                session,
                workspace_id=row.workspace_id,
                kind=NotificationKind.DELIVERABILITY_ALERT,
                title=f"{sender.from_email} is {status.value}",
                # Keyed on the transition, not on the day: a mailbox that stays
                # degraded for a fortnight is one alert, not fourteen.
                dedupe_key=(
                    f"sender-health:{sender.id}:"
                    f"{previous.value if previous else 'new'}->{status.value}"
                ),
                description="; ".join(sender_health.reasons(snapshot)) or None,
                now=now,
            )

    async def _check_deliverability(
        self, session: AsyncSession, row: OutboxMessage, email: OutboundEmail
    ) -> deliverability.DeliverabilityReport:
        """Assess inbox placement for this specific message."""
        sender = await session.get(SenderIdentity, row.sender_identity_id)
        now = self._now()

        # Trailing thirty days: a fresh window would let a bad week be forgotten
        # too quickly, and a lifetime window would never recover.
        #
        # Measured per *mailbox*, not per sending domain. This comment used to
        # claim the domain and the query has always said `sender_identity_id`,
        # and the difference is not cosmetic. Receivers judge the domain, so
        # per-domain is the truer model of the risk -- but it is also strictly
        # harsher, and switching would currently take the workspace to zero:
        # outreach@ carries 5 bounces over 94 sends and sales@ 0 over 30, so a
        # domain-wide rate of 4% would pause the clean mailbox along with the
        # dirty one.
        #
        # Per-mailbox is therefore a deliberate trade and not an oversight: it
        # quarantines the mailbox that produced the bounces and lets a clean one
        # keep working. What it does not do is make the clean mailbox safe --
        # arslanvuzmallone.com carries that history whatever this query counts,
        # and sales@ is sending on it.
        since = now - dt.timedelta(days=30)
        stats = (
            await session.execute(
                text(
                    """
                    SELECT
                      count(*) FILTER (WHERE sent_at IS NOT NULL)      AS sent,
                      count(*) FILTER (WHERE delivered_at IS NOT NULL) AS delivered,
                      -- Hard and unknown, never soft.
                      count(*) FILTER (WHERE bounced_at IS NOT NULL AND bounce_kind IS DISTINCT FROM 'soft')
                                                                       AS bounced,
                      count(*) FILTER (WHERE complained_at IS NOT NULL) AS complained
                      FROM messages
                     WHERE workspace_id = :workspace
                       AND sender_identity_id = :sender
                       AND created_at >= :since
                    """
                ),
                {
                    "workspace": row.workspace_id,
                    "sender": row.sender_identity_id,
                    "since": since,
                },
            )
        ).one()

        # Deliberately *not* bounded by ``since``. The question this answers is
        # "has anything bounced lately", and the newest bounce is the newest
        # bounce whether or not it falls inside the rate window -- clamping it
        # to the window would report a mailbox as quiet the moment its last
        # bounce aged out, which is the opposite of the check's purpose.
        last_bounce = await session.scalar(
            text(
                """
                SELECT max(bounced_at) FROM messages
                 WHERE workspace_id = :workspace
                   AND sender_identity_id = :sender
                   AND bounced_at IS NOT NULL
                   AND bounce_kind IS DISTINCT FROM 'soft'
                """
            ),
            {"workspace": row.workspace_id, "sender": row.sender_identity_id},
        )
        days_since_bounce = (
            None if last_bounce is None else max(0, (now - last_bounce).days)
        )

        # The same rule ``_capture_sender_health`` uses, and it did not used to
        # be. This read Titan's first send alone while the health snapshot took
        # ``_earliest`` of that and the provider's warm-up start, so the two
        # disagreed about the same mailbox on the same day: the snapshot for
        # sales@ said day 13, allowance 25, and this gate enforced day 2,
        # allowance 6. An operator reading the dashboard was told a number the
        # sender was never going to be given.
        #
        # ``_earliest`` is the documented rule and the one kept. What stops it
        # handing a day-13 allowance to a mailbox that has never sent more than
        # six is ``recent_peak_sends`` below, which bounds the jump rather than
        # the destination.
        titan_first_send = (
            await session.execute(
                text(
                    "SELECT min(sent_at) FROM messages "
                    "WHERE workspace_id = :workspace AND sender_identity_id = :sender "
                    "AND sent_at IS NOT NULL"
                ),
                {"workspace": row.workspace_id, "sender": row.sender_identity_id},
            )
        ).scalar_one_or_none()
        first_send_at = _earliest(
            titan_first_send, sender.warmup_started_at if sender else None
        )

        # Highest single day in the trailing week, **excluding today**. This is
        # a bound on day-over-day growth, so today's own sends cannot be part
        # of the evidence for how much may be sent today.
        #
        # Including them made the bound raise itself as it was consumed: six
        # sent permitted twelve, the twelfth made the peak twelve which
        # permitted twenty-four, and one batch walked a mailbox from six to its
        # full ramp allowance in a single burst -- exactly the jump the bound
        # exists to prevent, produced by the bound.
        #
        # None when nothing was sent in the window: "no evidence", not
        # "evidence of zero". A mailbox quarantined for a week by the
        # reputation gate must not also be throttled to the floor by its own
        # quarantine, or it could never send its way back out.
        recent_peak_sends = (
            await session.execute(
                text(
                    "SELECT max(c) FROM ("
                    "  SELECT count(*) AS c FROM messages"
                    "  WHERE workspace_id = :workspace"
                    "    AND sender_identity_id = :sender"
                    "    AND sent_at >= :since"
                    "    AND sent_at < :today_start"
                    "  GROUP BY (sent_at AT TIME ZONE 'UTC')::date"
                    ") d"
                ),
                {
                    "workspace": row.workspace_id,
                    "sender": row.sender_identity_id,
                    "since": now - dt.timedelta(days=WARMUP_PEAK_WINDOW_DAYS),
                    "today_start": dt.datetime.combine(
                        now.date(), dt.time.min, tzinfo=dt.UTC
                    ),
                },
            )
        ).scalar_one_or_none()

        sent_today = int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM messages "
                        "WHERE workspace_id = :workspace AND sender_identity_id = :s "
                        "AND sent_at >= :start"
                    ),
                    {
                        "workspace": row.workspace_id,
                        "s": row.sender_identity_id,
                        "start": dt.datetime.combine(
                            now.date(), dt.time.min, tzinfo=dt.UTC
                        ),
                    },
                )
            ).scalar_one()
            or 0
        )

        headers = dict(email.headers)
        if email.list_unsubscribe:
            headers["List-Unsubscribe"] = email.list_unsubscribe
        if email.list_unsubscribe_post:
            headers["List-Unsubscribe-Post"] = email.list_unsubscribe_post

        return deliverability.evaluate(
            deliverability.DeliverabilityContext(
                subject=email.subject,
                text_body=email.text_body,
                html_body=email.html_body,
                from_name=email.from_name,
                mailing_address=sender.mailing_address if sender else None,
                headers=headers,
                reputation=deliverability.ReputationWindow(
                    sent=int(stats.sent or 0),
                    delivered=int(stats.delivered or 0),
                    hard_bounced=int(stats.bounced or 0),
                    complained=int(stats.complained or 0),
                    days_since_bounce=days_since_bounce,
                ),
                first_send_at=first_send_at,
                sent_today=sent_today,
                recent_peak_sends=recent_peak_sends,
                now=now,
                warmup_target=sender.daily_send_limit if sender else 0,
                attachments=tuple(email.attachments),
            )
        )

    def _is_temporary(self, decision: Decision) -> bool:
        from titan.policy.engine import DenyCode

        temporary = {
            DenyCode.QUOTA_EXHAUSTED,
            DenyCode.QUIET_HOURS,
            DenyCode.OUTSIDE_SEND_WINDOW,
            DenyCode.SPACING,
        }
        codes = set(decision.codes)
        return bool(codes) and codes <= temporary

    async def _quota_requests(
        self,
        session: AsyncSession,
        row: OutboxMessage,
        limit: adaptive_limits.LimitDecision | None = None,
    ) -> list[quotas.QuotaRequest]:
        """The four scopes one send consumes.

        Built in one place so a release returns units to exactly the scopes the
        reservation took them from -- a release that reconstructed the list
        differently would silently corrupt the counters. Only the scope *keys*
        have to match for that: the release statement never reads ``limit``, so
        passing an adapted one on reservation and omitting it on release is
        safe, and omitting it is what the release path does.

        The sender scope is the only one that adapts. Workspace and campaign
        limits are business budgets a human set for reasons health knows nothing
        about, and the recipient-domain limit is already backed by a hard gate --
        a domain whose delivery record has gone bad refuses the send outright in
        evaluate_send, and a second mechanism throttling the same thing would be
        two rules for one decision.
        """
        settings = self._settings
        policy = (
            await session.execute(
                select(CampaignPolicy).where(
                    CampaignPolicy.campaign_id == row.campaign_id
                )
            )
        ).scalar_one()
        workspace = await session.get(Workspace, row.workspace_id)
        sender = await session.get(SenderIdentity, row.sender_identity_id)

        return [
            quotas.QuotaRequest(
                quotas.QuotaScope.WORKSPACE,
                str(row.workspace_id),
                workspace.daily_send_limit
                if workspace
                else settings.quota_workspace_daily,
            ),
            quotas.QuotaRequest(
                quotas.QuotaScope.CAMPAIGN,
                str(row.campaign_id),
                policy.daily_send_limit,
            ),
            quotas.QuotaRequest(
                quotas.QuotaScope.SENDER,
                str(row.sender_identity_id),
                limit.effective
                if limit is not None
                else (sender.daily_send_limit if sender else settings.quota_sender_daily),
            ),
            quotas.QuotaRequest(
                quotas.QuotaScope.RECIPIENT_DOMAIN,
                row.to_domain,
                policy.recipient_domain_daily_limit,
            ),
        ]

    async def _reserve_quota(
        self,
        session: AsyncSession,
        row: OutboxMessage,
        limit: adaptive_limits.LimitDecision | None = None,
    ) -> quotas.QuotaOutcome:
        return await quotas.reserve_all(
            session,
            workspace_id=row.workspace_id,
            requests=await self._quota_requests(session, row, limit),
            window_date=self._now().date(),
        )

    async def _release_quota(self, session: AsyncSession, row: OutboxMessage) -> None:
        """Give back the reservation for a send the provider refused."""
        await quotas.release_all(
            session,
            workspace_id=row.workspace_id,
            requests=await self._quota_requests(session, row),
            window_date=self._now().date(),
        )

    def _render(
        self, row: OutboxMessage, decision: Decision, ctx: SendContext | None = None
    ) -> OutboundEmail:
        payload = row.payload or {}
        return OutboundEmail(
            to_email=payload.get("to_email", row.to_email_normalized),
            from_email=payload["from_email"],
            from_name=payload["from_name"],
            reply_to=payload["reply_to"],
            subject=payload["subject"],
            text_body=payload["text_body"],
            html_body=payload.get("html_body"),
            idempotency_key=row.provider_idempotency_key,
            list_unsubscribe=payload.get("list_unsubscribe"),
            list_unsubscribe_post=payload.get("list_unsubscribe_post"),
            headers=payload.get("headers") or {},
            tags={"campaign": str(row.campaign_id), "lead": str(row.lead_id)},
            carrier_campaign_id=ctx.carrier_campaign_id if ctx else None,
        )

    async def _record(
        self,
        session: AsyncSession,
        row: OutboxMessage,
        result: SendResult,
        ctx: SendContext | None = None,
        *,
        one_pager_attached: bool | None = None,
    ) -> ProcessResult:
        now = self._now()
        if result.accepted:
            row.status = OutboxStatus.SENT
            row.sent_at = now
            row.lease_owner = None
            row.leased_until = None
            # The row got past whatever once held it up, so the reason it was
            # held has stopped being true. Leaving it behind is how 13 delivered
            # messages came to carry "outside the campaign's send window" as
            # their stated outcome -- and how the failure register came to
            # describe causes that had already been resolved.
            row.blocked_reason = None
            await session.execute(
                update(Message)
                .where(Message.id == row.message_id, Message.state_rank < 20)
                .values(
                    state=MessageState.SENT,
                    state_rank=20,
                    state_event_at=now,
                    provider_message_id=result.provider_message_id,
                    sent_at=now,
                    # From the envelope that actually left, not by re-running
                    # the sampling decision: recomputing would ask today's
                    # percentage about a message already sent, and answer wrong
                    # for everything either side of a change.
                    one_pager_attached=one_pager_attached,
                    **_local_frame(ctx, now),
                )
            )
            await session.execute(
                update(Lead)
                .where(Lead.id == row.lead_id)
                .values(
                    last_contacted_at=now,
                    status=LeadStatus.CONTACTED,
                    followups_sent=Lead.followups_sent + 1,
                )
            )
            await session.execute(
                update(MessageDraft)
                .where(MessageDraft.id == row.draft_id)
                .values(status=DraftStatus.QUEUED)
            )
            return ProcessResult(row.id, "sent")

        # Nothing below this point was delivered: the provider answered and
        # refused. Give the reservation back so a rejected message does not
        # spend one of the day's sends (see quotas.release_all).
        await self._release_quota(session, row)

        # Permanent recipient failure: suppress so no future campaign retries it.
        if result.is_permanent_failure:
            row.status = OutboxStatus.FAILED_PERMANENT
            row.last_error = result.error_detail
            row.lease_owner = None
            await suppress(
                session,
                workspace_id=row.workspace_id,
                email_or_domain=row.to_email_normalized,
                reason=SuppressionReason.HARD_BOUNCE,
                source="provider_send_rejection",
                source_reference=str(row.id),
                now=now,
            )
            await session.execute(
                update(Message)
                .where(Message.id == row.message_id, Message.state_rank < 70)
                .values(state=MessageState.BOUNCED, state_rank=70, state_event_at=now)
            )
            return ProcessResult(row.id, "failed_permanent", result.error_detail)

        # Configuration failure: stop, but do NOT punish the recipient.
        if result.is_configuration_failure:
            row.status = OutboxStatus.FAILED_PERMANENT
            row.blocked_reason = result.error_detail
            row.lease_owner = None
            logger.error(
                "outbox halted on a configuration error; recipient not suppressed",
                extra={"outbox_id": str(row.id), "kind": result.error_kind},
            )
            return ProcessResult(row.id, "failed_permanent", result.error_detail)

        await self._schedule_retry(
            session, row, result.error_detail, result.retry_after_seconds
        )
        return ProcessResult(row.id, "retried", result.error_detail)

    async def _schedule_retry(
        self,
        session: AsyncSession,
        row: OutboxMessage,
        error: str | None,
        retry_after: int | None = None,
    ) -> None:
        row.attempt_count += 1
        row.last_error = (error or "")[:2000]
        row.lease_owner = None
        row.leased_until = None

        if row.attempt_count >= self._settings.outbox_max_attempts:
            row.status = OutboxStatus.FAILED_PERMANENT
            row.blocked_reason = f"exhausted {row.attempt_count} attempts"
            return

        index = min(row.attempt_count - 1, len(BACKOFF_SCHEDULE) - 1)
        base = retry_after if retry_after is not None else BACKOFF_SCHEDULE[index]
        # Full jitter: without it, a fleet that failed together retries together
        # and reproduces the original overload.
        delay = random.uniform(base * 0.5, base * 1.5)
        # Back to PENDING means nothing is blocking it any more; the live error
        # is in last_error. A leftover blocked_reason here would outlive the
        # condition that wrote it and describe a row that is simply waiting.
        row.blocked_reason = None
        row.status = OutboxStatus.PENDING
        row.next_attempt_at = self._now() + dt.timedelta(seconds=delay)

    def _next_window_open(self, ctx: SendContext) -> dt.datetime | None:
        """When this campaign's window next opens for this recipient.

        Returns None when there is no window, no clock, or the window is already
        open -- in which case the deferral was for some other reason (quota,
        spacing) and the caller falls back to the next UTC window.

        Without this a message refused at 18:00 local retries at the next UTC
        midnight, which for a Pacific recipient is the middle of their afternoon
        and for a Sydney one is mid-morning -- neither is the start of the
        working day the window was configured to protect, and a message refused
        on Friday evening would wake up and be refused again every night of the
        weekend.
        """
        if ctx.send_window is None or not ctx.send_window.is_usable:
            return None
        local = self._recipient_local_time(ctx)
        if local is None:
            return None
        country = resolve_country(ctx.recipient_country, ctx.campaign_region)
        lookup = (
            (
                lambda day: holiday_on(
                    day, country=country, subdiv=ctx.recipient_admin_area
                )
            )
            if country
            else None
        )
        opens = ctx.send_window.next_open_from(local, lookup)
        if opens is None or opens <= local:
            return None
        return opens.astimezone(dt.UTC)

    def _next_quiet_hours_end(self, ctx: SendContext) -> dt.datetime | None:
        """When quiet hours next end on the recipient's clock.

        None when quiet hours are off, when the recipient has no resolvable
        clock, or when it is not currently quiet hours for them -- in each case
        the deferral was for something else and this is not the gate to wait on.

        Without this, quiet hours were the one temporary refusal with no retry
        time of its own, so it fell through to the next *UTC* window. For a
        European recipient that is 00:00-01:00 UTC, which is 01:00-03:00 on
        their clock: still inside quiet hours, deferred again, to the next UTC
        midnight, forever. It is a closed loop rather than a delay -- every
        European lead first attempted before 08:00 local was in it, and the
        oldest one found in production had been going round for nine days
        without ever being counted as failed.
        """
        settings = self._settings
        if not settings.quiet_hours_enabled:
            return None
        local = self._recipient_local_time(ctx)
        if local is None:
            return None
        start, end = settings.quiet_hours_start, settings.quiet_hours_end
        if start == end:
            return None
        hour = local.hour
        inside = (start <= hour < end) if start < end else (hour >= start or hour < end)
        if not inside:
            return None
        opens = local.replace(hour=end, minute=0, second=0, microsecond=0)
        if opens <= local:
            opens += dt.timedelta(days=1)
        return opens.astimezone(dt.UTC)

    def _next_local_opening(self, ctx: SendContext) -> dt.datetime | None:
        """The earliest instant both time-of-day gates would let this through.

        The *later* of the two, because each is a floor rather than a schedule:
        a campaign window opening at 08:00 is no use to a recipient still
        inside quiet hours, and the end of quiet hours is no use on a day the
        campaign does not send at all. None when neither gate is what deferred
        the message, and the caller falls back to the next UTC window.
        """
        candidates = [
            when
            for when in (self._next_window_open(ctx), self._next_quiet_hours_end(ctx))
            if when is not None
        ]
        return max(candidates) if candidates else None

    @staticmethod
    def _recipient_local_time(ctx: SendContext) -> dt.datetime | None:
        """Now, on the recipient's clock, or None when it cannot be resolved.

        The same resolution ``_next_window_open`` does, named once. None is a
        real answer -- ``resolve_timezone`` refuses rather than guessing, and a
        greeting is not worth defaulting to UTC for.
        """
        timezone = resolve_timezone(
            ctx.recipient_timezone,
            ctx.campaign_region,
            recipient_subregion=ctx.recipient_subregion,
            campaign_subregion=ctx.campaign_subregion,
        )
        return local_time(ctx.now, timezone)

    async def _defer(
        self,
        session: AsyncSession,
        row: OutboxMessage,
        reason: str,
        *,
        retry_at: dt.datetime | None = None,
    ) -> None:
        """Quota/quiet-hours deferral. Never a permanent failure (mission 15.4)."""
        row.status = OutboxStatus.DEFERRED
        row.blocked_reason = reason[:2000]
        row.lease_owner = None
        row.leased_until = None
        row.next_attempt_at = retry_at or quotas.next_window_start(
            self._now(), row.dedupe_key
        )

    def _routable_addresses(self) -> set[str] | None:
        """The addresses this worker holds SMTP credentials for.

        None when the provider does not route by address at all (a single-account
        provider, or the mock), in which case there is nothing to check against.
        """
        addresses = getattr(self._provider, "routable_addresses", None)
        return {a.lower() for a in addresses} if addresses else None

    async def _repin(self, session: AsyncSession, row: OutboxMessage) -> str | None:
        """Move this message to a mailbox that can still send it today.

        The pool chooses a mailbox when a message is queued, and until now that
        choice was final. A mailbox that went blocked a week later therefore
        took its whole backlog down with it: production had 47 messages pinned
        to a mailbox on a bounce block and 30 more to one capped at five a day,
        while three healthy mailboxes sat on fifteen unused slots between them.
        No amount of waiting would have cleared that -- those messages were not
        queued behind a clock, they were queued behind a mailbox that was never
        going to open.

        Re-consulted here, at the moment the pinned mailbox actually refuses,
        rather than on a timer: the refusal *is* the evidence that a routing
        decision made days ago has expired.

        Only for refusals that are about the mailbox. A recipient inside quiet
        hours is inside them from every mailbox in the pool, and moving the
        message would change nothing but the row's updated_at.

        Rewrites the sending identity in the payload as well as the foreign key.
        The SMTP pool routes on ``from_email``, so a row whose
        ``sender_identity_id`` moved and whose payload did not would go out over
        one mailbox's connection bearing another's address -- an SPF failure by
        construction, and precisely the kind of mismatch the rest of this system
        exists to prevent. Never moves to a mailbox this worker cannot
        authenticate as, for the same reason.
        """
        from titan.outreach import unsubscribe

        slots = await sender_pool.load_slots(
            session, row.workspace_id, row.campaign_id, now=self._now()
        )
        routable = self._routable_addresses()
        candidates = [
            slot
            for slot in slots
            if slot.sender_identity_id != row.sender_identity_id
            and (routable is None or slot.from_email.lower() in routable)
        ]
        chosen_id = sender_pool.choose(candidates).chosen_id
        if chosen_id is None:
            return None
        chosen = await session.get(SenderIdentity, chosen_id)
        if chosen is None:
            return None

        payload = dict(row.payload or {})
        previous = payload.get("from_email")
        recipient = payload.get("to_email") or row.to_email_normalized
        payload.update(
            {
                "from_email": chosen.from_email,
                "from_name": chosen.from_name,
                "reply_to": chosen.reply_to_email,
                **unsubscribe.headers_for(chosen, recipient, self._settings),
            }
        )
        row.payload = payload
        row.sender_identity_id = chosen.id
        # The message row carries the same two facts for the CRM to read. Left
        # behind, it would report the mailbox that refused as the one that sent.
        await session.execute(
            update(Message)
            .where(Message.id == row.message_id)
            .values(sender_identity_id=chosen.id, from_email=chosen.from_email)
        )
        logger.info(
            "outbox row moved to another mailbox",
            extra={
                "outbox_id": str(row.id),
                "from": previous,
                "to": chosen.from_email,
            },
        )
        return chosen.from_email

    async def _defer_or_repin(
        self, session: AsyncSession, row: OutboxMessage, reason: str
    ) -> str:
        """Defer a mailbox-specific refusal, trying another mailbox first.

        Returns the reason actually recorded, which says where the message went
        when it moved. A deferral that silently changed the sending address
        would make the outbox harder to read than leaving it stuck did.
        """
        moved_to = await self._repin(session, row)
        if moved_to is None:
            await self._defer(session, row, reason)
            return reason
        recorded = f"{reason}; moved to {moved_to}"
        # Deliberately not `now`: a message that keeps finding mailboxes which
        # then refuse it should walk the pool once a minute, not spin through it.
        await self._defer(
            session,
            row,
            recorded,
            retry_at=self._now() + dt.timedelta(seconds=60),
        )
        return recorded

    async def _block(
        self, session: AsyncSession, row: OutboxMessage, reason: str
    ) -> None:
        """Policy refused. Recorded, not retried -- the system stopping itself."""
        row.status = OutboxStatus.CANCELLED
        row.blocked_reason = reason[:2000]
        row.lease_owner = None
        row.leased_until = None
        await session.execute(
            update(Message)
            .where(Message.id == row.message_id, Message.state_rank < 60)
            .values(state=MessageState.FAILED, state_rank=60, state_event_at=self._now())
        )
        logger.info(
            "outbox row blocked by policy",
            extra={"outbox_id": str(row.id), "reason": reason[:500]},
        )

    # ---------------------------------------------------------------- loop
    async def run_once(self) -> list[ProcessResult]:
        """One poll cycle. Each row gets its own transaction."""
        maker = get_sessionmaker()
        results: list[ProcessResult] = []

        async with maker() as session, session.begin():
            claimed = await self.claim_batch(session, self._settings.outbox_batch_size)
            claimed_ids = [row.id for row in claimed]

        for outbox_id in claimed_ids:
            async with maker() as session, session.begin():
                row = await session.get(OutboxMessage, outbox_id, with_for_update=True)
                if row is None or row.status is not OutboxStatus.LEASED:
                    continue
                try:
                    results.append(await self.process_one(session, row))
                except Exception:
                    logger.exception(
                        "unhandled error processing outbox row",
                        extra={"outbox_id": str(outbox_id)},
                    )
                    raise
        return results

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """Poll until stopped. Graceful: finishes the current row first."""
        stop = stop or asyncio.Event()
        logger.info("outbox worker started", extra={"owner": self._owner})
        while not stop.is_set():
            try:
                processed = await self.run_once()
            except Exception:
                logger.exception("outbox poll cycle failed")
                processed = []
            # Poll faster while there is work, slower when idle.
            delay = 0.1 if processed else self._settings.outbox_poll_interval_seconds
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
        logger.info("outbox worker stopped", extra={"owner": self._owner})


def _evidence_ids(draft: MessageDraft) -> set[str]:
    return {
        eid
        for entry in (draft.claim_map or [])
        for eid in (entry.get("evidence_ids") or [])
    }


def _evidence_count(draft: MessageDraft) -> int:
    return len(_evidence_ids(draft))


async def _newest_evidence_captured_at(
    session: AsyncSession, draft: MessageDraft
) -> dt.datetime | None:
    """When the freshest thing this message cites was actually observed.

    The newest rather than the oldest, deliberately. A message quotes several
    findings and is only as current as its most recent look at the site; taking
    the oldest would refuse a freshly re-crawled lead because one supporting
    page happened to be read a month before.

    Returns None when nothing can be read -- an unparseable id, no rows, a
    query that fails. The policy engine denies nothing on None: this check
    exists to stop a stale claim, and a bug in it must not stop every send.
    Drafts citing no evidence at all are refused by `NO_EVIDENCE` instead.
    """
    ids: set[uuid.UUID] = set()
    for raw in _evidence_ids(draft):
        try:
            ids.add(uuid.UUID(str(raw)))
        except (ValueError, AttributeError, TypeError):
            continue
    if not ids:
        return None
    try:
        return await session.scalar(
            select(func.max(FindingEvidence.captured_at)).where(
                FindingEvidence.id.in_(ids)
            )
        )
    except Exception:
        logger.warning(
            "could not read evidence capture time; not applying the staleness check",
            extra={"draft_id": str(draft.id)},
            exc_info=True,
        )
        return None


def _is_member(value: str) -> bool:
    try:
        ContactSource(value)
    except ValueError:
        return False
    return True


__all__ = ["BACKOFF_SCHEDULE", "OutboxWorker", "ProcessResult", "worker_identity"]
