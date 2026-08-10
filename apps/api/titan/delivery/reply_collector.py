"""Collecting replies from a mailbox and feeding them to the ingest rules.

The missing half of the reply loop. :func:`titan.delivery.inbound.ingest_inbound`
knew what to do with an arriving message and had no way to be given one; nothing
in the system read mail. Until this ran, a person could answer, ask to be
removed, or hard-bounce, and Titan would keep writing -- the SMTP provider's own
docstring says as much: *"a bounce arrives as a separate email to the envelope
sender, which nothing here reads yet."*

Three jobs, in order:

1. **Attribute the message to a lead.** Threading headers first, sender address
   second. An unattributed reply still gets recorded and can still suppress an
   address; only the sequence stop needs to know which lead.
2. **Hand it to the rules**, which decide and record.
3. **Mark it read** -- and only then.

That last ordering is the one that matters. The database commit happens before
the IMAP flag is set, never the other way round. If the flag were set first and
the write then failed, the reply would be marked handled and never seen again;
somebody's unsubscribe would vanish. In the order used here a failure between
the two costs one re-read, which the unique constraint on
``provider_inbound_id`` collapses to nothing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.models.messaging import Message
from titan.db.session import system_unit_of_work, workspace_unit_of_work
from titan.delivery.inbound import IngestResult, ingest_inbound
from titan.delivery.mailbox import Mailbox, ParsedEmail, RawMessage, parse_email
from titan.intelligence.contacts import normalize_email
from titan.intelligence.replies import InboundMessage
from titan.notify.operator import push_notification

logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 60
DEFAULT_BATCH_SIZE = 50


@dataclass(frozen=True, slots=True)
class ThreadMatch:
    """Which outbound message, lead and workspace an inbound reply belongs to."""

    workspace_id: uuid.UUID
    lead_id: uuid.UUID | None
    message_id: uuid.UUID | None
    #: The address Titan originally wrote to. Not necessarily who replied: an
    #: assistant may answer from their own mailbox, and a bounce comes from the
    #: receiving host's daemon. This is the address outreach must stop going to.
    recipient: str | None
    matched_by: str


@dataclass(frozen=True, slots=True)
class CollectionResult:
    fetched: int = 0
    ingested: int = 0
    duplicates: int = 0
    #: Messages Titan itself sent, sitting in the polled folder.
    skipped_own: int = 0
    #: Recorded, but not attributable to any lead.
    unmatched: int = 0
    #: Left unread deliberately, to be retried.
    failed: int = 0
    stopped_sequences: int = 0
    suppressed: int = 0
    #: Operator notifications recorded this cycle.
    notified: int = 0
    #: Replies that asked for a call, each of which opened a proposed meeting.
    #: The number worth reporting upwards: it is the campaign's actual output.
    meetings_opened: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def processed(self) -> int:
        return self.ingested + self.duplicates + self.skipped_own


class ReplyCollector:
    """Polls one mailbox and ingests what it finds.

    ``mailbox_address`` is the address being polled. Messages from it are our
    own -- a copy in the folder, or a mail loop -- and are skipped rather than
    ingested, because classifying our own outreach as an inbound reply would
    stop the very sequence that sent it.

    ``default_workspace_id`` is used for a message that matches no outbound
    message: somebody writing in cold, or replying from an address Titan never
    wrote to. Without it such a message cannot be recorded at all, since every
    row in this schema is workspace-scoped.
    """

    def __init__(
        self,
        mailbox: Mailbox,
        *,
        mailbox_address: str,
        default_workspace_id: uuid.UUID | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._mailbox = mailbox
        self._own_address = normalize_email(mailbox_address)
        self._default_workspace_id = default_workspace_id
        self._batch_size = batch_size

    async def run_once(self) -> CollectionResult:
        """One poll cycle. Returns what happened to each message."""
        raws = await self._mailbox.fetch_unread(self._batch_size)
        if not raws:
            return CollectionResult()

        handled: list[str] = []
        counts = {
            "ingested": 0,
            "duplicates": 0,
            "skipped_own": 0,
            "unmatched": 0,
            "failed": 0,
            "stopped_sequences": 0,
            "suppressed": 0,
            "notified": 0,
            "meetings_opened": 0,
        }
        errors: list[str] = []

        for raw in raws:
            try:
                outcome = await self._process(raw, counts)
            except Exception as exc:
                # One malformed or unluckily-timed message must not take the
                # batch with it: the messages behind it in the folder include
                # the opt-outs.
                counts["failed"] += 1
                errors.append(f"uid {raw.uid}: {type(exc).__name__}: {exc}")
                logger.exception(
                    "failed to ingest inbound message", extra={"uid": raw.uid}
                )
                continue
            if outcome:
                handled.append(raw.uid)

        # Only now, and only for the ones that committed.
        if handled:
            try:
                await self._mailbox.mark_read(handled)
            except Exception as exc:
                # Harmless: the next cycle re-reads them and the unique
                # constraint makes every one a no-op duplicate.
                logger.warning(
                    "could not mark messages read; they will be re-read and deduped",
                    extra={"count": len(handled), "error": str(exc)[:200]},
                )

        return CollectionResult(fetched=len(raws), errors=tuple(errors), **counts)

    async def run_forever(
        self, stop: asyncio.Event, *, interval_seconds: int = DEFAULT_POLL_SECONDS
    ) -> None:
        """Poll until ``stop`` is set, surviving transient mailbox failures."""
        while not stop.is_set():
            try:
                result = await self.run_once()
                if result.fetched:
                    logger.info(
                        "reply poll complete",
                        extra={
                            "fetched": result.fetched,
                            "ingested": result.ingested,
                            "duplicates": result.duplicates,
                            "unmatched": result.unmatched,
                            "failed": result.failed,
                            "stopped_sequences": result.stopped_sequences,
                            "suppressed": result.suppressed,
                            "notified": result.notified,
                            "meetings_opened": result.meetings_opened,
                        },
                    )
            except Exception:
                # A mailbox that is down comes back; a poller that exited does
                # not. Log and wait out the interval.
                logger.exception("reply poll failed; retrying next interval")

            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                continue

    # ------------------------------------------------------------------ internals

    async def _process(self, raw: RawMessage, counts: dict[str, int]) -> bool:
        """Handle one message. True when it may be marked read."""
        parsed = parse_email(raw.raw)

        if parsed.from_email and parsed.from_email == self._own_address:
            counts["skipped_own"] += 1
            logger.debug("skipping our own message", extra={"uid": raw.uid})
            return True

        async with system_unit_of_work() as session:
            match = await self._match(session, parsed)

        workspace_id = match.workspace_id if match else self._default_workspace_id
        if workspace_id is None:
            # Nothing in this schema exists outside a workspace, so there is
            # nowhere to put it. Left unread on purpose: this is a configuration
            # gap (TITAN_IMAP_WORKSPACE_ID), and the reply should still be here
            # once it is fixed rather than silently consumed.
            counts["failed"] += 1
            logger.warning(
                "inbound message matches no outbound message and no default "
                "workspace is configured; leaving it unread",
                extra={"uid": raw.uid, "from": parsed.from_email},
            )
            return False

        if match is None or match.lead_id is None:
            counts["unmatched"] += 1

        result = await self._ingest(parsed, match, workspace_id)

        if result.duplicate:
            counts["duplicates"] += 1
            return True

        counts["ingested"] += 1
        if result.sequence_stopped:
            counts["stopped_sequences"] += 1
        if result.suppressed:
            counts["suppressed"] += 1
        if result.notification is not None:
            counts["notified"] += 1
        if result.meeting_id is not None:
            counts["meetings_opened"] += 1

        # After the commit, never inside it. The row is the guarantee; this is
        # the convenience on top, and it involves an HTTP call to a host that
        # may be having a bad minute.
        await push_notification(result.notification)
        return True

    async def _ingest(
        self,
        parsed: ParsedEmail,
        match: ThreadMatch | None,
        workspace_id: uuid.UUID,
    ) -> IngestResult:
        message = InboundMessage(
            from_email=parsed.from_email,
            subject=parsed.subject,
            body_text=parsed.body_text,
            headers=parsed.headers,
            content_type=parsed.content_type,
            in_reply_to=parsed.in_reply_to,
        )
        async with workspace_unit_of_work(workspace_id) as session:
            return await ingest_inbound(
                session,
                workspace_id=workspace_id,
                message=message,
                lead_id=match.lead_id if match else None,
                provider="imap",
                # The RFC 5322 Message-ID, which is stable across re-fetches,
                # folder moves and IMAP UID resets in a way a UID is not. Absent
                # only from messages that violate the spec, and ingest_inbound
                # derives a content hash for those.
                provider_inbound_id=parsed.message_id,
                in_reply_to_message_id=match.message_id if match else None,
                suppression_target=(
                    parsed.failed_recipient
                    or (match.recipient if match else None)
                    or parsed.from_email
                ),
                received_at=parsed.date or dt.datetime.now(dt.UTC),
            )

    async def _match(
        self, session: AsyncSession, parsed: ParsedEmail
    ) -> ThreadMatch | None:
        """Find the outbound message this is a reply to.

        Runs unscoped because the workspace is exactly what is being looked up:
        a Message-ID is globally unique and identifies its workspace. The write
        that follows is re-scoped to whatever this returns.
        """
        thread_ids = parsed.thread_ids()
        if thread_ids:
            row = (
                (
                    await session.execute(
                        select(Message).where(Message.provider_message_id.in_(thread_ids))
                    )
                )
                .scalars()
                .first()
            )
            if row is not None:
                return ThreadMatch(
                    workspace_id=row.workspace_id,
                    lead_id=row.lead_id,
                    message_id=row.id,
                    recipient=row.to_email_normalized,
                    matched_by="threading_headers",
                )

        # A bounce is not *from* the recipient, so match on who failed. Doing
        # this before the sender lookup matters: matching a DSN on
        # MAILER-DAEMON@ would attribute it to no lead at all.
        candidates = [
            address for address in (parsed.failed_recipient, parsed.from_email) if address
        ]
        for address in candidates:
            row = (
                (
                    await session.execute(
                        select(Message)
                        .where(Message.to_email_normalized == normalize_email(address))
                        .order_by(
                            Message.sent_at.desc().nullslast(), Message.created_at.desc()
                        )
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if row is not None:
                return ThreadMatch(
                    workspace_id=row.workspace_id,
                    lead_id=row.lead_id,
                    message_id=row.id,
                    recipient=row.to_email_normalized,
                    matched_by=(
                        "failed_recipient"
                        if address == parsed.failed_recipient
                        else "sender_address"
                    ),
                )
        return None


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_POLL_SECONDS",
    "CollectionResult",
    "ReplyCollector",
    "ThreadMatch",
]
