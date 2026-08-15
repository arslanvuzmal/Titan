"""A campaign declares its market, and gets that market's clock and week.

Two things were true at once before this. The region machinery was complete --
a market table, timezone bands beneath it, per-campaign window columns, a
four-level timezone resolution -- and the API had no field to set a region with.
So every campaign created through it was ``unspecified``, which resolves to no
timezone at all, which means every lead whose own timezone Places never resolved
was refused for having no clock to measure against. The feature existed and
nothing could reach it.

And the working week had been set per market exactly once, by a backfill.
Campaigns created after that migration were Monday to Friday whatever market
they were aimed at, because creation never learned the rule the backfill
applied.

Requests go through the real ASGI app with a real database.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from titan.db.enums import Region, SubRegion, WorkspaceRole
from titan.db.models import AuditLog, Campaign, CampaignPolicy
from titan.db.session import get_sessionmaker
from titan.policy.schedule import (
    SUNDAY_TO_THURSDAY,
    default_window_for,
    resolve_timezone,
)

from .test_api_security import auth, make_member, slug_of, token_for

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client():
    import os

    os.environ.setdefault("TITAN_LOCAL_JWT_SECRET", "test-secret-not-for-production")
    from titan.config import get_settings

    get_settings.cache_clear()
    from titan.api.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as http_client:
        yield http_client


async def create(client, token, **body):
    body.setdefault("name", "Regional")
    body.setdefault("slug", f"region-{uuid.uuid4().hex[:8]}")
    return await client.post("/api/v1/campaigns", headers=auth(token), json=body)


async def policy_of(campaign_id: str) -> CampaignPolicy:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(CampaignPolicy).where(
                    CampaignPolicy.campaign_id == uuid.UUID(campaign_id)
                )
            )
        ).scalar_one()


# ==========================================================================
# The market reaches the campaign at all
# ==========================================================================
@pytest.mark.asyncio
async def test_a_campaign_can_declare_its_market(client, db_session, workspace) -> None:
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="mkt")
    token = await token_for(client, email, await slug_of(workspace))

    created = await create(client, token, region="middle_east", sub_region="unspecified")
    assert created.status_code == 201

    async with get_sessionmaker()() as session:
        campaign = await session.get(Campaign, uuid.UUID(created.json()["id"]))

    assert campaign is not None
    assert campaign.region is Region.MIDDLE_EAST


@pytest.mark.asyncio
async def test_a_campaign_can_declare_a_band_inside_its_market(
    client, db_session, workspace
) -> None:
    """The Eastern default is three hours early for a campaign selling to
    California, and the market alone cannot say so."""
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="band")
    token = await token_for(client, email, await slug_of(workspace))

    created = await create(client, token, region="usa", sub_region="us_pacific")
    assert created.status_code == 201

    async with get_sessionmaker()() as session:
        campaign = await session.get(Campaign, uuid.UUID(created.json()["id"]))

    assert campaign is not None
    assert campaign.sub_region is SubRegion.US_PACIFIC
    assert (
        resolve_timezone(None, campaign.region, campaign_subregion=campaign.sub_region)
        == "America/Los_Angeles"
    )


@pytest.mark.asyncio
async def test_an_unknown_market_is_refused_rather_than_ignored(
    client, db_session, workspace
) -> None:
    """Falling back would leave the campaign with no market -- and therefore no
    clock -- while the request looked like it had worked."""
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="bad")
    token = await token_for(client, email, await slug_of(workspace))

    refused = await create(client, token, region="antarctica")

    assert refused.status_code == 422
    assert "middle_east" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_omitting_the_market_is_still_allowed(
    client, db_session, workspace
) -> None:
    """Adding a required field would break every existing caller. Unspecified
    remains legal and keeps the conservative window."""
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="omit")
    token = await token_for(client, email, await slug_of(workspace))

    created = await create(client, token)

    assert created.status_code == 201


# ==========================================================================
# The window is derived, not defaulted
# ==========================================================================
@pytest.mark.asyncio
async def test_a_middle_east_campaign_is_created_working_sunday_to_thursday(
    client, db_session, workspace
) -> None:
    """The rule the one-off backfill applied and creation never learned. A
    Monday-to-Friday Gulf campaign sends on the two days its recipients are not
    working and skips the two they are."""
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="gulf")
    token = await token_for(client, email, await slug_of(workspace))

    created = await create(client, token, region="middle_east")
    policy = await policy_of(created.json()["id"])

    assert tuple(policy.send_days) == SUNDAY_TO_THURSDAY
    assert 4 not in policy.send_days, "a Gulf campaign was created sending on Friday"
    assert 6 in policy.send_days, "a Gulf campaign was created skipping Sunday"


@pytest.mark.asyncio
async def test_a_campaign_opens_an_hour_before_its_own_market_works(
    client, db_session, workspace
) -> None:
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="lead")
    token = await token_for(client, email, await slug_of(workspace))

    for region in (Region.USA, Region.EUROPE, Region.MIDDLE_EAST):
        created = await create(client, token, region=region.value)
        policy = await policy_of(created.json()["id"])
        expected = default_window_for(region)

        assert policy.send_window_start_hour == expected.start_hour, region.value
        assert policy.send_window_end_hour == expected.end_hour, region.value


@pytest.mark.asyncio
async def test_a_european_campaign_opens_at_seven_and_a_us_one_at_eight(
    client, db_session, workspace
) -> None:
    """The single ``8`` was an hour of lead-in for one market and none for the
    other, and nothing recorded which was meant."""
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="split")
    token = await token_for(client, email, await slug_of(workspace))

    europe = await policy_of((await create(client, token, region="europe")).json()["id"])
    usa = await policy_of((await create(client, token, region="usa")).json()["id"])

    assert europe.send_window_start_hour == 7
    assert usa.send_window_start_hour == 8


@pytest.mark.asyncio
async def test_a_gulf_campaign_may_send_until_six(client, db_session, workspace) -> None:
    """The other half of what one pair of numbers cost: an hour of the Middle
    East working day was unreachable."""
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="dusk")
    token = await token_for(client, email, await slug_of(workspace))

    policy = await policy_of(
        (await create(client, token, region="middle_east")).json()["id"]
    )

    assert policy.send_window_end_hour == 18


@pytest.mark.asyncio
async def test_the_window_is_visible_to_whoever_created_it(
    client, db_session, workspace
) -> None:
    """A derived value nobody can see is one nobody can check."""
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="see")
    token = await token_for(client, email, await slug_of(workspace))

    created = await create(client, token, region="middle_east")
    body = (
        await client.get(
            f"/api/v1/campaigns/{created.json()['id']}/policy", headers=auth(token)
        )
    ).json()

    assert body["send_window_start_hour"] == 8
    assert body["send_window_end_hour"] == 18
    assert tuple(body["send_days"]) == SUNDAY_TO_THURSDAY


@pytest.mark.asyncio
async def test_the_reason_for_the_hours_is_audited(client, db_session, workspace) -> None:
    """08:00 could be a 09:00 market with an hour's lead-in or a market that
    starts at eight. The stored hours cannot tell you which, so the derivation
    is recorded at the one moment it is still known."""
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="why")
    token = await token_for(client, email, await slug_of(workspace))

    created = await create(client, token, region="europe")

    async with get_sessionmaker()() as session:
        entry = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.resource_id == created.json()["id"])
                .where(AuditLog.action == "campaign.create")
            )
        ).scalar_one()

    assert entry.detail["region"] == "europe"
    assert "08:00-17:00" in entry.detail["send_window_reason"]
    assert "opens 07:00" in entry.detail["send_window_reason"]


# ==========================================================================
# What a market buys at send time
# ==========================================================================
@pytest.mark.asyncio
async def test_declaring_a_market_gives_leads_a_clock_they_did_not_have(
    client, db_session, workspace
) -> None:
    """The consequence of the missing field. Without a market the campaign has
    no timezone, and the send-window check refuses every lead whose own timezone
    is unknown -- which is a great many of them."""
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="clock")
    token = await token_for(client, email, await slug_of(workspace))

    silent = await create(client, token)
    declared = await create(client, token, region="uk")

    async with get_sessionmaker()() as session:
        without = await session.get(Campaign, uuid.UUID(silent.json()["id"]))
        with_market = await session.get(Campaign, uuid.UUID(declared.json()["id"]))

    assert without is not None and with_market is not None
    assert resolve_timezone(None, without.region) is None
    assert resolve_timezone(None, with_market.region) == "Europe/London"
