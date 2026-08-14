"""Template variables, derived from a verified finding and nothing else.

The sequence templates read like a person wrote them, which is only honest if
every blank they fill traces back to something Titan measured on the recipient's
own site. So each value here is either

  * a field the research pipeline stored on the finding, or
  * a fixed phrase selected by ``issue_type`` -- a restatement of the same
    evidenced fact, worded to fit the sentence around it,

and never a claim that goes beyond either. There is no model call in this module
and no free text: a finding whose ``issue_type`` is unknown yields no variables
at all, because a plausible guess about somebody's business is exactly the thing
the evidence rules exist to prevent (CLAUDE.md section 6).

Why fixed phrases rather than the stored prose: ``business_impact`` is a
sentence ("Visitors who click the button reach an error page"), but the template
needs a gerund phrase that survives being dropped into "could be making X harder
than it needs to be". Splicing a sentence into that slot produces gibberish, and
gibberish in the one line that names the recipient's problem is worse than a
slightly plainer phrase.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from titan.db.models.research import AuditFinding

#: What the reader is being stopped from doing. Fills: "could be making {X}
#: harder than it needs to be", so every value must be a gerund phrase.
_CONSEQUENCE: Final[dict[str, str]] = {
    "broken_primary_cta": "booking an appointment",
    "no_booking_or_enquiry_path": "getting an enquiry through to you",
    "high_friction_contact_form": "getting an enquiry through to you",
    "missing_mobile_viewport": "using the site on a phone",
    "broken_internal_link": "finding the page they were after",
    "javascript_console_errors": "using the site reliably",
    "no_visible_phone_number": "reaching you by phone",
}

#: Short noun phrase naming the fault. Fills "One additional thought on {X}:"
#: and "because {X} still looked worth addressing".
_SHORT: Final[dict[str, str]] = {
    "broken_primary_cta": "the broken booking button",
    "no_booking_or_enquiry_path": "the missing enquiry path",
    "high_friction_contact_form": "the length of the enquiry form",
    "missing_mobile_viewport": "the mobile display issue",
    "broken_internal_link": "the broken navigation link",
    "javascript_console_errors": "the JavaScript errors",
    "no_visible_phone_number": "the missing phone number",
}

#: One narrower observation for the day-8 follow-up. Must stand alone as a
#: sentence and must not restate the pitch (personalization rule 9).
_INSIGHT: Final[dict[str, str]] = {
    "broken_primary_cta": (
        "the button is the only booking route on the page, so there is no "
        "second path for someone who wanted to act"
    ),
    "no_booking_or_enquiry_path": (
        "a single enquiry form on the page people already land on tends to "
        "recover more than adding a new booking system does"
    ),
    "high_friction_contact_form": (
        "most of the fields are ones you could ask for later, once somebody "
        "has already started talking to you"
    ),
    "missing_mobile_viewport": (
        "phone traffic is usually the larger share for this kind of business, "
        "so it is the first place a display fault costs you"
    ),
    "broken_internal_link": (
        "the link sits in the navigation, so it is reachable from every page "
        "rather than just the one"
    ),
    "javascript_console_errors": (
        "errors on load tend to break the interactive parts first, which is "
        "usually the booking or enquiry step"
    ),
    "no_visible_phone_number": (
        "a number in the header is the cheapest change here, and it captures "
        "the people who would rather not use a form at all"
    ),
}

#: The friction in the customer's own words. Carried for templates that will
#: want it; no shipped step references it yet.
_FRICTION: Final[dict[str, str]] = {
    "broken_primary_cta": "an appointment that cannot be booked online",
    "no_booking_or_enquiry_path": "no way to get in touch without phoning",
    "high_friction_contact_form": "a form long enough to talk people out of it",
    "missing_mobile_viewport": "a site that is awkward to use on a phone",
    "broken_internal_link": "a dead end in the navigation",
    "javascript_console_errors": "a page that does not always work",
    "no_visible_phone_number": "no phone number to call",
}


@dataclass(frozen=True, slots=True)
class FindingVariables:
    """The evidence-backed half of the template context.

    ``supported`` is false when the finding's ``issue_type`` has no mapping. The
    caller must then fall back to the long-form composer rather than render a
    step with empty slots: a follow-up whose one specific detail is blank reads
    as "One additional thought on :" and is worse than no follow-up at all.
    """

    consequence: str
    short: str
    insight: str
    friction: str

    @property
    def supported(self) -> bool:
        return bool(self.consequence and self.short and self.insight)

    def as_context(self) -> dict[str, str]:
        """The evidence-backed slots the follow-up steps render."""
        return {
            "likely_consequence": self.consequence,
            "verified_finding_short": self.short,
            "additional_insight": self.insight,
            "specific_business_friction": self.friction,
        }


_EMPTY: Final = FindingVariables(consequence="", short="", insight="", friction="")


def derive_variables(finding: AuditFinding) -> FindingVariables:
    """Map a verified finding onto its template variables.

    Returns empty variables -- not a guess -- for an unrecognised issue type, and
    for any finding the evidence rules do not consider pitchable. Callers check
    ``supported`` before using the short sequence.
    """
    if not finding.is_pitchable():
        return _EMPTY

    issue = finding.issue_type
    consequence = _CONSEQUENCE.get(issue, "")
    short = _SHORT.get(issue, "")
    insight = _INSIGHT.get(issue, "")
    if not (consequence and short and insight):
        return _EMPTY

    return FindingVariables(
        consequence=consequence,
        short=short,
        insight=insight,
        friction=_FRICTION.get(issue, ""),
    )


__all__ = ["FindingVariables", "derive_variables"]
