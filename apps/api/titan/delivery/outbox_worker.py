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

from sqlalchemy import select, text, update
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
    SenderIdentity,
    Workspace,
)
from titan.db.session import get_sessionmaker
from titan.delivery import deliverability, quotas
from titan.delivery.providers.base import (
    EmailProvider,
    OutboundEmail,
    SendResult,
)
from titan.delivery.suppression import is_suppressed, suppress
from titan.policy.engine import Decision, SendContext, evaluate_send

logger = logging.getLogger(__name__)

#: Retry backoff in seconds, indexed by attempt. Jittered at use.
BACKOFF_SCHEDULE = (30, 120, 600, 1800, 7200, 21600)


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

        decision = evaluate_send(ctx)
        if not decision.allowed:
            # Quota and quiet hours are temporary; everything else is a block.
            if self._is_temporary(decision):
                await self._defer(session, row, decision.reason_text())
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
        outcome = await self._reserve_quota(session, row)
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

        return await self._record(session, row, result)

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
                     WHERE sender_identity_id = :sender AND created_at >= :since
                    """
                ),
                {"sender": row.sender_identity_id, "since": since},
            )
        ).one()

        first_send_at = (
            await session.execute(
                text(
                    "SELECT min(sent_at) FROM messages "
                    "WHERE sender_identity_id = :sender AND sent_at IS NOT NULL"
                ),
                {"sender": row.sender_identity_id},
            )
        ).scalar_one_or_none()

        sent_today = int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM messages WHERE sender_identity_id = :s "
                        "AND sent_at >= :start"
                    ),
                    {
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

        temporary = {DenyCode.QUOTA_EXHAUSTED, DenyCode.QUIET_HOURS, DenyCode.SPACING}
        codes = set(decision.codes)
        return bool(codes) and codes <= temporary

    async def _quota_requests(
        self, session: AsyncSession, row: OutboxMessage
    ) -> list[quotas.QuotaRequest]:
        """The four scopes one send consumes.

        Built in one place so a release returns units to exactly the scopes the
        reservation took them from -- a release that reconstructed the list
        differently would silently corrupt the counters.
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
                sender.daily_send_limit if sender else settings.quota_sender_daily,
            ),
            quotas.QuotaRequest(
                quotas.QuotaScope.RECIPIENT_DOMAIN,
                row.to_domain,
                policy.recipient_domain_daily_limit,
            ),
        ]

    async def _reserve_quota(
        self, session: AsyncSession, row: OutboxMessage
    ) -> quotas.QuotaOutcome:
        return await quotas.reserve_all(
            session,
            workspace_id=row.workspace_id,
            requests=await self._quota_requests(session, row),
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
        self, session: AsyncSession, row: OutboxMessage, result: SendResult
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

    async def _defer(
        self, session: AsyncSession, row: OutboxMessage, reason: str
    ) -> None:
        """Quota/quiet-hours deferral. Never a permanent failure (mission 15.4)."""
        row.status = OutboxStatus.DEFERRED
        row.blocked_reason = reason[:2000]
        row.lease_owner = None
        row.leased_until = None
        row.next_attempt_at = quotas.next_window_start(self._now(), row.dedupe_key)

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
