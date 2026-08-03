"""Webhook ingestion proofs: invariants 12, 13, 16.

The failure modes these defend against are all real and all silent:
a duplicate delivery marks a message opened twice and skews every metric;
a delayed `sent` overwrites `bounced` and Titan keeps mailing a dead address;
a forged `delivered` hides a bounce until the domain is blocklisted.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import time

import pytest
from sqlalchemy import func, select
from titan.db.enums import DELIVERY_RANK, LeadStatus, MessageState, SuppressionReason
from titan.db.models import Lead, Message, ProviderEvent
from titan.db.session import get_sessionmaker
from titan.delivery.providers.base import WebhookVerificationError
from titan.delivery.providers.mock import MockEmailProvider
from titan.delivery.providers.resend import ResendProvider
from titan.delivery.suppression import is_suppressed
from titan.delivery.webhooks import ingest_event, record_reply

from .conftest import NOW

pytestmark = pytest.mark.integration


async def mark_sent(message_id, provider_message_id: str = "prov-1") -> None:
    """Put a message into the state the provider would report against."""
    from sqlalchemy import update

    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(
                provider_message_id=provider_message_id,
                state=MessageState.SENT,
                state_rank=DELIVERY_RANK[MessageState.SENT],
                state_event_at=NOW,
            )
        )


def event(state: str, *, event_id: str, at: dt.datetime, **extra) -> dict:
    return {
        "event_id": event_id,
        "state": state,
        "provider_message_id": "prov-1",
        "occurred_at": at.isoformat(),
        **extra,
    }


async def ingest(payload: dict, provider=None) -> object:
    async with get_sessionmaker()() as s, s.begin():
        return await ingest_event(
            s,
            provider or MockEmailProvider(),
            payload,
            signature_verified=True,
            now=NOW,
        )


async def current_state(message_id) -> MessageState:
    async with get_sessionmaker()() as s:
        return (await s.get(Message, message_id)).state


# ==========================================================================
# Invariant 12: duplicates
# ==========================================================================
@pytest.mark.asyncio
async def test_duplicate_event_is_recorded_once(db_session, sendable) -> None:
    await mark_sent(sendable.message_id)
    payload = event("delivered", event_id="evt-dup-1", at=NOW)

    first = await ingest(payload)
    second = await ingest(payload)
    third = await ingest(payload)

    assert first.state_changed is True
    assert second.duplicate is True and second.state_changed is False
    assert third.duplicate is True

    async with get_sessionmaker()() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(ProviderEvent)
            .where(ProviderEvent.provider_event_id == "evt-dup-1")
        )
    assert count == 1, "duplicate webhook created a second provider_events row"


@pytest.mark.asyncio
async def test_duplicate_complaint_suppresses_only_once(db_session, sendable) -> None:
    await mark_sent(sendable.message_id)
    payload = event("complained", event_id="evt-complain-1", at=NOW)
    await ingest(payload)
    await ingest(payload)

    async with get_sessionmaker()() as s:
        entries = (
            await s.execute(
                select(func.count()).select_from(
                    select(1)
                    .select_from(
                        __import__(
                            "titan.db.models", fromlist=["SuppressionEntry"]
                        ).SuppressionEntry
                    )
                    .where(
                        __import__(
                            "titan.db.models", fromlist=["SuppressionEntry"]
                        ).SuppressionEntry.normalized_value
                        == sendable.to_email
                    )
                    .subquery()
                )
            )
        ).scalar()
    assert entries == 1


# ==========================================================================
# Invariant 13: no state regression
# ==========================================================================
@pytest.mark.asyncio
async def test_delayed_sent_cannot_overwrite_delivered(db_session, sendable) -> None:
    await mark_sent(sendable.message_id)
    await ingest(event("delivered", event_id="e1", at=NOW))
    assert await current_state(sendable.message_id) is MessageState.DELIVERED

    # A 'sent' event arriving late, with an EARLIER provider timestamp.
    await ingest(event("sent", event_id="e2", at=NOW - dt.timedelta(minutes=5)))
    assert await current_state(sendable.message_id) is MessageState.DELIVERED


@pytest.mark.asyncio
async def test_delayed_open_cannot_overwrite_bounced(db_session, sendable) -> None:
    """The consequential case: a late 'opened' must not mask a bounce."""
    await mark_sent(sendable.message_id)
    await ingest(event("bounced", event_id="b1", at=NOW, hard_bounce=True))
    assert await current_state(sendable.message_id) is MessageState.BOUNCED

    await ingest(event("opened", event_id="o1", at=NOW + dt.timedelta(minutes=10)))
    assert await current_state(sendable.message_id) is MessageState.BOUNCED


@pytest.mark.asyncio
async def test_complaint_outranks_every_engagement_signal(db_session, sendable) -> None:
    await mark_sent(sendable.message_id)
    await ingest(event("delivered", event_id="d1", at=NOW))
    await ingest(event("opened", event_id="o1", at=NOW))
    await ingest(event("clicked", event_id="c1", at=NOW))
    assert await current_state(sendable.message_id) is MessageState.CLICKED

    await ingest(event("complained", event_id="cp1", at=NOW))
    assert await current_state(sendable.message_id) is MessageState.COMPLAINED

    # And nothing afterwards can undo it.
    await ingest(event("opened", event_id="o2", at=NOW + dt.timedelta(hours=1)))
    assert await current_state(sendable.message_id) is MessageState.COMPLAINED


@pytest.mark.asyncio
async def test_out_of_order_arrival_reaches_the_same_final_state(
    db_session, sendable
) -> None:
    """Processing order must not change the outcome."""
    await mark_sent(sendable.message_id)
    # Deliberately reversed arrival order.
    await ingest(event("opened", event_id="x3", at=NOW + dt.timedelta(minutes=3)))
    await ingest(event("delivered", event_id="x2", at=NOW + dt.timedelta(minutes=1)))
    await ingest(event("sent", event_id="x1", at=NOW))
    assert await current_state(sendable.message_id) is MessageState.OPENED


# ==========================================================================
# Invariant 16: bounce/complaint -> suppression
# ==========================================================================
@pytest.mark.asyncio
async def test_hard_bounce_suppresses_the_address(db_session, sendable) -> None:
    await mark_sent(sendable.message_id)
    await ingest(event("bounced", event_id="hb1", at=NOW, hard_bounce=True))

    async with get_sessionmaker()() as s:
        hit = await is_suppressed(
            s, workspace_id=sendable.workspace_id, email=sendable.to_email, now=NOW
        )
        lead = await s.get(Lead, sendable.lead_id)
    assert hit is not None
    assert hit.reason is SuppressionReason.HARD_BOUNCE
    assert lead.status is LeadStatus.SUPPRESSED


@pytest.mark.asyncio
async def test_soft_bounce_does_not_suppress(db_session, sendable) -> None:
    """A full mailbox is temporary; suppressing would lose a real lead."""
    await mark_sent(sendable.message_id)
    await ingest(event("bounced", event_id="sb1", at=NOW, hard_bounce=False))

    async with get_sessionmaker()() as s:
        hit = await is_suppressed(
            s, workspace_id=sendable.workspace_id, email=sendable.to_email, now=NOW
        )
    assert hit is None


@pytest.mark.asyncio
async def test_complaint_suppresses_and_marks_the_lead(db_session, sendable) -> None:
    await mark_sent(sendable.message_id)
    await ingest(event("complained", event_id="cp2", at=NOW))

    async with get_sessionmaker()() as s:
        hit = await is_suppressed(
            s, workspace_id=sendable.workspace_id, email=sendable.to_email, now=NOW
        )
        lead = await s.get(Lead, sendable.lead_id)
    assert hit is not None and hit.reason is SuppressionReason.COMPLAINT
    assert lead.status is LeadStatus.SUPPRESSED


# ==========================================================================
# Invariant 15: reply stops the sequence
# ==========================================================================
@pytest.mark.asyncio
async def test_record_reply_stops_further_outreach(db_session, sendable) -> None:
    async with get_sessionmaker()() as s, s.begin():
        await record_reply(
            s,
            workspace_id=sendable.workspace_id,
            lead_id=sendable.lead_id,
            replied_at=NOW,
        )
    async with get_sessionmaker()() as s:
        lead = await s.get(Lead, sendable.lead_id)
    assert lead.replied_at is not None
    assert lead.status is LeadStatus.REPLIED
    assert lead.outreach_blocking_errors()


@pytest.mark.asyncio
async def test_reply_does_not_resurrect_a_suppressed_lead(db_session, sendable) -> None:
    await mark_sent(sendable.message_id)
    await ingest(event("complained", event_id="cp3", at=NOW))

    async with get_sessionmaker()() as s, s.begin():
        await record_reply(
            s,
            workspace_id=sendable.workspace_id,
            lead_id=sendable.lead_id,
            replied_at=NOW + dt.timedelta(hours=1),
        )
    async with get_sessionmaker()() as s:
        lead = await s.get(Lead, sendable.lead_id)
    assert lead.status is LeadStatus.SUPPRESSED, "a reply un-suppressed the lead"


# ==========================================================================
# Unrecognised / unmatched events
# ==========================================================================
@pytest.mark.asyncio
async def test_unknown_event_type_is_accepted_and_ignored(db_session, sendable) -> None:
    outcome = await ingest({"state": "not_a_real_state", "event_id": "u1"})
    assert outcome.accepted and outcome.ignored_reason == "unrecognised_event_type"


@pytest.mark.asyncio
async def test_event_for_unknown_message_is_not_an_error(db_session) -> None:
    outcome = await ingest(
        {
            "state": "delivered",
            "event_id": "orphan-1",
            "provider_message_id": "does-not-exist",
            "occurred_at": NOW.isoformat(),
        }
    )
    assert outcome.accepted
    assert outcome.ignored_reason == "no_matching_message"


# ==========================================================================
# Signature verification (H-17)
# ==========================================================================
class TestResendSignature:
    SECRET = "whsec_" + base64.b64encode(b"titan-test-secret-key-32-bytes!!").decode()

    def provider(self) -> ResendProvider:
        return ResendProvider(api_key="re_test", webhook_secret=self.SECRET)

    def sign(self, body: bytes, msg_id: str = "msg_1", ts: int | None = None) -> dict:
        timestamp = str(ts if ts is not None else int(time.time()))
        raw = self.SECRET[len("whsec_") :]
        key = base64.b64decode(raw)
        signed = b".".join([msg_id.encode(), timestamp.encode(), body])
        digest = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
        return {
            "svix-id": msg_id,
            "svix-timestamp": timestamp,
            "svix-signature": f"v1,{digest}",
        }

    def test_valid_signature_verifies(self) -> None:
        body = json.dumps({"type": "email.delivered"}).encode()
        self.provider().verify_webhook(payload=body, headers=self.sign(body))

    def test_forged_signature_is_rejected(self) -> None:
        body = json.dumps({"type": "email.delivered"}).encode()
        headers = self.sign(body)
        headers["svix-signature"] = "v1,AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        with pytest.raises(WebhookVerificationError):
            self.provider().verify_webhook(payload=body, headers=headers)

    def test_tampered_body_is_rejected(self) -> None:
        body = json.dumps({"type": "email.delivered"}).encode()
        headers = self.sign(body)
        tampered = json.dumps({"type": "email.bounced"}).encode()
        with pytest.raises(WebhookVerificationError):
            self.provider().verify_webhook(payload=tampered, headers=headers)

    def test_replayed_old_timestamp_is_rejected(self) -> None:
        body = b"{}"
        old = int(time.time()) - 3600
        with pytest.raises(WebhookVerificationError, match="replay window"):
            self.provider().verify_webhook(payload=body, headers=self.sign(body, ts=old))

    def test_missing_headers_are_rejected(self) -> None:
        with pytest.raises(WebhookVerificationError, match="missing"):
            self.provider().verify_webhook(payload=b"{}", headers={})

    def test_absent_secret_fails_closed(self) -> None:
        """An unverifiable webhook must never be treated as authentic."""
        provider = ResendProvider(api_key="re_test", webhook_secret=None)
        body = b"{}"
        with pytest.raises(WebhookVerificationError, match="no webhook secret"):
            provider.verify_webhook(payload=body, headers=self.sign(body))

    def test_signature_rotation_accepts_any_matching_version(self) -> None:
        body = b'{"type":"email.sent"}'
        headers = self.sign(body)
        good = headers["svix-signature"]
        headers["svix-signature"] = f"v1,AAAA{' '}{good}"
        self.provider().verify_webhook(payload=body, headers=headers)


class TestResendNormalization:
    def provider(self) -> ResendProvider:
        return ResendProvider(api_key="re_test")

    def test_bounce_type_determines_suppression(self) -> None:
        hard = self.provider().normalize_webhook(
            {
                "type": "email.bounced",
                "created_at": NOW.isoformat(),
                "data": {
                    "email_id": "m1",
                    "to": ["a@b.test"],
                    "bounce": {"type": "Permanent"},
                },
            }
        )
        assert hard is not None and hard.is_hard_bounce is True

        soft = self.provider().normalize_webhook(
            {
                "type": "email.bounced",
                "created_at": NOW.isoformat(),
                "data": {
                    "email_id": "m1",
                    "to": ["a@b.test"],
                    "bounce": {"type": "Transient"},
                },
            }
        )
        assert soft is not None and soft.is_hard_bounce is False

    def test_unknown_type_returns_none(self) -> None:
        assert self.provider().normalize_webhook({"type": "email.unheard_of"}) is None

    def test_event_id_is_always_present(self) -> None:
        """Deduplication depends on it, so it is derived when the provider omits it."""
        normalized = self.provider().normalize_webhook(
            {"type": "email.delivered", "data": {"email_id": "m9", "to": ["x@y.test"]}}
        )
        assert normalized is not None
        assert normalized.provider_event_id
