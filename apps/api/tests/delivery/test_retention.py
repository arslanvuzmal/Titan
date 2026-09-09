"""Erasing what a business never asked us to keep.

Runs against a real PostgreSQL, because the whole behaviour is a set of
conditional UPDATEs and a mock would only prove the SQL string had not changed.

The theme is the same as the rest of the delivery suite inverted: everywhere
else the expensive failure is sending something wrong. Here it is *erasing* the
wrong thing -- a suppression that stops existing, or a conversation deleted
halfway through -- and every test below guards one of those.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from titan.delivery.retention import RETENTION_DAYS, erase_expired

from tests.delivery.conftest import build_sendable

pytestmark = pytest.mark.asyncio

NOW = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)
LONG_AGO = NOW - dt.timedelta(days=RETENTION_DAYS + 5)
RECENT = NOW - dt.timedelta(days=3)


async def _send(session, sendable, when: dt.datetime) -> None:
    """Mark the built message as sent at a chosen moment."""
    await session.execute(
        text("UPDATE messages SET sent_at = :when WHERE draft_id = :draft"),
        {"when": when, "draft": sendable.draft_id},
    )
    await session.commit()


async def _body_of(session, sendable) -> str | None:
    return await session.scalar(
        text("SELECT body_text FROM message_drafts WHERE id = :id"),
        {"id": sendable.draft_id},
    )


async def test_an_ignored_message_is_erased_after_the_window(db_session, workspace):
    """Planted violation: never stamp or erase, and the estate keeps the words
    it wrote to strangers for ever.

    `body_retained_until` has existed since the first migration carrying the
    comment "Body retained only until the retention window expires", and 0 of
    807 rows had ever been stamped.
    """
    lead = await build_sendable(db_session, workspace, suffix="ignored")
    await _send(db_session, lead, LONG_AGO)

    report = await erase_expired(db_session, workspace_id=workspace, now=NOW)
    await db_session.commit()

    assert report.drafts_erased >= 1
    assert await _body_of(db_session, lead) == ""


async def test_a_recent_message_is_left_alone(db_session, workspace):
    """The window is the whole policy. A message sent last week is still doing
    its job -- the follow-up sequence runs four deep."""
    lead = await build_sendable(db_session, workspace, suffix="recent")
    await _send(db_session, lead, RECENT)

    report = await erase_expired(db_session, workspace_id=workspace, now=NOW)
    await db_session.commit()

    assert report.leads_examined == 0
    assert await _body_of(db_session, lead) != ""


async def test_a_business_that_replied_is_never_erased(db_session, workspace):
    """Planted violation: erase on age alone.

    A reply is a relationship, and deleting half a conversation is worse than
    keeping all of it. Age is not the only condition and this is the other one.
    """
    lead = await build_sendable(db_session, workspace, suffix="replied")
    await _send(db_session, lead, LONG_AGO)
    await db_session.execute(
        text(
            """
            INSERT INTO inbound_messages
              (provider, provider_inbound_id, lead_id, from_email_normalized,
               subject, body_text, received_at, raw_payload, workspace_id)
            VALUES ('imap', :pid, :lead, 'someone@example.test',
                    'Re: your note', 'Yes, interested.', :at, '{}'::jsonb, :ws)
            """
        ),
        {"pid": f"retention-{lead.draft_id}", "lead": lead.lead_id, "at": LONG_AGO, "ws": workspace},
    )
    await db_session.commit()

    report = await erase_expired(db_session, workspace_id=workspace, now=NOW)
    await db_session.commit()

    assert report.kept_for_reply >= 1
    assert await _body_of(db_session, lead) != "", "erased a business that wrote back"


async def test_the_suppression_list_survives(db_session, workspace):
    """Planted violation: erase the address wherever it appears.

    This is the failure that looks like better privacy and is the opposite.
    Suppression matches on the plaintext address, so an erased suppression is a
    business we write to again -- and the person who most wants to be left
    alone is exactly the person that harms.
    """
    lead = await build_sendable(db_session, workspace, suffix="optedout")
    await _send(db_session, lead, LONG_AGO)
    address = await db_session.scalar(
        text("SELECT to_email_normalized FROM messages WHERE draft_id = :d"),
        {"d": lead.draft_id},
    )
    await db_session.execute(
        text(
            """
            INSERT INTO suppression_entries
              (scope, normalized_value, reason, source, suppressed_at,
               legal_hold, detail, workspace_id)
            VALUES ('email', :addr, 'unsubscribe', 'test', :at,
                    false, '{}'::jsonb, :ws)
            """
        ),
        {"addr": address, "at": LONG_AGO, "ws": workspace},
    )
    await db_session.commit()

    await erase_expired(db_session, workspace_id=workspace, now=NOW)
    await db_session.commit()

    still_there = await db_session.scalar(
        text(
            "SELECT normalized_value FROM suppression_entries"
            " WHERE workspace_id = :ws AND normalized_value = :addr"
        ),
        {"ws": workspace, "addr": address},
    )
    assert still_there == address, "erasing this would re-contact somebody who opted out"


async def test_running_twice_erases_nothing_the_second_time(db_session, workspace):
    """It runs hourly. A pass that reported work every hour for ever would make
    the number meaningless and the log unreadable."""
    lead = await build_sendable(db_session, workspace, suffix="twice")
    await _send(db_session, lead, LONG_AGO)

    first = await erase_expired(db_session, workspace_id=workspace, now=NOW)
    await db_session.commit()
    second = await erase_expired(db_session, workspace_id=workspace, now=NOW)
    await db_session.commit()

    assert first.erased_anything
    assert not second.erased_anything


async def test_the_claim_map_survives_the_erasure(db_session, workspace):
    """The row is emptied, not deleted. It carries the validation report and
    the claim map -- the record of *why* the message was justified -- and
    destroying that would remove the only evidence the send was defensible."""
    lead = await build_sendable(db_session, workspace, suffix="claims")
    await _send(db_session, lead, LONG_AGO)

    await erase_expired(db_session, workspace_id=workspace, now=NOW)
    await db_session.commit()

    row = (
        await db_session.execute(
            text(
                "SELECT claim_map IS NOT NULL, validation_report IS NOT NULL"
                "  FROM message_drafts WHERE id = :id"
            ),
            {"id": lead.draft_id},
        )
    ).one()
    assert all(row), "the justification for the send was destroyed with its text"
