"""Reading a Google Business Profile for what it does not contain.

The point of this detector is that it works for a business with **no website**,
which every other detector in the system cannot say anything about at all.

The rule it must not break is the one the whole codebase turns on: a claim is
what was read, never what was inferred, and never a fact about our own request
dressed up as a fact about them. `None` in a snapshot means *the field was not
returned*. A mask that did not ask for photos must never produce "this business
has no photos" -- so the unmeasured cases below are the load-bearing tests, not
the populated ones.
"""

from __future__ import annotations

import pytest
from titan.intelligence.profile_defects import (
    MIN_PHOTOS,
    MIN_REVIEWS_TO_JUDGE_REPLIES,
    ProfileSnapshot,
    findings_from_profile,
)

LISTING = "https://maps.google.com/?cid=123"


def _snap(**kw) -> ProfileSnapshot:
    return ProfileSnapshot(place_id="p1", listing_url=LISTING, **kw)


def _types(snapshot: ProfileSnapshot, *, pitchable: bool = True) -> set[str]:
    return {f.issue_type for f in findings_from_profile(snapshot, pitchable=pitchable)}


# ------------------------------------- nothing measured means nothing claimed


def test_an_empty_snapshot_produces_no_findings() -> None:
    """A profile nobody could read is not a profile with nothing in it."""
    assert findings_from_profile(_snap()) == []


@pytest.mark.parametrize(
    "field",
    ["has_opening_hours", "photo_count", "has_description"],
)
def test_an_unreturned_field_is_never_an_absence(field: str) -> None:
    """None is "not asked for", not "not there".

    This is the difference between a fact about the business and a fact about
    our field mask -- the exact confusion this codebase elsewhere calls a claim
    about our crawler rather than about their site.
    """
    assert _types(_snap(**{field: None})) == set()


def test_a_missing_website_field_is_not_the_same_as_an_empty_one() -> None:
    """Not asking for websiteUri must not assert they have no website."""
    assert "no_website_listed" not in _types(_snap(website_uri=None))
    assert "no_website_listed" in _types(_snap(website_uri=""))


# ------------------------------------------------------------ what it detects


def test_a_siteless_listing_yields_the_headline_finding() -> None:
    findings = findings_from_profile(_snap(website_uri=""), pitchable=True)
    assert [f.issue_type for f in findings] == ["no_website_listed"]
    assert findings[0].severity.value == "high"
    assert findings[0].page_url == LISTING


def test_a_business_with_a_website_gets_no_website_finding() -> None:
    assert "no_website_listed" not in _types(_snap(website_uri="https://x.co.uk"))


def test_missing_hours_are_detected() -> None:
    assert "no_opening_hours_listed" in _types(_snap(has_opening_hours=False))
    assert "no_opening_hours_listed" not in _types(_snap(has_opening_hours=True))


@pytest.mark.parametrize("count", [0, 1, MIN_PHOTOS - 1])
def test_a_listing_with_almost_no_photos_is_detected(count: int) -> None:
    assert "listing_has_almost_no_photos" in _types(_snap(photo_count=count))


def test_a_listing_with_enough_photos_is_not(
) -> None:
    assert "listing_has_almost_no_photos" not in _types(_snap(photo_count=MIN_PHOTOS))


def test_unanswered_reviews_are_detected() -> None:
    assert "reviews_go_unanswered" in _types(
        _snap(review_count=40, replied_review_count=0)
    )


def test_a_business_that_replies_is_not_accused_of_not_replying() -> None:
    assert "reviews_go_unanswered" not in _types(
        _snap(review_count=40, replied_review_count=20)
    )


def test_too_few_reviews_to_judge_a_reply_habit() -> None:
    """Two unanswered reviews is not a policy."""
    assert "reviews_go_unanswered" not in _types(
        _snap(review_count=MIN_REVIEWS_TO_JUDGE_REPLIES - 1, replied_review_count=0)
    )


def test_reply_rate_needs_both_numbers() -> None:
    """Knowing the review count without the reply count proves nothing."""
    assert "reviews_go_unanswered" not in _types(
        _snap(review_count=40, replied_review_count=None)
    )


def test_a_neglected_listing_yields_every_finding_at_once() -> None:
    """The population this is for: findable on Google, invisible everywhere else."""
    assert _types(
        _snap(
            website_uri="",
            has_opening_hours=False,
            photo_count=1,
            review_count=40,
            replied_review_count=0,
            has_description=False,
        )
    ) == {
        "no_website_listed",
        "no_opening_hours_listed",
        "listing_has_almost_no_photos",
        "reviews_go_unanswered",
        "listing_has_no_description",
    }


# ----------------------------------------------------------------- staging


def test_findings_are_unpitchable_until_switched_on() -> None:
    """Counted before they are said, exactly as absence.py stages its claims."""
    unpitchable = findings_from_profile(_snap(website_uri=""), pitchable=False)
    pitchable = findings_from_profile(_snap(website_uri=""), pitchable=True)

    assert not unpitchable[0].is_pitchable()
    assert pitchable[0].is_pitchable()


def test_every_finding_cites_the_listing_it_was_read_from() -> None:
    """No claim without a citation the recipient can open."""
    findings = findings_from_profile(
        _snap(website_uri="", has_opening_hours=False, photo_count=0), pitchable=True
    )
    assert findings
    for f in findings:
        assert f.evidence
        assert all(url == LISTING for _observed, url in f.evidence)
