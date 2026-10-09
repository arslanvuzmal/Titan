"""Two written forms of one telephone number.

The case that matters: a suppression arrives from a dialler in E.164 and the
organisation is stored the way Google Places handed it over. If those two do
not compare equal, somebody who asked not to be rung again gets rung again.
"""

from __future__ import annotations

import pytest
from coldops.delivery.phone import dial_key, same_line, strip_formatting


@pytest.mark.parametrize(
    "written",
    [
        "+441611234567",
        "+44 161 123 4567",
        "0161 123 4567",
        "(0161) 123-4567",
        "01611234567",
        "0161.123.4567",
    ],
)
def test_every_written_form_of_one_manchester_number_agrees(written):
    assert dial_key(written) == "611234567"


def test_a_suppression_in_e164_matches_an_organisation_in_national_form():
    """The bug this module exists for. No call has been made yet, so it has
    never fired; it would have fired on the first refusal."""
    assert same_line("+441611234567", "01611234567")


def test_north_american_numbers_match_across_the_country_code():
    assert same_line("+17868128622", "7868128622")
    assert same_line("(786) 812-8622", "+1 786-812-8622")


def test_two_different_numbers_do_not_match():
    assert not same_line("01611234567", "01611234568")
    assert not same_line("+441611234567", "+442071234567")


@pytest.mark.parametrize("junk", [None, "", "   ", "12345", "+", "n/a", "ext 4"])
def test_anything_too_short_to_be_a_number_has_no_key(junk):
    assert dial_key(junk) is None


def test_two_missing_numbers_are_not_the_same_number():
    """Otherwise one organisation with no phone suppresses every other."""
    assert not same_line(None, None)
    assert not same_line("", "")
    assert not same_line(None, "01611234567")


# Stripping itself is covered by tests/activities/test_phone_normalisation.py,
# which predates this module and moved with the implementation. What belongs
# here is the join between the two ideas:
def test_stripping_does_not_change_which_line_a_number_is():
    """The point of doing it on the way in: the key is unaffected."""
    raw = "0161 912 6200"
    assert dial_key(raw) == dial_key(strip_formatting(raw))
