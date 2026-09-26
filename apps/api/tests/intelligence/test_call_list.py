"""Which practice to ring first, when a call costs a minute that cannot be
spent twice.

Email is free, so its list can be long and its ordering sloppy. Twenty calls a
day against 2,109 practices means the call list is never exhausted -- nothing
is lost by being picky, and everything is lost by ringing in whatever order the
database returned.
"""

from __future__ import annotations

import pytest
from titan.intelligence.call_list import (
    MAX_EVIDENCE_AGE_DAYS,
    CallTarget,
    freshness_penalty,
    rating_points,
    size_points,
    tier_of,
)


def _target(**kw) -> CallTarget:
    base = dict(
        lead_id="l1",
        practice="Test Dental",
        phone="+441234567890",
        website="https://test.co.uk",
        issue_type="broken_internal_link",
        page_url="https://test.co.uk/book",
        observed="HTTP 404",
        review_count=200,
        rating=4.7,
        evidence_age_days=1,
    )
    base.update(kw)
    return CallTarget(**base)  # type: ignore[arg-type]


# ----------------------------------------------------------- what to lead with


def test_a_dead_booking_page_is_the_strongest_call_there_is() -> None:
    assert tier_of("broken_internal_link", "https://x.co.uk/book") == "booking_dead"
    assert tier_of("broken_internal_link", "https://x.co.uk/booking") == "booking_dead"


def test_the_same_detector_elsewhere_is_not() -> None:
    """A dead link in the news archive costs nobody an appointment."""
    assert tier_of("broken_internal_link", "https://x.co.uk/news/2019") == "cosmetic"


def test_cosmetic_findings_are_never_worth_a_phone_call() -> None:
    """They are worth an email only because an email costs nothing."""
    for issue in ("images_missing_alt_text", "missing_security_headers",
                  "no_structured_data", "javascript_console_errors"):
        assert tier_of(issue, None) == "cosmetic"
        assert not _target(issue_type=issue, page_url=None).worth_calling


def test_the_defect_outranks_everything_size_can_buy() -> None:
    """A huge practice with a cosmetic problem still is not a call.

    Size and rating together are capped below the gap between tiers, on
    purpose: no amount of revenue makes alt text worth a minute of the day.
    """
    huge_but_cosmetic = _target(
        issue_type="images_missing_alt_text", page_url=None,
        review_count=5000, rating=5.0,
    )
    small_but_broken = _target(review_count=12, rating=4.0)
    assert small_but_broken.score > huge_but_cosmetic.score


# --------------------------------------------------------------- the modifiers


def test_practice_size_is_compressed_not_linear() -> None:
    """40 vs 400 reviews is a real difference; 2,000 vs 2,400 is noise."""
    assert size_points(400) - size_points(40) > 5
    assert size_points(2400) - size_points(2000) < 1
    assert size_points(50_000) <= 20.0


def test_a_struggling_practice_is_not_punished_only_unrewarded() -> None:
    """Below 4.0 scores zero, never negative.

    A practice at 3.4 is not a worse human being -- its website simply is not
    its most pressing problem, and the call will not land.
    """
    assert rating_points(3.2) == 0.0
    assert rating_points(4.0) == 0.0
    assert rating_points(4.8) > 0
    assert rating_points(None) == 0.0


def test_stale_evidence_is_penalised_harder_than_email_would() -> None:
    """A spoken claim cannot be withdrawn once they have checked.

    The email gate allows 30 days. A call opens by asserting a fact while the
    person is at their desk, so being wrong in the first sentence is
    unrecoverable in a way an email is not.
    """
    assert MAX_EVIDENCE_AGE_DAYS < 30
    assert freshness_penalty(1) == 0.0
    assert freshness_penalty(30) > freshness_penalty(10)
    assert freshness_penalty(None) > 0


def test_evidence_past_the_window_is_not_callable_at_all() -> None:
    assert not _target(evidence_age_days=MAX_EVIDENCE_AGE_DAYS + 1).worth_calling
    assert _target(evidence_age_days=MAX_EVIDENCE_AGE_DAYS).worth_calling


# ------------------------------------------------------------- what is said


def test_the_opener_names_the_page_so_they_can_look_at_it() -> None:
    """A receptionist who can check it becomes an ally, not a filter."""
    opener = _target().opener()
    assert "test.co.uk/book" in opener
    assert "error" in opener.lower()


def test_the_form_opener_carries_the_actual_count() -> None:
    t = _target(
        issue_type="high_friction_contact_form",
        page_url="https://test.co.uk/contact",
        observed="11 visible fields",
    )
    assert "11" in t.opener()


def test_the_ask_is_small_enough_for_a_receptionist_to_grant() -> None:
    """Stage one closes on a name and an email, never on a meeting.

    A receptionist can hand those over without deciding anything, which is why
    she will. Asking for the dentist's diary asks her to make a decision that
    is not hers, and the answer is "send us an email".
    """
    ask = _target().ask()
    assert "who" in ask.lower()
    for forbidden in ("meeting", "diary", "calendar", "20 minutes", "twenty minutes"):
        assert forbidden not in ask.lower()


def test_ranking_puts_the_best_call_first() -> None:
    """The whole product: the first call of the morning is the best one."""
    targets = [
        _target(issue_type="images_missing_alt_text", page_url=None, review_count=900),
        _target(issue_type="no_booking_or_enquiry_path", page_url=None),
        _target(),  # dead booking page
        _target(issue_type="high_friction_contact_form", page_url="https://t.co/c"),
    ]
    ordered = sorted(targets, key=lambda t: -t.score)
    assert ordered[0].tier == "booking_dead"
    assert ordered[-1].tier == "cosmetic"
