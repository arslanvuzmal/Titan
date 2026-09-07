"""Turning "they do not run this" into something Titan may actually say.

Every one of the fourteen existing finding types is a website defect, so a
business with a clean site produces nothing to write about and cannot be
contacted at all -- however far behind it is. The modernisation reader has
measured that gap since it was written and its entire output was one float in
ScoringInput.

The whole risk of this feature is over-claiming. A defect is positive
evidence: we saw a 404. An absence is not, and being wrong about it is worse
-- telling a clinic that has just bought an AI phone system that it has no AI
loses that recipient deservedly. These tests are mostly about refusing to
claim.
"""

from __future__ import annotations

from titan.intelligence.absence import findings_from_gap
from titan.intelligence.modernisation import (
    Capability,
    ModernisationProfile,
    Signal,
)

PAGES = ("https://clinic.test/", "https://clinic.test/contact")


def profile(**overrides) -> ModernisationProfile:
    """A clinic with everything measured and nothing running."""
    signals = dict.fromkeys(Capability, Signal.ABSENT)
    signals.update(overrides)
    return ModernisationProfile(signals=signals)


# ==========================================================================
# What it will say
# ==========================================================================
def test_a_measured_absence_becomes_a_pitchable_finding() -> None:
    """The point of the whole exercise: a clean site that runs nothing is now
    a business Titan has something true to say to."""
    found = findings_from_gap(profile(), pages_read=PAGES, pitchable=True)

    assert {f.issue_type for f in found} >= {
        "no_conversational_capability",
        "no_self_service_booking",
    }
    assert all(f.is_pitchable() for f in found)


def test_the_claim_is_about_what_was_read_not_about_what_they_have() -> None:
    """Planted violation: assert the absence directly.

    Eighteen pages cannot support "you have no online booking". They can
    support "I could not find one", which stays true when we missed something
    and invites a correction instead of an argument -- and the difference is
    the difference between a reply and a lost recipient.
    """
    found = findings_from_gap(profile(), pages_read=PAGES, pitchable=True)
    booking = next(f for f in found if f.issue_type == "no_self_service_booking")
    said = f"{booking.title} {booking.observed_value}".lower()

    assert "could not find" in said
    assert "you have no" not in said
    assert "you do not" not in said


def test_the_evidence_is_the_pages_that_were_actually_read() -> None:
    """An absence claim traces to the pages that were read and found not to
    contain it. Without them the validator has nothing to check the sentence
    against, which is the whole contract."""
    found = findings_from_gap(profile(), pages_read=PAGES, pitchable=True)

    for finding in found:
        assert {url for _excerpt, url in finding.evidence} == set(PAGES)


# ==========================================================================
# What it refuses to say -- the reason this can ship at all
# ==========================================================================
def test_an_unmeasured_capability_is_never_claimed_as_absent() -> None:
    """Planted violation: treat NOT_MEASURED as ABSENT.

    A cookie wall on the booking page makes that one capability unmeasurable
    while the rest of the site still reads fine, and treating the blank as an
    absence would pitch hardest at exactly the businesses we learned least
    about.

    Deliberately measured *above* the four-of-six floor, so the floor cannot
    be what makes this pass. An earlier version of this test set every
    capability to NOT_MEASURED and was caught by the floor instead -- it
    proved nothing about the guard it was named for.
    """
    one_blank = profile(**{Capability.CONVERSATIONAL: Signal.NOT_MEASURED})

    types = {f.issue_type for f in findings_from_gap(one_blank, pages_read=PAGES, pitchable=True)}

    assert "no_conversational_capability" not in types
    assert types, "the capabilities that *were* measured are still claimable"


def test_a_capability_they_already_run_is_never_pitched() -> None:
    """The operator's original brief: verify they do *not* have an AI setup.
    Pitching a chat widget to a business already running one is the message
    that proves nobody looked."""
    running_chat = profile(**{Capability.CONVERSATIONAL: Signal.PRESENT})

    types = {f.issue_type for f in findings_from_gap(running_chat, pages_read=PAGES)}

    assert "no_conversational_capability" not in types


def test_too_little_was_measured_to_claim_anything() -> None:
    """Planted violation: drop the MIN_MEASURED_CAPABILITIES floor.

    Below four of six the gap describes how much of the site we managed to
    read rather than how the business runs. A confident absence computed from
    two observations is exactly the over-claim this design exists to avoid.
    """
    barely = profile(
        **{
            Capability.MARKETING_AUTOMATION: Signal.NOT_MEASURED,
            Capability.REPUTATION_AUTOMATION: Signal.NOT_MEASURED,
            Capability.ANALYTICS: Signal.NOT_MEASURED,
            Capability.MODERN_SITE: Signal.NOT_MEASURED,
        }
    )

    assert findings_from_gap(barely, pages_read=PAGES, pitchable=True) == []


def test_nothing_is_claimed_when_no_page_could_be_read() -> None:
    """An absence with no pages behind it is an assertion with no evidence,
    and the validator would be right to refuse the message it produced."""
    assert findings_from_gap(profile(), pages_read=(), pitchable=True) == []


# ==========================================================================
# The two booking claims are different claims
# ==========================================================================
def test_no_booking_path_at_all_outranks_bookable_only_by_phone() -> None:
    """Planted violation: raise both booking findings together.

    "I could not find any way to contact you" and "contact is a telephone
    number" are different sentences and the first is the more urgent. Sending
    both would say the business has no contact path and then describe the
    contact path, in one message.
    """
    found = findings_from_gap(
        profile(), pages_read=PAGES, has_contact_path=False, pitchable=True
    )
    types = {f.issue_type for f in found}

    assert "no_self_service_booking" not in types


# ==========================================================================
# Measured before it is sent -- the staging from the design
# ==========================================================================
def test_absences_are_scored_but_not_pitched_until_switched_on() -> None:
    """Planted violation: pitch them the moment they are detected.

    The design stages this deliberately. An absence finding changes what
    Titan says to real businesses, and the honest question -- how many leads
    does this actually make sendable? -- has a number that should be known
    before the message changes. Detected and scored answers it; pitched acts
    on it.

    `pitchable` is what `select_offers` and the composer key on, so this is
    the switch that decides whether a word goes out.
    """
    found = findings_from_gap(profile(), pages_read=PAGES, pitchable=False)

    assert found, "still detected, so the population can be counted"
    assert not any(f.is_pitchable() for f in found), "but nothing is said yet"
