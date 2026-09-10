"""Which finding gets to be the one thing a message says.

A message says one thing. The finding that leads decides what the message *is*,
so this ordering is a copywriting decision expressed as a sort key, and it is
worth testing as one.

Pure: ``lead_rank`` reads an issue type and a URL and returns an integer.
"""

from __future__ import annotations

import pytest
from titan.activities.pipeline import lead_rank

#: Everything the estate can currently detect, with the page a finding of that
#: kind is typically found on.
CONVERSION = [
    ("broken_primary_cta", None),
    ("high_friction_contact_form", None),
    ("no_visible_phone_number", None),
    ("broken_internal_link", "https://example.com/book"),
    ("broken_internal_link", "https://example.com/contact"),
]

AUTOMATION = [("no_booking_or_enquiry_path", None)]

QUALITY = [
    ("images_missing_alt_text", None),
    ("javascript_console_errors", None),
    ("slow_largest_contentful_paint", None),
    ("serious_accessibility_violations", None),
    ("missing_security_headers", None),
    ("no_structured_data", None),
    ("missing_meta_description", None),
    ("failed_network_requests", None),
    ("broken_internal_link", "https://example.com/news/2019-press-release"),
]


@pytest.mark.parametrize(("issue_type", "page_url"), CONVERSION)
def test_a_person_who_tried_to_buy_comes_first(issue_type, page_url) -> None:
    """These are the only findings that describe a failed transaction."""
    assert lead_rank(issue_type, page_url) == 0


@pytest.mark.parametrize(("issue_type", "page_url"), AUTOMATION)
def test_no_way_to_book_outranks_every_cosmetic_finding(issue_type, page_url) -> None:
    """Planted violation: ``0 if CONVERSION else 1``, which is what shipped.

    That key put "there is no way to book or enquire at all" in the same bucket
    as a missing alt attribute, and then broke the tie on detector confidence.
    An absence is inferred and an alt-text check is certain, so the absence lost
    every time: **912 such findings in the estate led 9 messages out of 413.**

    It is the most legible thing this system can tell a business, and the only
    pitch an owner can act on without hiring a developer.
    """
    rank = lead_rank(issue_type, page_url)
    assert rank == 1
    for quality_type, quality_url in QUALITY:
        assert rank < lead_rank(quality_type, quality_url), (
            f"{issue_type} must outrank {quality_type}"
        )


@pytest.mark.parametrize(("issue_type", "page_url"), QUALITY)
def test_findings_the_owner_cannot_feel_come_last(issue_type, page_url) -> None:
    """True, cheap to detect, and invisible to the person paying the bills."""
    assert lead_rank(issue_type, page_url) == 2


def test_a_live_defect_still_beats_a_missing_capability() -> None:
    """The middle tier is a promotion over quality, not over conversion.

    A business taking bookings by telephone is not broken -- ``_OPERATIONAL``
    in ``vernacular`` says so, and says why the pitch is different in kind. A
    booking button that returns 404 *is* broken, and is the more urgent of the
    two.
    """
    assert lead_rank("broken_primary_cta", None) < lead_rank(
        "no_booking_or_enquiry_path", None
    )


def test_the_same_finding_is_ranked_by_where_it_was_found() -> None:
    """``broken_internal_link`` is the second-largest type and covers both
    "your /book page is down" and "a footer link points at a dead press
    release". Sending the first as an emergency is right; sending the second as
    one is what makes a reader stop reading."""
    money_path = lead_rank("broken_internal_link", "https://example.com/book")
    footer = lead_rank("broken_internal_link", "https://example.com/news/old")

    assert money_path == 0
    assert footer == 2
    assert money_path < footer


def test_an_unknown_issue_type_is_not_promoted() -> None:
    """A detector added tomorrow does not get to lead a message by default."""
    assert lead_rank("some_detector_written_next_week", None) == 2
