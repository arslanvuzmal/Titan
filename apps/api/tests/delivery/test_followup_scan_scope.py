"""A scan that mutates rows must own the rows it mutates.

Found on the live pipeline. ``plan_campaign_cycle`` runs once per campaign, and
the first thing it does is a follow-up scan -- which was workspace-wide. With 27
active campaigns and 79 contacted leads, every cycle selected the *same* 79 rows
(ordered by ``last_contacted_at``, so literally the same ones), wrote
``next_action_at`` and ``status_reason`` on each, and committed under optimistic
version locking.

Whichever committed second lost::

    sqlalchemy.orm.exc.StaleDataError: UPDATE statement on table 'leads'
    expected to update 49 row(s); 0 were matched.

The exception propagated out of the activity, so the campaign did no research
and queued no sends for that cycle -- 279 activity failures in an afternoon,
recorded only as the generic "Activity task failed".

``leads.campaign_id`` is NOT NULL, so scoping the scan to one campaign makes the
row sets disjoint and two campaigns can no longer contend for a row.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from titan.db.models import Lead
from titan.db.session import workspace_unit_of_work
from titan.delivery.followup_scheduler import FollowUpScheduler

from tests.delivery.conftest import build_sendable

CONTACTED_DAYS_AGO = 5


async def make_contacted(session, workspace, suffix: str):
    """A lead that has been written to and has not replied -- the only kind the
    scan considers, and therefore the only kind that can collide."""
    fixture = await build_sendable(session, workspace, suffix=suffix)
    lead = await session.get(Lead, fixture.lead_id)
    lead.last_contacted_at = dt.datetime.now(dt.UTC) - dt.timedelta(
        days=CONTACTED_DAYS_AGO
    )
    lead.replied_at = None
    lead.next_action_at = None
    await session.commit()
    return fixture


async def test_the_scan_only_touches_its_own_campaigns_leads(
    db_session, workspace
) -> None:
    """The property the whole change exists for.

    Two campaigns, one contacted lead each. Scanning the first must leave the
    second's lead exactly as it found it -- untouched, not merely unchanged in
    value, because it is the *write* that takes the version and causes the
    collision.
    """
    mine = await make_contacted(db_session, workspace, "scope-mine")
    theirs = await make_contacted(db_session, workspace, "scope-theirs")

    before = (
        await db_session.execute(select(Lead.version).where(Lead.id == theirs.lead_id))
    ).scalar_one()

    async with workspace_unit_of_work(workspace) as session:
        results = await FollowUpScheduler().scan(
            session, workspace, campaign_id=mine.campaign_id
        )

    assert {r.lead_id for r in results} == {mine.lead_id}

    after = (
        await db_session.execute(select(Lead.version).where(Lead.id == theirs.lead_id))
    ).scalar_one()
    assert after == before, "the other campaign's lead was written to"


async def test_two_campaigns_scanning_in_turn_do_not_collide(
    db_session, workspace
) -> None:
    """The failure itself, reproduced at the level it happened.

    Both campaigns scan and commit against the same workspace. Unscoped, the
    second commit raised StaleDataError because the first had already bumped
    the shared rows' versions. Scoped, they write disjoint sets and both land.
    """
    first = await make_contacted(db_session, workspace, "collide-a")
    second = await make_contacted(db_session, workspace, "collide-b")

    async with workspace_unit_of_work(workspace) as session:
        await FollowUpScheduler().scan(session, workspace, campaign_id=first.campaign_id)

    # No exception is the assertion. Before the fix this raised.
    async with workspace_unit_of_work(workspace) as session:
        await FollowUpScheduler().scan(session, workspace, campaign_id=second.campaign_id)

    async with workspace_unit_of_work(workspace) as session:
        for lead_id in (first.lead_id, second.lead_id):
            lead = await session.get(Lead, lead_id)
            assert lead.next_action_at is not None or lead.status_reason is not None, (
                "a scanned lead should carry a decision or a recorded reason"
            )


async def test_a_replied_lead_is_never_scanned(db_session, workspace) -> None:
    """Unchanged behaviour, asserted because the where-clause was rewritten.

    A reply ends the sequence. Scanning one would be the scheduler proposing a
    follow-up to somebody who already answered.
    """
    fixture = await make_contacted(db_session, workspace, "replied")
    lead = await db_session.get(Lead, fixture.lead_id)
    lead.replied_at = dt.datetime.now(dt.UTC)
    await db_session.commit()

    async with workspace_unit_of_work(workspace) as session:
        results = await FollowUpScheduler().scan(
            session, workspace, campaign_id=fixture.campaign_id
        )

    assert results == []


async def test_a_never_contacted_lead_is_never_scanned(db_session, workspace) -> None:
    """Also unchanged, also in the rewritten clause. A follow-up follows
    something."""
    fixture = await build_sendable(db_session, workspace, suffix="uncontacted")
    lead = await db_session.get(Lead, fixture.lead_id)
    lead.last_contacted_at = None
    await db_session.commit()

    async with workspace_unit_of_work(workspace) as session:
        results = await FollowUpScheduler().scan(
            session, workspace, campaign_id=fixture.campaign_id
        )

    assert results == []


async def test_the_limit_applies_per_campaign_not_across_the_workspace(
    db_session, workspace
) -> None:
    """The quieter half of the same bug.

    A workspace-wide ``limit`` lets the oldest-contacted leads crowd out the
    rest, so a campaign whose leads fall outside the global top-N would never be
    scanned at all -- silently, because an unscanned lead looks exactly like a
    lead with nothing owed. Per campaign, a small limit still reaches each one.
    """
    older = await make_contacted(db_session, workspace, "crowd-older")
    newer = await make_contacted(db_session, workspace, "crowd-newer")

    # Make the first campaign's lead unambiguously the oldest, so under a
    # workspace-wide limit of 1 it would take the only slot.
    lead = await db_session.get(Lead, older.lead_id)
    lead.last_contacted_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=90)
    await db_session.commit()

    async with workspace_unit_of_work(workspace) as session:
        results = await FollowUpScheduler().scan(
            session, workspace, campaign_id=newer.campaign_id, limit=1
        )

    assert [r.lead_id for r in results] == [newer.lead_id]


@pytest.mark.parametrize("campaign_id", [None])
async def test_an_unscoped_scan_still_covers_the_workspace(
    db_session, workspace, campaign_id
) -> None:
    """The parameter is optional and defaults to the old behaviour.

    Kept deliberately: the send path always scopes, but a workspace-wide sweep
    is a legitimate thing to want from a CLI or a backfill, and silently
    scoping it to nothing would be worse than the collision.
    """
    first = await make_contacted(db_session, workspace, "wide-a")
    second = await make_contacted(db_session, workspace, "wide-b")

    async with workspace_unit_of_work(workspace) as session:
        results = await FollowUpScheduler().scan(
            session, workspace, campaign_id=campaign_id
        )

    assert {first.lead_id, second.lead_id} <= {r.lead_id for r in results}
