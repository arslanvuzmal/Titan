"""`merge` must give the same answer for a generator and a list.

The defect this pins produced **zero** absence findings across the estate's
entire history, while 22,000 crawled pages sat in the database with the signals
already in them.

`merge` walks its argument once per capability. `activities/pipeline.py` passes
a generator expression:

    merge_modernisation(
        modernisation_profile(_page_signals(url, obs)) for url, obs in page_rows
    )

so the first capability in ``VENDORS`` consumed it and every later one read an
empty list as NOT_MEASURED. It failed in the quietest possible way: the first
capability -- CONVERSATIONAL -- was measured correctly, so a profile always
came back looking populated. `gap` needs ``MIN_MEASURED_CAPABILITIES`` measured,
got one, and returned None; `findings_from_gap` produces nothing without a gap.

Measured against one real crawl before the fix: generator gave 1 of 6
capabilities and ``gap=None``; the same pages as a list gave 6 of 6 and
``gap=0.92``.

The test is written as an equivalence rather than as "merge accepts a
generator", because the equivalence is the property that was violated and it
stays true whatever the argument type becomes.
"""

from __future__ import annotations

import pytest
from titan.intelligence.modernisation import (
    MIN_MEASURED_CAPABILITIES,
    VENDORS,
    Capability,
    ModernisationProfile,
    Signal,
    merge,
)


def _page(**signals: Signal) -> ModernisationProfile:
    """One page's profile, defaulting every unnamed capability to ABSENT."""
    filled = {c: signals.get(c.name.lower(), Signal.ABSENT) for c in VENDORS}
    return ModernisationProfile(signals=filled)


def _realistic() -> list[ModernisationProfile]:
    """What a real crawl looks like: several pages, analytics present."""
    return [_page(analytics=Signal.PRESENT) for _ in range(18)]


def test_a_generator_and_a_list_agree() -> None:
    """The property. Same pages, same answer, whatever the argument type."""
    pages = _realistic()

    from_list = merge(pages)
    from_generator = merge(p for p in pages)

    assert from_generator.signals == from_list.signals


def test_a_generator_measures_every_capability_not_just_the_first() -> None:
    """The failure mode, named.

    Under the defect this returned NOT_MEASURED for all but the first
    capability in VENDORS -- which is why the profile looked plausible instead
    of obviously broken.
    """
    merged = merge(p for p in _realistic())

    unmeasured = [c.name for c, s in merged.signals.items() if s is Signal.NOT_MEASURED]
    assert unmeasured == [], f"not measured from a generator: {unmeasured}"


def test_a_generator_still_produces_a_gap() -> None:
    """The consequence that mattered: no gap means no absence finding, ever."""
    merged = merge(p for p in _realistic())

    assert len(merged.measured) >= MIN_MEASURED_CAPABILITIES
    assert merged.gap is not None
    assert merged.gap > 0


def test_merge_can_be_called_twice_on_the_same_argument() -> None:
    """A second caller must not receive a consumed iterable."""
    pages = _realistic()
    once = merge(pages)
    twice = merge(pages)
    assert once.signals == twice.signals


@pytest.mark.parametrize("empty", [[], iter([])])
def test_nothing_in_means_nothing_measured(empty) -> None:
    """The honest answer for a site that was obstructed throughout."""
    merged = merge(empty)
    assert all(s is Signal.NOT_MEASURED for s in merged.signals.values())
    assert merged.gap is None


def test_present_on_any_page_wins_over_absent_on_the_rest() -> None:
    """Unchanged behaviour, checked through a generator this time.

    Booking lives on the booking page and chat is often only on the home page,
    so requiring every page to show a capability would find nothing.
    """
    pages = [
        _page(),
        _page(conversational=Signal.PRESENT),
        _page(),
    ]
    merged = merge(p for p in pages)
    assert merged.signals[Capability.CONVERSATIONAL] is Signal.PRESENT
