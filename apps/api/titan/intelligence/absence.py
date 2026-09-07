"""What a business does not run, said in a way that survives being wrong.

:mod:`titan.intelligence.findings` detects fourteen things and every one of
them is a website defect. The composer writes about findings, ``select_offers``
is keyed on finding types, and scoring sets ``services_deliverable`` from
whether any offer matched -- so a business with a clean site produces nothing
to say and cannot be written to at all, however far behind it is. For an AI
services business that is the wrong filter entirely: a practice running an AI
receptionist and one taking every booking by telephone were distinguishable
only by whether their CSS was tidy.

:mod:`titan.intelligence.modernisation` has measured the difference since it
was written. This module is the missing step that turns its measurement into
something the composer may cite.

**The whole risk here is over-claiming, and the module is mostly refusals.** A
defect is positive evidence -- we saw a 404, here is the selector. An absence
is not: we looked at eighteen pages and did not see a chat widget, which is a
weaker thing and fails in a worse direction. Telling a clinic that has just
spent twenty thousand pounds on an AI phone system that it has no AI loses
that recipient permanently and deserves to.

Two properties make it sendable anyway.

**The claim is about what was read, not about what they have.** "I went
through your site and couldn't find a way to book without calling" stays true
when we missed something, and invites a correction rather than an argument.
"You have no online booking" is an assertion about their business that
eighteen pages cannot support. The wording is not politeness; it is the
difference between a claim we can defend and one we cannot.

**Nothing unmeasured is ever claimed.** A cookie wall makes every capability
``NOT_MEASURED``, and reading that as "runs nothing" would pitch hardest at
exactly the businesses we learned least about. The floor from the
modernisation reader carries through unchanged.
"""

from __future__ import annotations

from titan.db.enums import FindingCategory, Severity, VerificationMethod
from titan.intelligence.findings import DetectedFinding
from titan.intelligence.modernisation import (
    MIN_MEASURED_CAPABILITIES,
    Capability,
    ModernisationProfile,
    Signal,
)

#: How many pages an absence claim quotes as evidence.
#:
#: Three, matching the defect detectors. The claim map wants enough to show the
#: reading was real and not so much that the message carries a sitemap.
EVIDENCE_PAGES = 3

#: Confidence for an absence.
#:
#: Below the defect detectors' 0.9 and above the 0.7 pitchability floor, and
#: the gap between those numbers is the honest one: a vendor token either was
#: or was not in the page, which is a measurement, but its absence from the
#: pages we read is weaker evidence than a 404 we triggered ourselves.
ABSENCE_CONFIDENCE = 0.8

#: Confidence used while the feature is being measured rather than sent.
#:
#: Deliberately below `DetectedFinding.is_pitchable`'s 0.7 floor. The finding
#: is still detected, stored and scored -- so the question the design stages
#: for, *how many leads does this actually make sendable*, has an answer before
#: the message changes -- but nothing composes a sentence from it.
UNPITCHABLE_CONFIDENCE = 0.5


def _absence(
    *,
    issue_type: str,
    category: FindingCategory,
    title: str,
    observed: str,
    expected: str,
    impact: str,
    solution: str,
    pages: tuple[str, ...],
    pitchable: bool,
) -> DetectedFinding:
    return DetectedFinding(
        category=category,
        issue_type=issue_type,
        title=title,
        severity=Severity.MEDIUM,
        # Below the pitchability floor when the feature is still being
        # measured: the finding is detected, stored and scored, and the
        # composer will not build a sentence out of it. See `findings_from_gap`.
        confidence=ABSENCE_CONFIDENCE if pitchable else UNPITCHABLE_CONFIDENCE,
        # The vendor tokens were read out of the page, so this is a DOM
        # assertion in exactly the sense the pitchability rule means: measured,
        # not inferred by a model.
        verification_method=VerificationMethod.DOM_ASSERTION,
        page_url=pages[0],
        observed_value=observed,
        expected_behavior=expected,
        business_impact=impact,
        recommended_solution=solution,
        evidence=tuple(
            (f"{url}: no such system found on this page", url)
            for url in pages[:EVIDENCE_PAGES]
        ),
    )


