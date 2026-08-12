"""Renewing sender verification, against a real database.

DNS is stubbed. What is under test is the half Phase 6 was missing: that flags
are written from what a resolver said, that they come *down* when a domain
stops publishing, and that an identity which was sending and now cannot gets an
operator alert rather than silence.
"""

from __future__ import annotations

import datetime as dt
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select
from titan.activities.verification import verify_sender_identities
from titan.db.models import SenderIdentity
from titan.db.models.ops import Task
from titan.db.session import get_sessionmaker
from titan.intelligence.sender_auth import DomainAuth
from titan.workflows.types import VerifySendersInput

pytestmark = pytest.mark.asyncio


def auth(
    domain: str = "sending-fixture.test",
    *,
    resolved: bool = True,
    spf: bool = True,
    dmarc: bool = True,
    dkim: bool = True,
) -> DomainAuth:
    return DomainAuth(
        domain=domain,
        resolved=resolved,
        spf_ok=spf,
        dmarc_ok=dmarc,
        dkim_ok=dkim,
        dkim_conclusive=dkim,
        spf_record="v=spf1 -all" if spf else None,
        dmarc_record="v=DMARC1; p=reject" if dmarc else None,
        dkim_selector="default" if dkim else None,
        notes=() if resolved else ("domain does not exist in DNS",),
    )


async def seed_sender(
    workspace_id: uuid.UUID,
    *,
    domain: str,
    tag: str,
    verified: bool = True,
    last_verified_at: dt.datetime | None = None,
    active: bool = True,
) -> uuid.UUID:
    async with get_sessionmaker()() as session, session.begin():
        sender = SenderIdentity(
            workspace_id=workspace_id,
            label=f"sender-{tag}",
            from_email=f"{tag}@{domain}",
            from_name="Titan",
            reply_to_email=f"{tag}@{domain}",
            sending_domain=domain,
            domain_verified=verified,
            spf_ok=verified,
            dkim_ok=verified,
            dmarc_ok=verified,
            last_verified_at=last_verified_at,
            is_active=active,
            mailing_address="12 Fictional Row",
            unsubscribe_mailto=f"mailto:unsub@{domain}",
        )
        session.add(sender)
        await session.flush()
        return sender.id


async def run(workspace_id: uuid.UUID, results: dict[str, DomainAuth]):
    def fake(domain: str, **_):
        return results[domain]

    with patch("titan.activities.verification.check_domain_auth", side_effect=fake):
        with patch("titan.activities.verification.activity") as fake_activity:
            fake_activity.heartbeat = lambda *a, **k: None
            return await verify_sender_identities(
                VerifySendersInput(workspace_id=str(workspace_id))
            )


# ==========================================================================
# The gate reopening
# ==========================================================================
async def test_a_good_domain_renews_the_verification(db_session, workspace):
    """Without this, every campaign stops 14 days after setup."""
    stale = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
    sender_id = await seed_sender(
        workspace, domain="good.test", tag="a", last_verified_at=stale
    )

    result = await run(workspace, {"good.test": auth("good.test")})

    assert result.passing == 1
    assert result.failing == 0

    async with get_sessionmaker()() as s:
        sender = await s.get(SenderIdentity, sender_id)
        assert sender.last_verified_at > stale
        assert sender.domain_verified is True
        # And the gate it feeds now passes.
        assert sender.authorization_errors() == []


# ==========================================================================
# Flags come down on evidence
# ==========================================================================
async def test_a_domain_that_stopped_publishing_spf_loses_the_flag(db_session, workspace):
    sender_id = await seed_sender(
        workspace,
        domain="lapsed.test",
        tag="b",
        last_verified_at=dt.datetime.now(dt.UTC),
    )

    result = await run(workspace, {"lapsed.test": auth("lapsed.test", spf=False)})

    assert result.failing == 1

    async with get_sessionmaker()() as s:
        sender = await s.get(SenderIdentity, sender_id)
        assert sender.spf_ok is False
        assert sender.domain_verified is False
        assert sender.authorization_errors(), "a broken domain must stop sending"


async def test_a_domain_that_no_longer_exists_stops_sending(db_session, workspace):
    """The production case: twenty identities on a domain with no DNS."""
    sender_id = await seed_sender(
        workspace,
        domain="gone.test",
        tag="c",
        last_verified_at=dt.datetime.now(dt.UTC),
    )

    await run(
        workspace,
        {
            "gone.test": auth(
                "gone.test", resolved=False, spf=False, dmarc=False, dkim=False
            )
        },
    )

    async with get_sessionmaker()() as s:
        sender = await s.get(SenderIdentity, sender_id)
        assert sender.domain_verified is False
        assert sender.spf_ok is False
        assert sender.dmarc_ok is False
        assert sender.dkim_ok is False


async def test_an_unknown_dkim_selector_does_not_stop_a_good_domain(
    db_session, workspace
):
    """DKIM cannot be established from DNS, so it must not be a blocker."""
    sender_id = await seed_sender(
        workspace, domain="nodkim.test", tag="d", last_verified_at=None
    )

    result = await run(workspace, {"nodkim.test": auth("nodkim.test", dkim=False)})

    assert result.passing == 1

    async with get_sessionmaker()() as s:
        sender = await s.get(SenderIdentity, sender_id)
        assert sender.dkim_ok is False
        assert sender.domain_verified is True
        assert sender.authorization_errors() == []


# ==========================================================================
# Alerting
# ==========================================================================
async def test_an_identity_that_could_send_and_now_cannot_alerts(db_session, workspace):
    await seed_sender(
        workspace,
        domain="breaking.test",
        tag="e",
        last_verified_at=dt.datetime.now(dt.UTC),
    )

    result = await run(workspace, {"breaking.test": auth("breaking.test", dmarc=False)})

    assert result.newly_broken == ("e@breaking.test",)

    async with get_sessionmaker()() as s:
        task = (
            (await s.execute(select(Task).where(Task.kind == "deliverability_alert")))
            .scalars()
            .one()
        )
        assert "can no longer send" in task.title


async def test_a_domain_that_was_never_configured_does_not_alert(db_session, workspace):
    """Otherwise every unfinished setup pages the operator every morning."""
    await seed_sender(
        workspace,
        domain="never.test",
        tag="f",
        verified=False,
        last_verified_at=None,
    )

    result = await run(
        workspace,
        {
            "never.test": auth(
                "never.test", resolved=False, spf=False, dmarc=False, dkim=False
            )
        },
    )

    assert result.newly_broken == ()

    async with get_sessionmaker()() as s:
        tasks = (await s.execute(select(Task))).scalars().all()
    assert tasks == []


# ==========================================================================
# Cost
# ==========================================================================
async def test_identities_sharing_a_domain_cost_one_lookup(db_session, workspace):
    """Twenty identities on one domain is the shape that started all of this."""
    for n in range(5):
        await seed_sender(workspace, domain="shared.test", tag=f"s{n}")

    calls: list[str] = []

    def counting(domain: str, **_):
        calls.append(domain)
        return auth("shared.test")

    with (
        patch("titan.activities.verification.check_domain_auth", side_effect=counting),
        patch("titan.activities.verification.activity") as fake_activity,
    ):
        fake_activity.heartbeat = lambda *a, **k: None
        result = await verify_sender_identities(
            VerifySendersInput(workspace_id=str(workspace))
        )

    assert result.checked == 5
    assert result.domains_resolved == 1
    assert calls == ["shared.test"]


async def test_a_workspace_with_no_senders_is_not_an_error(db_session, workspace):
    result = await run(workspace, {})

    assert result.checked == 0
