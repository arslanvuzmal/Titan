"""One over-long phone number must not lose a whole search.

Places returns ``nationalPhoneNumber`` formatted for display, and it was
written verbatim into ``organizations.phone_e164`` -- a column holding twenty
characters. Most locales fit; the ones that do not raised
``StringDataRightTruncation`` inside ``_create_lead``.

The blast radius is what makes this worth a test rather than a one-line patch.
The insert runs inside the discovery unit of work, so the failure took down
``discover_leads`` through all three of its retries and **every business in that
search was lost**, not merely the one with the long number. It was doing so on
the live workspace while the campaign it belonged to reported healthy.

The rule the fix encodes: normalise, and drop what still does not fit. A
truncated phone number is not a shorter phone number, it is a different one --
and a wrong number in a CRM is worse than an empty field, because eventually
somebody rings it.
"""

from __future__ import annotations

import pytest
from titan.activities.discovery import PHONE_COLUMN_LIMIT, _to_e164


class TestNormalisation:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("(786) 812-8622", "7868128622"),
            ("+974 4444 5555", "+97444445555"),
            ("+49 30 12345678", "+493012345678"),
            ("020 7946 0958", "02079460958"),
            ("+44 (0)161 496 0000", "+4401614960000"),
            ("+1 305-988-9388", "+13059889388"),
        ],
    )
    def test_formatting_is_stripped(self, raw: str, expected: str) -> None:
        assert _to_e164(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "+", "n/a", "call us"])
    def test_nothing_usable_is_none(self, raw: str | None) -> None:
        assert _to_e164(raw) is None


class TestItAlwaysFitsTheColumn:
    """The property that actually stops the batch dying."""

    @pytest.mark.parametrize(
        "raw",
        [
            "(786) 812-8622",
            "+974 4444 5555 ext. 1234",
            "+49 (0) 30 1234 5678 / +49 (0) 30 8765 4321",
            "+1 (305) 988-9388 or +1 (786) 305-8898",
            "0" * 40,
        ],
    )
    def test_the_result_never_exceeds_the_column(self, raw: str) -> None:
        value = _to_e164(raw)
        assert value is None or len(value) <= PHONE_COLUMN_LIMIT

    def test_an_over_long_number_is_dropped_not_trimmed(self) -> None:
        """A trimmed number would dial somebody else."""
        raw = "+49 (0) 30 1234 5678 / +49 (0) 30 8765 4321"
        assert _to_e164(raw) is None

    def test_a_long_but_recoverable_number_survives(self) -> None:
        """Stripping is tried before giving up."""
        assert _to_e164("+ 9 7 4   4 4 4 4   5 5 5 5") == "+97444445555"