#: One entry per capability Titan actually sells against.
#:
#: Analytics and MODERN_SITE are deliberately absent. They are indicators of
#: whether anybody has invested in the business online, not things to sell, and
#: "I couldn't find Google Analytics on your site" is not an opening anybody
#: replies to.
_CLAIMS: dict[Capability, dict[str, object]] = {
    Capability.CONVERSATIONAL: {
        "issue_type": "no_conversational_capability",
        "category": FindingCategory.CONVERSION,
        "title": "Could not find anything that answers a visitor without a person",
        "observed": "no chat, assistant or automated responder found on the pages read",
        "expected": "Something that answers a question when the desk is closed",
        "impact": (
            "An enquiry arriving outside opening hours waits until somebody is "
            "back, and the people who will not wait contact whoever answers first"
        ),
        "solution": "An assistant that answers common questions and captures the enquiry",
    },
    Capability.SELF_SERVICE_BOOKING: {
        "issue_type": "no_self_service_booking",
        "category": FindingCategory.BOOKING,
        "title": "Could not find a way to book without telephoning",
        "observed": "no booking system found on the pages read; contact appears to be by telephone",
        "expected": "A booking path that does not require somebody to answer the phone",
        "impact": (
            "Bookings can only be made while the desk is staffed, so evenings "
            "and weekends convert at whatever the answerphone converts at"
        ),
        "solution": "Self-service booking on the site itself",
    },
    Capability.MARKETING_AUTOMATION: {
        "issue_type": "no_follow_up_automation",
        "category": FindingCategory.RETENTION,
        "title": "Could not find anything that follows up automatically",
        "observed": "no marketing automation or email platform found on the pages read",
        "expected": "Follow-up that happens without somebody remembering to send it",
        "impact": (
            "Following up depends on somebody having time that week, so it is "
            "the first thing dropped when the business is busy"
        ),
        "solution": "An automatic follow-up sequence after an enquiry or a visit",
    },
    Capability.REPUTATION_AUTOMATION: {
        "issue_type": "no_review_automation",
        "category": FindingCategory.RETENTION,
        "title": "Could not find anything that asks for reviews automatically",
        "observed": "no review platform found on the pages read",
        "expected": "Review requests that go out without being remembered",
        "impact": (
            "Reviews arrive only from people motivated enough to leave one "
            "unprompted, which is a different population from your customers"
        ),
        "solution": "Automatic review requests after a visit",
    },
}


def findings_from_gap(
    gap: ModernisationProfile,
    *,
    pages_read: tuple[str, ...],
    has_contact_path: bool = True,
    pitchable: bool = False,
) -> list[DetectedFinding]:
    """Absences worth saying out loud, or nothing at all.

    ``pitchable`` defaults to False, which is the staged rollout from the
    design rather than timidity: absences are detected, stored and scored so
    the population can be counted, and say nothing until somebody has looked
    at that number and turned them on.

    ``has_contact_path`` is False when the site offers no way to make contact
    whatsoever, in which case :mod:`titan.intelligence.findings` has already
    raised ``no_booking_or_enquiry_path``. That is the more urgent sentence and
    the two must not both be sent: a message cannot say there is no way to
    contact the business and then describe the contact path.
    """
    if not pages_read:
        # An absence with no page behind it is an assertion with no evidence,
        # and the validator would be right to refuse whatever it produced.
        return []
    if len(gap.measured) < MIN_MEASURED_CAPABILITIES:
        # Below the floor the profile describes how much of the site we managed
        # to read, not how the business runs.
        return []

    found: list[DetectedFinding] = []
    for capability, claim in _CLAIMS.items():
        if gap.signals.get(capability) is not Signal.ABSENT:
            continue
        if capability is Capability.SELF_SERVICE_BOOKING and not has_contact_path:
            continue
        found.append(
            _absence(pages=pages_read, pitchable=pitchable, **claim)  # type: ignore[arg-type]
        )
    return found


#: Every issue type this module can raise, for the playbooks and the composer.
ABSENCE_ISSUE_TYPES: frozenset[str] = frozenset(
    str(claim["issue_type"]) for claim in _CLAIMS.values()
)

__all__ = ["ABSENCE_ISSUE_TYPES", "findings_from_gap"]
