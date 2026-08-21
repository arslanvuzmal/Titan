"""Where an English cold email is a reasonable thing to send.

Titan writes English and writes to local small businesses. A message the owner
cannot comfortably read is not a weaker message -- it is a spam complaint, and
the complaint is charged to a mailbox that took three weeks to warm.

The threshold is a judgement and these fix it in place, so moving it is a
deliberate act with a failing test attached rather than a quiet edit.
"""

from __future__ import annotations

import pytest
from titan.intelligence import territories as T
from titan.intelligence.languages import (
    EF_EPI_2025,
    VERY_HIGH,
    english_outreach_ok,
    english_score,
    why_excluded,
)


# ------------------------------------------------------- the counter-intuitive


@pytest.mark.parametrize("code", ["RO", "PL", "HR", "SK", "PT"])
def test_the_markets_intuition_would_have_cut(code) -> None:
    """Romania, Poland, Croatia, Slovakia and Portugal all band Very High.

    Worth a test of its own because the intuitive ordering is close to
    backwards, and an unchecked guess would have excluded every one of them.
    """
    assert english_outreach_ok(code), f"{code} scores {english_score(code)}"


@pytest.mark.parametrize("code", ["ES", "FR", "IT"])
def test_the_markets_intuition_would_have_kept(code) -> None:
    """Spain, France and Italy band Moderate -- below Romania and Poland."""
    assert not english_outreach_ok(code)


def test_native_english_needs_no_score() -> None:
    for code in ("GB", "US", "CA", "IE", "AU"):
        assert english_outreach_ok(code)
        assert english_score(code) is None, "should not be scored at all"


def test_the_gulf_passes_on_its_own_reasoning() -> None:
    """English is the working language of the private clinics Titan sells to
    there. Held apart from the population index rather than folded into it."""
    assert english_outreach_ok("AE")
    assert english_score("AE") is None


# ----------------------------------------------------- absent is not permission


def test_an_unscored_country_is_refused_not_admitted() -> None:
    """Planted violation: default to True and one unchecked market quietly
    receives mail nobody established it could read.

    Slovenia is in the catalogue and probably comfortable. Nothing here shows
    that, and "we did not check" must not read as "we checked".
    """
    assert not english_outreach_ok("SI")
    assert "no English proficiency score" in why_excluded("SI")


def test_no_country_at_all_is_refused() -> None:
    assert not english_outreach_ok(None)
    assert not english_outreach_ok("")


# ------------------------------------------------------------- the threshold


def test_the_threshold_is_the_top_band_and_is_inclusive() -> None:
    """Poland sits exactly on 600 and is in; Latvia on 599 is out by a point.

    A threshold has an edge and this is where it falls. Fixed by a test so that
    moving it is deliberate.
    """
    assert EF_EPI_2025["PL"] == VERY_HIGH
    assert english_outreach_ok("PL")
    assert EF_EPI_2025["LV"] == VERY_HIGH - 1
    assert not english_outreach_ok("LV")


def test_an_exclusion_says_what_would_change_it() -> None:
    reason = why_excluded("IT")

    assert "513" in reason and "600" in reason


def test_a_country_that_passes_has_no_exclusion_reason() -> None:
    assert why_excluded("NL") == ""


# ------------------------------------------------------ the catalogue applied


def test_the_gate_cuts_europe_and_leaves_everywhere_else() -> None:
    """Measured against the real catalogue: 92 of 107 territories, and every
    one of the fifteen losses is European."""
    english = T.all_territories(english_only=True)
    everywhere = T.all_territories(english_only=False)

    assert len(everywhere) == 107
    assert len(english) == 92
    lost = set(everywhere) - set(english)
    assert {t.region for t in lost} == {T.Region.EUROPE}


def test_a_global_campaign_starts_where_the_message_is_read() -> None:
    """English-first markets lead the rotation. The early searches happen before
    anybody has looked at the results, so they should land where the copy is
    most likely to work."""
    first = T.all_territories()[0]

    assert first.query_name == "London UK"


def test_every_market_is_in_the_global_order_exactly_once() -> None:
    """Planted violation: drop one and its territories become unreachable to
    every business-type campaign, silently."""
    assert len(T.GLOBAL_ORDER) == len(set(T.GLOBAL_ORDER))
    covered = {t.region for t in T.all_territories(english_only=False)}
    assert covered == set(T.GLOBAL_ORDER)


# --------------------------------------------------------------- the rotation


def test_a_global_campaign_crosses_borders_where_a_market_one_does_not() -> None:
    """The whole point. ``next_territory`` refuses to leave its market -- right
    for a campaign that declared one. A campaign that declared none is routed
    to its recipient's own carrier, so crossing is what it is for."""
    every_uk = {t.query_name for t in T.for_region(T.Region.UK)}

    nxt = T.next_territory_anywhere(exhausted=every_uk)

    assert nxt is not None
    assert nxt.region is T.Region.USA
    assert T.next_territory(T.Region.UK, exhausted=every_uk) is None


def test_spending_everything_is_a_real_answer() -> None:
    """None means widen the business type or add a language -- a decision for a
    person, not something to solve by searching again."""
    everything = {t.query_name for t in T.all_territories(english_only=False)}

    assert T.next_territory_anywhere(exhausted=everything) is None


def test_a_spent_territory_is_skipped_not_re_asked() -> None:
    spent = {"London UK"}

    nxt = T.next_territory_anywhere(exhausted=spent)

    assert nxt is not None
    assert nxt.query_name == "Manchester UK"


def test_the_gate_can_be_lifted_when_a_second_language_exists() -> None:
    """``english_only`` is the state of the world, not a preference. The day
    the composer writes German, this stops excluding Zurich."""
    everywhere = T.all_territories(english_only=False)

    assert any(t.country_code == "IT" for t in everywhere)
