"""The rollups, over the API.

Phase 01's whole complaint is that things get built and never wired: a poller
that was never scheduled, sequence steps nothing created, a local-frame column
with a partial index over an empty set. A query layer no route can reach is the
same defect, so the route is tested rather than assumed.

The other assertion here is about *nulls*. A slice below the sample floor has no
rate, and the API must return null rather than 0.0 -- a client that renders
"0% bounced" for a mailbox nobody has measured is worse than one that says
nothing, because it looks like good news.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import update
from titan.db.enums import WorkspaceRole
from titan.db.models import Message
from titan.delivery.deliverability import MIN_SAMPLE_FOR_RATES

from tests.delivery.conftest import build_sendable

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


async def _headers(client, workspace, *, tag: str) -> dict[str, str]:
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag=tag)
    return auth(await token_for(client, email, await slug_of(workspace)))


async def test_every_grouping_is_reachable(client, workspace) -> None:
    """Six dimensions, one route. Omitting the parameter returns them all."""
    headers = await _headers(client, workspace, tag="roll1")

    response = await client.get("/api/v1/analytics/outcomes", headers=headers)

    assert response.status_code == 200
    dimensions = {row["dimension"] for row in response.json()}
    assert dimensions == {
        "campaign",
        "sender",
        "recipient_domain",
        "lead_source",
        "local_slot",
        "variant",
    }


async def test_one_grouping_can_be_asked_for(client, workspace) -> None:
    headers = await _headers(client, workspace, tag="roll2")

    response = await client.get(
        "/api/v1/analytics/outcomes",
        params={"dimension": "local_slot"},
        headers=headers,
    )

    body = response.json()
    assert response.status_code == 200
    assert len(body) == 1
    assert body[0]["dimension"] == "local_slot"
    assert body[0]["sample_floor"] == MIN_SAMPLE_FOR_RATES


async def test_an_unknown_grouping_is_refused_rather_than_ignored(
    client, workspace
) -> None:
    """Silently returning every dimension for a typo would be worse than a 422.

    The caller asked a specific question and would be shown an answer to a
    different one.
    """
    headers = await _headers(client, workspace, tag="roll3")

    response = await client.get(
        "/api/v1/analytics/outcomes",
        params={"dimension": "not_a_dimension"},
        headers=headers,
    )

    assert response.status_code == 422


async def test_a_thin_slice_reports_a_null_rate_not_zero(
    client, db_session, workspace
) -> None:
    """Null means "not measured". Zero means "measured, and clean".

    Collapsing the two is how an unmeasured mailbox reads as a healthy one.
    """
    from sqlalchemy import update
    from titan.db.models import Message

    built = await build_sendable(db_session, workspace, suffix="thinslice")
    await db_session.execute(
        update(Message)
        .where(Message.id == built.message_id)
        .values(sent_at=dt.datetime.now(dt.UTC))
    )
    await db_session.commit()

    headers = await _headers(client, workspace, tag="roll4")
    response = await client.get(
        "/api/v1/analytics/outcomes",
        params={"dimension": "campaign"},
        headers=headers,
    )

    slices = response.json()[0]["slices"]
    assert slices, "the send did not appear; the test proves nothing"
    thin = slices[0]
    assert thin["sent"] < MIN_SAMPLE_FOR_RATES
    assert thin["has_signal"] is False
    assert thin["bounce_rate"] is None
    assert thin["positive_reply_rate"] is None


async def test_the_rollups_require_authentication(client) -> None:
    response = await client.get("/api/v1/analytics/outcomes")

    assert response.status_code in (401, 403)


# ----------------------------------------------------- the three judgements
#
# timing.py, experiments.py and portfolio.py were each written, tested, and
# imported by nothing. These assert the routes that finally call them.


async def test_the_timing_report_is_reachable(client, workspace) -> None:
    """`timing.py` deferred wiring because there was no history to read.

    Reconciling Smartlead's sends gave every real message a local weekday and
    hour, so the reason for the deferral no longer holds.
    """
    headers = await _headers(client, workspace, tag="tim1")

    response = await client.get("/api/v1/analytics/timing", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert "has_enough_to_rank" in body
    assert body["min_sends_per_slot"] > 0
    assert isinstance(body["slots"], list)


async def test_a_slot_below_the_floor_reports_no_reply_rate(
    client, db_session, workspace
) -> None:
    """Same discipline as the rollups: unmeasured is not the same as clean."""
    built = await build_sendable(db_session, workspace, suffix="timslot")
    await db_session.execute(
        update(Message)
        .where(Message.id == built.message_id)
        .values(
            sent_at=dt.datetime.now(dt.UTC),
            local_sent_hour=9,
            local_sent_weekday=1,
            sent_timezone="Europe/London",
        )
    )
    await db_session.commit()

    headers = await _headers(client, workspace, tag="tim2")
    response = await client.get("/api/v1/analytics/timing", headers=headers)

    slots = response.json()["slots"]
    assert slots, "the send did not appear; the test proves nothing"
    assert slots[0]["reply_rate"] is None
    assert slots[0]["label"] == "Tue 09:00"


async def test_one_variant_is_not_a_comparison(client, workspace) -> None:
    """Null is the honest answer to "which won" when there is one arm.

    Naming a winner from a single variant, or from arms of nine sends, would be
    noise with a p-value attached.
    """
    headers = await _headers(client, workspace, tag="var1")

    response = await client.get("/api/v1/analytics/variants", headers=headers)

    assert response.status_code == 200
    assert response.json() is None


async def test_the_portfolio_counts_each_message_once(
    client, db_session, workspace
) -> None:
    """The bug this caught before it shipped.

    Joining campaigns to leads and to messages separately crosses the two within
    each campaign, so a plain count multiplies every message by the number of
    leads in its campaign -- 40 real sends were reported as 1,415.
    """
    built = await build_sendable(db_session, workspace, suffix="portfolio")
    await db_session.execute(
        update(Message)
        .where(Message.id == built.message_id)
        .values(sent_at=dt.datetime.now(dt.UTC))
    )
    await db_session.commit()

    headers = await _headers(client, workspace, tag="port1")
    response = await client.get("/api/v1/analytics/portfolio", headers=headers)

    assert response.status_code == 200
    assert response.json()["total_sent"] == 1


async def test_the_portfolio_names_the_markets_it_is_not_in(
    client, db_session, workspace
) -> None:
    """ "The six markets as one object" has to include the empty ones.

    Listed rather than given rows of zeros: a market that has never sent would
    otherwise sort into the table as "0% bounced", which reads as the healthiest
    line on the page.
    """
    built = await build_sendable(db_session, workspace, suffix="markets")
    await db_session.execute(
        update(Message)
        .where(Message.id == built.message_id)
        .values(sent_at=dt.datetime.now(dt.UTC))
    )
    await db_session.commit()

    headers = await _headers(client, workspace, tag="port2")
    body = (await client.get("/api/v1/analytics/portfolio", headers=headers)).json()

    configured = {s["region"] for s in body["slices"]}
    unconfigured = set(body["unconfigured_markets"])

    assert configured, "no market was configured; the test proves nothing"
    assert configured.isdisjoint(unconfigured), "a market cannot be both"
    assert configured | unconfigured >= {
        "usa",
        "canada",
        "uk",
        "europe",
        "australia",
        "middle_east",
    }
    # A market nobody configured has no numbers to be ranked on.
    assert all(m not in configured for m in unconfigured)


# ------------------------------------------------- recipient domain health
#
# Phase 02 promised a bad source visible as a number rather than a hunch. The
# lead-source half is the rollup; this is the domain half, and it was the one
# nothing could reach — domain_health has decided admission since it was
# written and had no reader outside the pipeline and the outbox worker.


async def test_recipient_domains_are_reachable(client, workspace) -> None:
    headers = await _headers(client, workspace, tag="dom1")

    response = await client.get("/api/v1/recipient-domains", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["window_days"] > 0
    assert body["sample_floor"] > 0
    assert isinstance(body["domains"], list)


async def test_a_domain_below_the_floor_reports_no_bounce_rate(
    client, db_session, workspace
) -> None:
    """Two sends and one bounce is not a 50% bounce rate.

    A list sorted on that number would put the least-measured domains at the
    top, which is the opposite of useful.
    """
    built = await build_sendable(db_session, workspace, suffix="domhealth")
    await db_session.execute(
        update(Message)
        .where(Message.id == built.message_id)
        .values(sent_at=dt.datetime.now(dt.UTC), bounced_at=dt.datetime.now(dt.UTC))
    )
    await db_session.commit()

    headers = await _headers(client, workspace, tag="dom2")
    body = (await client.get("/api/v1/recipient-domains", headers=headers)).json()

    assert body["domains"], "the send did not appear; the test proves nothing"
    domain = body["domains"][0]
    assert domain["sent"] == 1
    assert domain["bounced"] == 1
    # has_history is "have we sent here at all"; a rate needs the floor behind
    # it. One send and one bounce is history without a meaningful rate.
    assert domain["has_history"] is True
    assert domain["bounce_rate"] is None
    assert domain["explanation"]


async def test_an_unmeasured_domain_sorts_last_not_first(client, workspace) -> None:
    """An unknown domain is not a problem, it is an absence of evidence.

    Sorting it alongside degraded domains would put every new domain at the top
    of a list whose whole purpose is to surface the bad ones.
    """
    headers = await _headers(client, workspace, tag="dom3")
    body = (await client.get("/api/v1/recipient-domains", headers=headers)).json()

    healths = [d["health"] for d in body["domains"]]
    if "unknown" in healths and len(set(healths)) > 1:
        assert healths.index("unknown") >= max(
            healths.index(h) for h in healths if h != "unknown"
        )


async def test_recipient_domains_require_authentication(client) -> None:
    response = await client.get("/api/v1/recipient-domains")

    assert response.status_code in (401, 403)
