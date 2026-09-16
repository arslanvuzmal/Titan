"""Opening the next market without being asked.

The decision "where to look next" was the last part of lead supply still made
by a person editing a file. When the 29 configured combinations were worked
out, discovery stopped dead for eight days and nothing said so.

Most of these guard one direction. Failing to open a market costs a quiet week;
opening the wrong thing spends the discovery budget re-reading businesses the
estate already has, or -- much worse -- creates a campaign that can send.
"""

from __future__ import annotations

from titan.config import OperatingMode
from titan.db.enums import CampaignStatus
from titan.intelligence import territories
from titan.intelligence.expansion import (
    MAX_NEW_PER_PASS,
    PRODUCTIVE_NEW_SHARE,
    Exhausted,
    _next_territory,
    _slug,
)

FIRST = territories.TERRITORIES[0]


def worked_out(returned: int, new: int) -> Exhausted:
    import uuid

    return Exhausted(
        campaign_id=uuid.uuid4(),
        business_type="dentists",
        geography="Manchester UK",
        searches=10,
        returned=returned,
        new_records=new,
    )


# ------------------------------------------------ the signal
def test_a_city_returning_nothing_new_is_worked_out() -> None:
    """Measured live: 320 records returned, zero of them new."""
    assert worked_out(320, 0).new_share == 0.0
    assert worked_out(320, 0).new_share < PRODUCTIVE_NEW_SHARE


def test_a_city_still_producing_is_not() -> None:
    """Planted violation: treat "we have searched a lot" as exhaustion.

    A dense metro returns the same hundreds of businesses on every pass and a
    handful of genuinely new ones. Volume of searching says nothing; the share
    that is new says everything.
    """
    assert worked_out(400, 120).new_share > PRODUCTIVE_NEW_SHARE


def test_a_search_that_returned_nothing_at_all_is_not_evidence() -> None:
    """Zero returned is an API problem or a bad query, not a worked-out city.

    Dividing by it would be a crash; calling it exhausted would close a market
    because the network had a bad afternoon.
    """
    assert worked_out(0, 0).new_share == 0.0


def test_the_description_carries_the_numbers() -> None:
    """It lands in a log line explaining why a market was opened. "exhausted"
    is not a reason somebody can check."""
    text = worked_out(320, 0).describe()

    assert "320" in text
    assert "dentists" in text and "Manchester UK" in text


# ------------------------------------------------ where to go next
def test_the_next_territory_is_the_densest_unworked_one() -> None:
    """``TERRITORIES`` is ordered densest-first, and that order is the ranking."""
    assert _next_territory("dentists", taken=set()) is FIRST


def test_a_territory_already_worked_is_skipped() -> None:
    """Planted violation: re-open a city the estate has already searched.

    That spends the discovery budget re-reading known businesses, and the
    duplicate check downstream would silently throw all of it away.
    """
    taken = {("dentists", FIRST.query_name)}

    assert _next_territory("dentists", taken) is not FIRST


def test_another_trade_may_still_have_the_same_city() -> None:
    """The pairing is (business type, city). Dentists being done in London
    says nothing about law firms there."""
    taken = {("dentists", FIRST.query_name)}

    assert _next_territory("law firms", taken) is FIRST


def test_running_out_of_catalogue_returns_none_rather_than_repeating() -> None:
    """107 territories is a lot, not infinity. The honest answer at the end is
    "nowhere left", not the first one again."""
    every = {("dentists", t.query_name) for t in territories.TERRITORIES}

    assert _next_territory("dentists", every) is None


# ------------------------------------------------ the slug
def test_the_slug_is_stable_and_readable() -> None:
    slug = _slug("dental implant clinics", FIRST)

    assert slug == _slug("dental implant clinics", FIRST), "must be deterministic"
    assert " " not in slug
    assert len(slug) <= 100
    assert FIRST.country_code.lower() in slug


# ------------------------------------------------ the safety property
def test_a_new_market_is_never_authorised_to_send() -> None:
    """Planted violation: copy the source campaign's sending settings.

    This is what makes unattended expansion safe at all. A campaign opened by a
    machine is RESEARCH_ONLY and not authorised, exactly as provision_markets
    creates them -- so the worst a bug here can do is crawl a city nobody asked
    for. It can never put mail in front of a business nobody chose.

    Asserted against the module's own constants rather than a database, so it
    fails the moment someone edits the defaults rather than the moment a
    campaign sends.
    """
    import inspect

    from titan.intelligence import expansion

    source = inspect.getsource(expansion.expand)

    assert "OperatingMode.RESEARCH_ONLY" in source
    assert "sending_authorized=False" in source
    assert OperatingMode.RESEARCH_ONLY.value == "research_only"


def test_one_pass_opens_a_bounded_number() -> None:
    """Each new campaign starts crawling immediately, and the browser worker is
    bounded at eight concurrent activities. Opening ten at once would starve
    the campaigns already running."""
    assert 1 <= MAX_NEW_PER_PASS <= 3


def test_new_campaigns_are_active_so_discovery_actually_runs() -> None:
    """RESEARCH_ONLY is about sending. A campaign that is not ACTIVE is not
    planned at all, and the whole point is that it starts finding leads."""
    import inspect

    from titan.intelligence import expansion

    assert "CampaignStatus.ACTIVE" in inspect.getsource(expansion.expand)
    assert CampaignStatus.ACTIVE.value == "active"
