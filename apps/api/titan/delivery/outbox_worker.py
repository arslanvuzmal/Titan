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
import datetime as dt
import logging
import os
import random
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text, update
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
from titan.delivery import adaptive_limits, deliverability, quotas, sender_health
from titan.delivery.providers.base import (
    EmailProvider,
    OutboundEmail,
    SendResult,
)
from titan.delivery.suppression import is_suppressed, suppress
from titan.intelligence import domain_health
from titan.intelligence.domain_health import DomainHealth, DomainWindow
from titan.intelligence.sender_auth import is_stale
from titan.notify.operator import NotificationKind, record_notification
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


def worker_identity() -> str:
    """Stable-per-process lease owner, so a crashed worker is identifiable."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True, slots=True)
class ProcessResult:
    outbox_id: uuid.UUID
    outcome: str  # sent | deferred | blocked | retried | failed_permanent
    detail: str | None = None


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
        """Atomically claim up to ``limit`` due rows.

        SKIP LOCKED is what makes this safe under concurrency: a row already
        locked by another worker is passed over rather than blocking, so
        throughput scales with worker count instead of serialising.
        """
        now = self._now()
        lease_until = now + dt.timedelta(seconds=self._settings.outbox_lease_seconds)

        claim = text(
            """
            WITH claimable AS (
                SELECT id
                  FROM outbox_messages
                 WHERE (
                        status IN ('pending', 'deferred')
                        OR (status = 'leased' AND leased_until < :now)
                       )
                   AND next_attempt_at <= :now
                 ORDER BY next_attempt_at
                 FOR UPDATE SKIP LOCKED
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

        ctx = SendContext(
            settings=self._settings,
            now=self._now(),
            workspace_mode=workspace.operating_mode,
            workspace_sending_authorized=workspace.sending_authorized,
            campaign_mode=policy.operating_mode,
            campaign_status=campaign.status,
            campaign_sending_authorized=policy.sending_authorized,
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
            contact_is_active=channel.is_active,
            recipient_timezone=location.timezone if location else None,
            recipient_domain_health=domain_health,
            recipient_subregion=subregion_for_location(
                location.country_code if location else None,
                location.region if location else None,
                location.longitude if location else None,
            ),
            campaign_subregion=campaign.sub_region,
            send_window=SendWindow(
                start_hour=policy.send_window_start_hour,
                end_hour=policy.send_window_end_hour,
                days=tuple(int(d) for d in (policy.send_days or ())),
            ),
            campaign_region=campaign.region,
            evidence_count=_evidence_count(draft),
            validation_passed=draft.validation_passed,
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
                    retry_at=self._next_window_open(ctx),
                )
                return ProcessResult(row.id, "deferred", decision.reason_text())
            await self._block(session, row, decision.reason_text())
            return ProcessResult(row.id, "blocked", decision.reason_text())

        email = self._render(row, decision)

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
                # Temporary: volume or reputation. Defer rather than discard.
                await self._defer(session, row, f"deliverability: {reasons}")
                return ProcessResult(row.id, "deferred", reasons)
            await self._block(session, row, f"deliverability: {reasons}")
            return ProcessResult(row.id, "blocked", reasons)

        # Last thing before the provider call, so every refusal above this line
        # costs nothing from the day's allowance.
        outcome = await self._reserve_quota(session, row, limit)
        if not outcome.granted:
            await self._defer(session, row, outcome.reason or "quota exhausted")
            return ProcessResult(row.id, "deferred", outcome.reason)

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

        return await self._record(session, row, result, ctx)

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
                      count(*) FILTER (WHERE bounced_at IS NOT NULL)    AS bounced,
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

        first_send_at = stats.first_send_at
        attempted = int(throughput.attempted or 0)
        retries = int(throughput.retries or 0)
        warmup_limit = deliverability.warmup_limit(first_send_at=first_send_at, now=now)
        warmup_day = (
            None
            if warmup_limit is None
            else (
                0 if first_send_at is None else (now.date() - first_send_at.date()).days
            )
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

        decision = adaptive_limits.daily_limit(
            sender.daily_send_limit,
            recent=(status, *history),
            warmup_limit=warmup_limit,
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

        # Reputation is measured per sending domain over the trailing 30 days:
        # a fresh window would let a bad week be forgotten too quickly, and a
        # lifetime window would never recover.
        since = now - dt.timedelta(days=30)
        stats = (
            await session.execute(
                text(
                    """
                    SELECT
                      count(*) FILTER (WHERE sent_at IS NOT NULL)      AS sent,
                      count(*) FILTER (WHERE delivered_at IS NOT NULL) AS delivered,
                      count(*) FILTER (WHERE bounced_at IS NOT NULL)   AS bounced,
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

        first_send_at = (
            await session.execute(
                text(
                    "SELECT min(sent_at) FROM messages "
                    "WHERE workspace_id = :workspace AND sender_identity_id = :sender "
                    "AND sent_at IS NOT NULL"
                ),
                {"workspace": row.workspace_id, "sender": row.sender_identity_id},
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
                ),
                first_send_at=first_send_at,
                sent_today=sent_today,
                now=now,
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

    def _render(self, row: OutboxMessage, decision: Decision) -> OutboundEmail:
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
        )

    async def _record(
        self,
        session: AsyncSession,
        row: OutboxMessage,
        result: SendResult,
        ctx: SendContext | None = None,
    ) -> ProcessResult:
        now = self._now()
        if result.accepted:
            row.status = OutboxStatus.SENT
            row.sent_at = now
            row.lease_owner = None
            row.leased_until = None
            await session.execute(
                update(Message)
                .where(Message.id == row.message_id, Message.state_rank < 20)
                .values(
                    state=MessageState.SENT,
                    state_rank=20,
                    state_event_at=now,
                    provider_message_id=result.provider_message_id,
                    sent_at=now,
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
        delay = random.uniform(base * 0.5, base * 1.5)  # noqa: S311 - not cryptographic
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
        timezone = resolve_timezone(
            ctx.recipient_timezone,
            ctx.campaign_region,
            recipient_subregion=ctx.recipient_subregion,
            campaign_subregion=ctx.campaign_subregion,
        )
        local = local_time(ctx.now, timezone)
        if local is None:
            return None
        opens = ctx.send_window.next_open_from(local)
        if opens is None or opens <= local:
            return None
        return opens.astimezone(dt.UTC)

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


def _evidence_count(draft: MessageDraft) -> int:
    return len(
        {
            eid
            for entry in (draft.claim_map or [])
            for eid in (entry.get("evidence_ids") or [])
        }
    )


def _is_member(value: str) -> bool:
    try:
        ContactSource(value)
    except ValueError:
        return False
    return True


__all__ = ["BACKOFF_SCHEDULE", "OutboxWorker", "ProcessResult", "worker_identity"]
