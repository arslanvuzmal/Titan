"""Ask Places once, at full depth, then leave the question alone for a month.

Page one asked every cycle is three billed requests for the same twenty
businesses before the rate rule retires it; pages one to three asked once are
three billed requests for sixty different ones.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import update
from titan.activities import discovery
from titan.activities.discovery import FULL_DEPTH_REST, discover_leads
from titan.db.models import LeadSource
from titan.db.session import workspace_unit_of_work
from titan.intelligence.discovery import MAX_RESULTS_PER_SEARCH

from tests.activities.test_discovery import found, places_result, run_for, seed_campaign

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _search(request, result):
    """As ``run_discovery``, but hands back the query Places was asked."""
    with (
        patch("titan.activities.discovery.GooglePlacesProvider") as ProviderCls,
        patch("titan.activities.discovery.get_settings") as get_settings,
        patch("titan.activities.discovery.activity") as fake_activity,
    ):
        fake_activity.heartbeat = lambda *a, **k: None
        get_settings.return_value.google_places_api_key = "key"
        get_settings.return_value.discover_siteless = False
        get_settings.return_value.places_monthly_request_cap = 950
        get_settings.return_value.places_daily_request_cap = 40
        instance = ProviderCls.from_settings.return_value
        instance.search = AsyncMock(return_value=result)
        instance.aclose = AsyncMock()
        outcome = await discover_leads(request)
        asked = instance.search.call_args.args[0] if instance.search.call_args else None
        return outcome, asked


async def test_a_search_asks_for_every_page_and_keeps_everything_it_admits(
    db_session, workspace
) -> None:
    campaign_id = await seed_campaign(workspace, suffix="deep")
    businesses = [found(i) for i in range(1, 31)]

    outcome, asked = await _search(
        run_for(workspace, campaign_id), places_result(*businesses)
    )

    assert asked is not None
    assert asked.max_results == MAX_RESULTS_PER_SEARCH
    # Thirty admissible businesses, thirty leads -- not the first twenty.
    assert outcome.leads_created == 30


async def test_a_full_depth_query_rests_for_thirty_days(db_session, workspace) -> None:
    campaign_id = await seed_campaign(workspace, suffix="rest")
    async with workspace_unit_of_work(workspace) as session:
        session.add(
            LeadSource(
                workspace_id=workspace,
                kind=discovery.SOURCE_KIND,
                label="dentists in Leeds UK",
                campaign_id=campaign_id,
                # Eight results and most of them new: the old rate rule alone
                # would say "ask again". Full depth says there is nothing more.
                records_returned=8,
                records_deduplicated=1,
                query_parameters={"full_depth": True, "pages_fetched": 1},
            )
        )

    async with workspace_unit_of_work(workspace) as session:
        spent = await discovery._exhausted_geographies(
            session, campaign_id=campaign_id, business_type="dentists"
        )
    assert "leeds uk" in spent


async def test_after_the_rest_the_query_may_be_asked_again(db_session, workspace) -> None:
    campaign_id = await seed_campaign(workspace, suffix="rested")
    async with workspace_unit_of_work(workspace) as session:
        source = LeadSource(
            workspace_id=workspace,
            kind=discovery.SOURCE_KIND,
            label="dentists in York UK",
            campaign_id=campaign_id,
            records_returned=8,
            records_deduplicated=1,
            query_parameters={"full_depth": True},
        )
        session.add(source)
        await session.flush()
        await session.execute(
            update(LeadSource)
            .where(LeadSource.id == source.id)
            .values(
                created_at=dt.datetime.now(dt.UTC)
                - FULL_DEPTH_REST
                - dt.timedelta(days=1)
            )
        )

    async with workspace_unit_of_work(workspace) as session:
        spent = await discovery._exhausted_geographies(
            session, campaign_id=campaign_id, business_type="dentists"
        )
    assert "york uk" not in spent


async def test_page_one_searches_from_before_keep_the_old_rule(
    db_session, workspace
) -> None:
    """Rows written before full depth carry no flag, and are judged as before."""
    campaign_id = await seed_campaign(workspace, suffix="legacy")
    async with workspace_unit_of_work(workspace) as session:
        session.add(
            LeadSource(
                workspace_id=workspace,
                kind=discovery.SOURCE_KIND,
                label="dentists in Hull UK",
                campaign_id=campaign_id,
                records_returned=8,
                records_deduplicated=1,
                query_parameters={},
            )
        )

    async with workspace_unit_of_work(workspace) as session:
        spent = await discovery._exhausted_geographies(
            session, campaign_id=campaign_id, business_type="dentists"
        )
    assert "hull uk" not in spent


def test_the_rest_is_a_month() -> None:
    assert FULL_DEPTH_REST == dt.timedelta(days=30)


# ------------------------------------------------------------------ the cap
def test_the_cap_needs_room_for_a_full_depth_search() -> None:
    assert (
        discovery.places_cap_refusal(
            used_month=948, used_today=0, monthly_cap=950, daily_cap=40
        )
        is not None
    )
    assert (
        discovery.places_cap_refusal(
            used_month=947, used_today=0, monthly_cap=950, daily_cap=40
        )
        is None
    )
    refusal = discovery.places_cap_refusal(
        used_month=0, used_today=38, monthly_cap=950, daily_cap=40
    )
    assert refusal is not None and "daily" in refusal


async def test_searches_are_counted_by_the_pages_they_cost(db_session, workspace) -> None:
    campaign_id = await seed_campaign(workspace, suffix="cap")
    async with workspace_unit_of_work(workspace) as session:
        for pages in (3, 2):
            session.add(
                LeadSource(
                    workspace_id=workspace,
                    kind=discovery.SOURCE_KIND,
                    label="dentists in Leeds UK",
                    campaign_id=campaign_id,
                    query_parameters={"full_depth": True, "pages_fetched": pages},
                )
            )
        # An older row, written before pages were stamped: costs one.
        session.add(
            LeadSource(
                workspace_id=workspace,
                kind=discovery.SOURCE_KIND,
                label="dentists in York UK",
                campaign_id=campaign_id,
                query_parameters={},
            )
        )

    async with workspace_unit_of_work(workspace) as session:
        month, today = await discovery.places_requests_used(
            session, workspace_id=workspace, now=dt.datetime.now(dt.UTC)
        )
    assert (month, today) == (6, 6)


async def test_a_search_over_the_cap_is_refused_before_it_is_billed(
    db_session, workspace
) -> None:
    campaign_id = await seed_campaign(workspace, suffix="capped")
    async with workspace_unit_of_work(workspace) as session:
        session.add(
            LeadSource(
                workspace_id=workspace,
                kind=discovery.SOURCE_KIND,
                label="earlier",
                campaign_id=campaign_id,
                query_parameters={"pages_fetched": 40},
            )
        )
    outcome, asked = await _search(
        run_for(workspace, campaign_id), places_result(found(1))
    )
    assert asked is None, "Places was called past the cap"
    assert outcome.refused_reason is not None and "cap" in outcome.refused_reason
