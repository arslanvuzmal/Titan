"""A lead nobody has measured must still get measured.

Reconstructs the starvation found on 17 September: 325 leads with websites sat
in `discovered` from 5 August, never once researched, while the pipeline
completed 548 research runs in a single day.

The cause was an ordering, not a filter. `ORDER BY latest_score DESC NULLS
LAST` puts every unscored lead behind every scored one -- and a lead that has
never been researched has no score, because scoring happens inside the research
pipeline. With thousands of qualified leads ahead of them, the LIMIT window
never reached the end.

The subtlety worth keeping: the code already refused to treat "unscored" as
"below threshold", and said so in a comment. The ordering was doing it anyway,
by starvation rather than exclusion, which is why that guard did not catch it.
So the test that matters is about *reachability*, not about the filter.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import pytest_asyncio
from titan.activities.orchestration import UNMEASURED_PATIENCE, _select_leads
from titan.db.enums import CampaignStatus, LeadStatus
from titan.db.models import Campaign, Lead, Organization

NOW = dt.datetime(2026, 9, 17, 12, 0, tzinfo=dt.UTC)


@pytest_asyncio.fixture
async def campaign(db_session, workspace) -> uuid.UUID:
    """An empty campaign, so each test controls exactly which leads exist."""
    tag = uuid.uuid4().hex[:8]
    row = Campaign(
        workspace_id=workspace,
        name=f"Unmeasured {tag}",
        slug=f"unmeasured-{tag}",
        status=CampaignStatus.ACTIVE,
    )
    db_session.add(row)
    await db_session.flush()
    return row.id


async def _lead(
    session,
    *,
    workspace_id: uuid.UUID,
    campaign_id: uuid.UUID,
    score: int | None,
    age_days: int,
    tag: str,
) -> uuid.UUID:
    unique = f"{tag}-{uuid.uuid4().hex[:6]}"
    org = Organization(
        workspace_id=workspace_id,
        display_name=f"Fixture {unique}",
        normalized_name=f"fixture {unique}",
        canonical_domain=f"{unique}.fixture-business.test",
    )
    session.add(org)
    await session.flush()
    lead = Lead(
        workspace_id=workspace_id,
        campaign_id=campaign_id,
        organization_id=org.id,
        # An unscored lead is one research has never reached; a scored one has
        # been through the pipeline at least once. That is the distinction the
        # planner was blind to.
        status=LeadStatus.DISCOVERED if score is None else LeadStatus.QUALIFIED,
        latest_score=score,
        created_at=NOW - dt.timedelta(days=age_days),
    )
    session.add(lead)
    await session.flush()
    return lead.id


@pytest.mark.asyncio
async def test_an_unmeasured_lead_is_reachable_behind_many_scored_ones(
    db_session, workspace, campaign
) -> None:
    """The defect, directly.

    Forty high-scoring leads and one that has never been measured. Under the
    old single ordering the unmeasured one sorted last behind all forty and
    could not be reached at any budget this planner ever uses.
    """
    for i in range(40):
        await _lead(
            db_session,
            workspace_id=workspace,
            campaign_id=campaign,
            score=95,
            age_days=1,
            tag=f"scored{i}",
        )
    starved = await _lead(
        db_session,
        workspace_id=workspace,
        campaign_id=campaign,
        score=None,
        age_days=40,
        tag="starved",
    )

    planned = await _select_leads(
        db_session, campaign_id=campaign, min_score=55, limit=4, now=NOW
    )

    assert str(starved) in {p.lead_id for p in planned}


@pytest.mark.asyncio
async def test_a_budget_of_one_still_reaches_it(db_session, workspace, campaign) -> None:
    """Campaigns routinely plan a single lead per cycle.

    A reservation expressed only as a percentage would round to nothing at that
    size and the starvation would survive the fix.
    """
    for i in range(10):
        await _lead(
            db_session,
            workspace_id=workspace,
            campaign_id=campaign,
            score=90,
            age_days=1,
            tag=f"s{i}",
        )
    starved = await _lead(
        db_session,
        workspace_id=workspace,
        campaign_id=campaign,
        score=None,
        age_days=30,
        tag="lonely",
    )

    planned = await _select_leads(
        db_session, campaign_id=campaign, min_score=55, limit=1, now=NOW
    )

    assert [p.lead_id for p in planned] == [str(starved)]


@pytest.mark.asyncio
async def test_a_fresh_unmeasured_lead_does_not_jump_the_queue(
    db_session, workspace, campaign
) -> None:
    """The escape is for starvation, not a standing quota.

    A lead discovered an hour ago has not been starved of anything, and letting
    it preempt a strong scored lead would just reverse who goes hungry.
    """
    strong = await _lead(
        db_session,
        workspace_id=workspace,
        campaign_id=campaign,
        score=98,
        age_days=1,
        tag="strong",
    )
    await _lead(
        db_session,
        workspace_id=workspace,
        campaign_id=campaign,
        score=None,
        age_days=0,
        tag="brandnew",
    )

    planned = await _select_leads(
        db_session, campaign_id=campaign, min_score=55, limit=1, now=NOW
    )

    assert [p.lead_id for p in planned] == [str(strong)]


@pytest.mark.asyncio
async def test_the_reservation_switches_off_once_the_backlog_drains(
    db_session, workspace, campaign
) -> None:
    """Boundary: exactly at the patience window, nothing is starving yet."""
    strong = await _lead(
        db_session,
        workspace_id=workspace,
        campaign_id=campaign,
        score=99,
        age_days=1,
        tag="best",
    )
    await _lead(
        db_session,
        workspace_id=workspace,
        campaign_id=campaign,
        score=None,
        age_days=UNMEASURED_PATIENCE.days - 1,
        tag="patient",
    )

    planned = await _select_leads(
        db_session, campaign_id=campaign, min_score=55, limit=1, now=NOW
    )

    assert [p.lead_id for p in planned] == [str(strong)]


@pytest.mark.asyncio
async def test_scored_leads_below_threshold_are_still_refused(
    db_session, workspace, campaign
) -> None:
    """The filter must survive the reordering."""
    await _lead(
        db_session,
        workspace_id=workspace,
        campaign_id=campaign,
        score=20,
        age_days=1,
        tag="weak",
    )

    planned = await _select_leads(
        db_session, campaign_id=campaign, min_score=55, limit=5, now=NOW
    )

    assert planned == []


@pytest.mark.asyncio
async def test_the_oldest_unmeasured_lead_goes_first(
    db_session, workspace, campaign
) -> None:
    """Otherwise the August backlog drains in an arbitrary order and the worst
    case never improves."""
    oldest = await _lead(
        db_session,
        workspace_id=workspace,
        campaign_id=campaign,
        score=None,
        age_days=40,
        tag="august",
    )
    await _lead(
        db_session,
        workspace_id=workspace,
        campaign_id=campaign,
        score=None,
        age_days=9,
        tag="september",
    )

    planned = await _select_leads(
        db_session, campaign_id=campaign, min_score=55, limit=1, now=NOW
    )

    assert planned[0].lead_id == str(oldest)


@pytest.mark.asyncio
async def test_a_short_scored_pool_does_not_waste_the_budget(
    db_session, workspace, campaign
) -> None:
    """If one pool cannot fill its share, the other may."""
    await _lead(
        db_session,
        workspace_id=workspace,
        campaign_id=campaign,
        score=91,
        age_days=1,
        tag="only-scored",
    )
    for i in range(4):
        await _lead(
            db_session,
            workspace_id=workspace,
            campaign_id=campaign,
            score=None,
            age_days=20 + i,
            tag=f"waiting{i}",
        )

    planned = await _select_leads(
        db_session, campaign_id=campaign, min_score=55, limit=5, now=NOW
    )

    assert len(planned) == 5
