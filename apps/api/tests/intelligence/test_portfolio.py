"""The portfolio view: region mapping, the aggregate, and the query behind it.

The row this view exists to produce is the boring-looking one: a market with
active campaigns that sent nothing. Every campaign page in it looks fine, which
is exactly why nobody notices, and it is the only place in the report where that
state is visible at all.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import update
from titan.activities.reporting import _portfolio_slices
from titan.db.enums import SCHEDULABLE_REGIONS, CampaignStatus, Region
from titan.db.models import Campaign, Message
from titan.db.session import get_sessionmaker
from titan.intelligence.portfolio import (
    Portfolio,
    RegionSlice,
    describe,
    disagrees_with_country,
    region_for_country,
    summarise,
)

from tests.delivery.conftest import build_sendable

NOW = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.UTC)
SINCE = NOW - dt.timedelta(days=7)


# ==========================================================================
# Country to market
# ==========================================================================
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("US", Region.USA),
        ("CA", Region.CANADA),
        ("GB", Region.UK),
        ("UK", Region.UK),  # not ISO, written by humans anyway
        ("gb", Region.UK),
        (" GB ", Region.UK),
        ("DE", Region.EUROPE),
        ("IE", Region.EUROPE),
        ("AU", Region.AUSTRALIA),
        ("NZ", Region.AUSTRALIA),
        ("AE", Region.MIDDLE_EAST),
    ],
)
def test_known_countries_map_to_their_market(code: str, expected: Region) -> None:
    assert region_for_country(code) is expected


def test_an_unset_country_is_unspecified_not_other() -> None:
    """Different facts. UNSPECIFIED is a campaign nobody has told us about;
    OTHER is one aimed somewhere the schedule has no opinion on."""
    assert region_for_country(None) is Region.UNSPECIFIED
    assert region_for_country("") is Region.UNSPECIFIED
    assert region_for_country("   ") is Region.UNSPECIFIED


def test_an_unrecognised_country_is_other() -> None:
    assert region_for_country("ZZ") is Region.OTHER
    assert region_for_country("JP") is Region.OTHER


def test_the_schedulable_set_excludes_the_two_that_have_no_business_day() -> None:
    """There is no local working day for "somewhere", and pretending otherwise
    is how mail gets sent at 3am."""
    assert Region.OTHER not in SCHEDULABLE_REGIONS
    assert Region.UNSPECIFIED not in SCHEDULABLE_REGIONS
    assert len(SCHEDULABLE_REGIONS) == 6


# ==========================================================================
# Region against country code
# ==========================================================================
def test_a_contradiction_is_reported() -> None:
    assert disagrees_with_country(Region.USA, "GB") is True


def test_agreement_is_not_a_contradiction() -> None:
    assert disagrees_with_country(Region.UK, "GB") is False


def test_europe_declared_with_one_european_country_is_not_a_contradiction() -> None:
    """A campaign aimed at Europe naming Germany first is a legitimate pairing,
    and silently rewriting either would destroy a deliberate choice."""
    assert disagrees_with_country(Region.EUROPE, "DE") is False
    assert disagrees_with_country(Region.EUROPE, "FR") is False


def test_nothing_is_contradicted_when_either_side_says_nothing() -> None:
    assert disagrees_with_country(Region.UNSPECIFIED, "GB") is False
    assert disagrees_with_country(Region.OTHER, "GB") is False
    assert disagrees_with_country(Region.UK, None) is False
    assert disagrees_with_country(Region.UK, "ZZ") is False


# ==========================================================================
# The aggregate
# ==========================================================================
def slice_(region: Region, **kw) -> RegionSlice:
    return RegionSlice(region=region, **kw)


def test_markets_are_ordered_by_share_of_sending() -> None:
    small = slice_(Region.USA, sent=10, campaigns=1, active_campaigns=1)
    large = slice_(Region.UK, sent=90, campaigns=1, active_campaigns=1)

    ordered = summarise([small, large]).slices

    assert [s.region for s in ordered] == [Region.UK, Region.USA]


def test_share_of_sending_is_of_the_whole_portfolio() -> None:
    book = summarise([slice_(Region.UK, sent=75), slice_(Region.USA, sent=25)])

    assert book.share_of_sending(Region.UK) == 0.75
    assert book.share_of_sending(Region.USA) == 0.25
    assert book.share_of_sending(Region.CANADA) == 0.0


def test_a_portfolio_that_sent_nothing_reports_no_share() -> None:
    """Rather than dividing by zero, or reporting an even split of nothing."""
    book = summarise([slice_(Region.UK), slice_(Region.USA)])

    assert book.sent == 0
    assert book.share_of_sending(Region.UK) == 0.0


def test_a_configured_market_that_sent_nothing_is_idle_not_absent() -> None:
    """The row the whole view exists for. Every campaign page in this market
    looks fine, which is why nobody notices."""
    book = summarise(
        [
            slice_(Region.UK, sent=40, campaigns=2, active_campaigns=2),
            slice_(Region.USA, sent=0, campaigns=3, active_campaigns=3, leads=60),
        ]
    )

    assert [s.region for s in book.working] == [Region.UK]
    assert [s.region for s in book.idle] == [Region.USA]
    assert "nothing sent" in describe(book.idle[0], book)


def test_a_market_with_no_active_campaigns_is_not_idle() -> None:
    """Nothing is wrong with a market nobody has switched on. Reporting it as
    idle would put a permanent item on a list read for exceptions."""
    book = summarise([slice_(Region.CANADA, campaigns=2, active_campaigns=0)])

    assert book.idle == ()
    assert "none active" in describe(book.slices[0], book)


def test_rates_are_computed_on_the_right_denominators() -> None:
    working = slice_(Region.UK, sent=100, bounced=4, contacted=40, replied=8)

    assert working.bounce_rate == 0.04
    assert working.reply_rate == 0.2


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"campaigns": 1},
        {"campaigns": 1, "active_campaigns": 1},
        {"campaigns": 1, "active_campaigns": 1, "leads": 5},
        {"sent": 10, "bounced": 1, "contacted": 5, "replied": 1},
    ],
)
def test_every_market_describes_itself(kw: dict) -> None:
    book = summarise([slice_(Region.UK, **kw)])
    line = describe(book.slices[0], book)

    assert line
    assert "None" not in line


# ==========================================================================
# The query
# ==========================================================================
async def _campaign_in(
    session, workspace_id, *, region: Region, suffix: str, active=True
):
    fixture = await build_sendable(session, workspace_id, suffix=suffix)
    await session.execute(
        update(Campaign)
        .where(Campaign.id == fixture.campaign_id)
        .values(
            region=region,
            status=CampaignStatus.ACTIVE if active else CampaignStatus.DRAFT,
        )
    )
    await session.execute(
        update(Message).where(Message.id == fixture.message_id).values(sent_at=NOW)
    )
    await session.commit()
    return fixture


@pytest.mark.asyncio
async def test_the_query_groups_this_week_by_market(db_session, workspace) -> None:
    """The regression guard. _portfolio_slices fails soft, so an empty result
    is indistinguishable from a broken query."""
    await _campaign_in(db_session, workspace, region=Region.UK, suffix="pf1")
    await _campaign_in(db_session, workspace, region=Region.UK, suffix="pf2")
    await _campaign_in(db_session, workspace, region=Region.USA, suffix="pf3")

    slices = await _portfolio_slices(db_session, workspace, SINCE)
    by_region = {s.region: s for s in slices}

    assert by_region, "the query returned nothing for three campaigns"
    assert by_region[Region.UK].campaigns == 2
    assert by_region[Region.UK].sent == 2
    assert by_region[Region.USA].sent == 1

    book = summarise(slices)
    assert book.share_of_sending(Region.UK) == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_campaign_counts_are_current_state_not_windowed(
    db_session, workspace
) -> None:
    """A market with active campaigns and no sends must still appear. Windowing
    the campaigns would erase the row by reporting the market as absent."""
    fixture = await _campaign_in(
        db_session, workspace, region=Region.CANADA, suffix="pfold"
    )
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(Message)
            .where(Message.id == fixture.message_id)
            .values(sent_at=NOW - dt.timedelta(days=60))
        )

    slices = await _portfolio_slices(db_session, workspace, SINCE)
    canada = next(s for s in slices if s.region is Region.CANADA)

    assert canada.active_campaigns == 1
    assert canada.sent == 0
    assert canada.is_working is False
    assert summarise(slices).idle


@pytest.mark.asyncio
async def test_a_lead_with_several_messages_is_still_one_contacted_lead(
    db_session, workspace
) -> None:
    fixture = await _campaign_in(db_session, workspace, region=Region.UK, suffix="pfm")
    extra = await build_sendable(db_session, workspace, suffix="pfmx")
    async with get_sessionmaker()() as s, s.begin():
        # Re-point the second message at the first lead, so one lead has two.
        await s.execute(
            update(Message)
            .where(Message.id == extra.message_id)
            .values(lead_id=fixture.lead_id, sent_at=NOW)
        )
        # Park the now-message-less second campaign in another market rather
        # than orphaning its lead: leads.campaign_id is NOT NULL, and a lead
        # with no campaign is not a state this schema allows.
        await s.execute(
            update(Campaign)
            .where(Campaign.id == extra.campaign_id)
            .values(region=Region.CANADA)
        )

    slices = await _portfolio_slices(db_session, workspace, SINCE)
    uk = next(s for s in slices if s.region is Region.UK)

    assert uk.contacted == 1
    assert uk.sent == 2


@pytest.mark.asyncio
async def test_another_workspace_is_not_in_the_portfolio(db_session, workspace) -> None:
    from titan.db.models import Workspace

    other = Workspace(name="Other", slug=f"o-{uuid.uuid4().hex[:12]}")
    db_session.add(other)
    await db_session.commit()
    other_id = other.id
    try:
        await _campaign_in(db_session, other_id, region=Region.AUSTRALIA, suffix="pfiso")

        mine = await _portfolio_slices(db_session, workspace, SINCE)
        assert all(s.region is not Region.AUSTRALIA for s in mine)
    finally:
        from sqlalchemy import delete

        await db_session.rollback()
        await db_session.execute(delete(Workspace).where(Workspace.id == other_id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_a_new_campaign_defaults_to_unspecified(db_session, workspace) -> None:
    """The column default, and what every campaign predating it carries."""
    fixture = await build_sendable(db_session, workspace, suffix="pfdefault")
    await db_session.commit()

    async with get_sessionmaker()() as s:
        campaign = await s.get(Campaign, fixture.campaign_id)
    assert campaign is not None
    assert campaign.region is Region.UNSPECIFIED


def test_the_portfolio_dataclass_is_empty_by_default() -> None:
    assert Portfolio().slices == ()
    assert Portfolio().sent == 0
