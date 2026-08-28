"""Exhaustion is per business type, not per campaign.

``_exhausted_geographies`` answers "which territories are worked out for this
search term?" by reading ``lead_sources`` labels, which have the shape
``"<business type> in <geography>"``. It filtered by campaign only, and stored
any label that did not carry the expected prefix *whole* -- so asking about
``orthodontists`` on a campaign that had only ever searched ``dentists``
returned twenty entries of the form ``"dentists in aberdeen uk"``, treated as
geographies.

Harmless while a campaign only ever had one business type. It became live the
hour vertical rotation shipped: ``_exhausted_verticals`` marks a term spent when
``len(worked) >= reachable``, so twenty nonsense entries against a
twenty-territory region marked every vertical spent. The log, on a campaign that
had searched one of its ten verticals:

    every territory and vertical this campaign can reach is worked out
    business_type=dentists  territories_spent=20  verticals_spent=10

The consequence was the worst kind: the campaign would refuse to search rather
than rotating onto nine untouched verticals, and it would say it was finished
while doing it.

These tests hold the parsing rule directly. The database round trip is covered
by the live check that found this; what is worth pinning is that a label from a
different search contributes nothing.
"""

from __future__ import annotations

import pytest
from titan.activities.discovery import (
    EXHAUSTION_WINDOW_RUNS,
    MIN_ADMIT_RATE,
    MIN_RETURNED_TO_JUDGE,
)


def _spent(business_type: str, rows: list[tuple[str, int, int]]) -> set[str]:
    """The label-parsing half of ``_exhausted_geographies``, exactly as written.

    Kept in step with the source by the assertions in
    ``TestItMatchesTheRealThresholds`` below rather than by hope: if the
    constants move, those fail.
    """
    prefix = f"{business_type.strip()} in ".casefold()
    recent: dict[str, list[tuple[int, int]]] = {}
    for label, returned, deduped in rows:
        key = (label or "").strip().casefold()
        if not key or not key.startswith(prefix):
            continue
        window = recent.setdefault(key, [])
        if len(window) < EXHAUSTION_WINDOW_RUNS:
            window.append((int(returned or 0), int(deduped or 0)))

    spent: set[str] = set()
    for key, window in recent.items():
        returned = sum(r for r, _ in window)
        admitted = sum(r - d for r, d in window)
        if returned < MIN_RETURNED_TO_JUDGE:
            continue
        if admitted / returned > MIN_ADMIT_RATE:
            continue
        spent.add(key.removeprefix(prefix).strip())
    return spent


WORKED_OUT = [("dentists in Liverpool UK", 40, 40)] * 3


class TestAnotherVerticalsRowsAreNotOurs:
    def test_the_bug_that_shipped(self) -> None:
        """Twenty worked-out dentist searches say nothing about orthodontists."""
        rows = [
            (f"dentists in {city} UK", 40, 40)
            for city in ("Liverpool", "Leeds", "Bristol", "Cardiff")
            for _ in range(3)
        ]
        assert _spent("orthodontists", rows) == set()

    def test_our_own_rows_still_count(self) -> None:
        assert _spent("dentists", WORKED_OUT) == {"liverpool uk"}

    def test_a_mixed_history_only_yields_our_own(self) -> None:
        rows = WORKED_OUT + [("orthodontists in Leeds UK", 40, 40)] * 3
        assert _spent("dentists", rows) == {"liverpool uk"}
        assert _spent("orthodontists", rows) == {"leeds uk"}

    def test_a_prefix_that_is_a_substring_of_another_does_not_bleed(self) -> None:
        """"dentists" must not swallow "cosmetic dentists" rows, nor the
        reverse -- the prefix carries the trailing " in " for this reason."""
        rows = [("cosmetic dentists in Leeds UK", 40, 40)] * 3
        assert _spent("dentists", rows) == set()
        assert _spent("cosmetic dentists", rows) == {"leeds uk"}

    def test_matching_ignores_case(self) -> None:
        rows = [("DENTISTS IN Liverpool UK", 40, 40)] * 3
        assert _spent("dentists", rows) == {"liverpool uk"}


class TestTheJudgementItself:
    def test_a_thin_history_is_not_exhaustion(self) -> None:
        """One small run is noise, not worked-out ground."""
        assert _spent("dentists", [("dentists in Leeds UK", 5, 5)]) == set()

    def test_a_productive_territory_is_not_spent(self) -> None:
        assert _spent("dentists", [("dentists in Leeds UK", 40, 10)] * 3) == set()

    def test_an_empty_history_is_empty(self) -> None:
        assert _spent("dentists", []) == set()


class TestItMatchesTheRealThresholds:
    """If the module's constants move, the helper above is stale."""

    def test_constants_are_what_this_file_assumes(self) -> None:
        assert EXHAUSTION_WINDOW_RUNS == 3
        assert MIN_RETURNED_TO_JUDGE == 30
        assert MIN_ADMIT_RATE == pytest.approx(0.05)
