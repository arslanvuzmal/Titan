"""Recording which carrier campaign each market's leads leave through.

Creating the per-market campaigns in Smartlead is only half the job. Until the
ids are written onto Titan's own campaigns, ``campaigns.smartlead_campaign_id``
is null everywhere and the delivery path falls back to the single carrier in
``TITAN_SMARTLEAD_CAMPAIGN_ID`` -- so the markets exist, nothing uses them, and
a Dubai recipient is still scheduled to London hours.

Against a real PostgreSQL, because the writeback goes through the workspace
guard and the point of it is which rows it does and does not touch.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from titan.db.enums import CampaignStatus, Industry, Region
from titan.db.models import Campaign, CampaignPolicy
from titan.db.session import get_sessionmaker
from titan.provision_smartlead import record_carriers

pytestmark = pytest.mark.asyncio


async def seed_campaign(
    workspace_id: uuid.UUID, *, suffix: str, region: Region
) -> uuid.UUID:
    async with get_sessionmaker()() as session, session.begin():
        campaign = Campaign(
            workspace_id=workspace_id,
            name=f"Carrier {suffix}",
            slug=f"carrier-{suffix}",
            status=CampaignStatus.ACTIVE,
            industry=Industry.DENTIST,
            region=region,
        )
        session.add(campaign)
        await session.flush()
        session.add(CampaignPolicy(workspace_id=workspace_id, campaign_id=campaign.id))
        return campaign.id


async def carrier_of(campaign_id: uuid.UUID) -> int | None:
    async with get_sessionmaker()() as session:
        campaign = (
            await session.execute(select(Campaign).where(Campaign.id == campaign_id))
        ).scalar_one()
        return campaign.smartlead_campaign_id


async def test_each_market_gets_its_own_carrier(workspace) -> None:
    """The whole point: a lead leaves through the campaign for its market."""
    uk = await seed_campaign(workspace, suffix="uk", region=Region.UK)
    dubai = await seed_campaign(workspace, suffix="me", region=Region.MIDDLE_EAST)

    updated = await record_carriers(workspace, {Region.UK: 101, Region.MIDDLE_EAST: 202})

    assert updated == {Region.UK: 1, Region.MIDDLE_EAST: 1}
    assert await carrier_of(uk) == 101
    assert await carrier_of(dubai) == 202


async def test_a_market_with_no_carrier_is_left_alone(workspace) -> None:
    """Null is meaningful: it means "use the configured default", not "broken".

    A market nobody provisioned must not be blanked or guessed at -- it keeps
    falling back, which is exactly the behaviour it had before markets existed.
    """
    usa = await seed_campaign(workspace, suffix="usa", region=Region.USA)

    await record_carriers(workspace, {Region.UK: 101})

    assert await carrier_of(usa) is None


async def test_every_campaign_in_a_market_is_pointed_at_it(workspace) -> None:
    """A market has more than one campaign; all of them share its clock."""
    first = await seed_campaign(workspace, suffix="uk-a", region=Region.UK)
    second = await seed_campaign(workspace, suffix="uk-b", region=Region.UK)

    updated = await record_carriers(workspace, {Region.UK: 101})

    assert updated == {Region.UK: 2}
    assert await carrier_of(first) == 101
    assert await carrier_of(second) == 101


async def test_another_workspace_is_not_touched(workspace, second_workspace) -> None:
    """The writeback is scoped, so provisioning one tenant cannot reroute another.

    The Smartlead API key is global to the account, so nothing downstream would
    have refused the write -- the scoping here is the only thing stopping it.
    """
    mine = await seed_campaign(workspace, suffix="uk-mine", region=Region.UK)
    theirs = await seed_campaign(second_workspace, suffix="uk-theirs", region=Region.UK)

    updated = await record_carriers(workspace, {Region.UK: 101})

    assert updated == {Region.UK: 1}
    assert await carrier_of(mine) == 101
    assert await carrier_of(theirs) is None
