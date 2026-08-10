"""Writing the message.

What replaced a single hardcoded template. Every lead used to receive the same
four sentences with a domain substituted in, which is two problems wearing one
coat: it reads as a mail merge, and a hundred near-identical bodies leaving one
domain in a day is the shape a filter is built to catch.

**Persuasion here is specificity, not enthusiasm.** The validator already
rejects the levers cold email usually reaches for -- manufactured deadlines,
fear framing, flattery, fabricated metrics, "I hope this finds you well". That
is not a constraint working against good copy; it is most of the definition of
it. What is left to be good at is naming precisely what is wrong, on which page,
and why it costs them something. A stranger who reads one sentence that could
only have been written about their business will read the second one.

So the variation this module produces is **structural, never factual**. Four
registers exist for the opening, the impact line and the ask; the *evidence*
they are built from is identical in each, and every factual sentence is emitted
together with its claim-map entry so the two cannot drift apart. A register is
chosen by hashing the lead id, which means the same lead always gets the same
message -- an activity retry, a workflow replay, or a second look a week later
all produce byte-identical output, and the outbox dedupe key keeps meaning what
it says.

Deliberately still deterministic rather than model-generated. The model gateway
is wired and available, and the moment its output can be held to the same
claim-map contract this becomes its post-processor rather than its replacement.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

#: How the greeting is written when nobody's name was published. "Hi there" is
#: the least-bad of a bad set: "Dear Sir/Madam" is a form letter, "Hey!" is
#: presumptuous, and inventing a name is invariant 6 territory.
FALLBACK_GREETING = "Hi there"


class FindingLike(Protocol):
    """The fields the composer reads. Kept narrow so tests need no ORM row."""

    issue_type: str
    title: str
    page_url: str | None
    observed_value: str | None
    business_impact: str | None
    recommended_solution: str | None


@dataclass(frozen=True, slots=True)
class ComposerContext:
    org_domain: str
    finding: FindingLike
    evidence_ids: list[str]
    owner_name: str
    portfolio_url: str
    mailing_address: str
    unsubscribe_url: str
    #: What the offer delivers, as a NOUN PHRASE. An imperative here produces
    #: "I build point the button at a tested flow".
    solution: str = "enquiry capture and follow-up automation"
    #: Published first name, when one was actually found. Never inferred from an
    #: email local part: "sam@" might be Samantha, Samuel, or the sales team.
    contact_first_name: str | None = None
    #: Seeds register selection. The lead id, so the choice is stable per lead.
    variant_seed: str = ""
    #: 0 for the first message. Follow-ups open differently -- repeating the
    #: original opening at somebody who ignored it reads as a broken robot.
    step_number: int = 0


@dataclass(frozen=True, slots=True)
class ComposedMessage:
    subject: str
    body: str
    #: One entry per factual sentence. Built beside the sentence, never after.
    claim_map: list[dict[str, Any]] = field(default_factory=list)
    #: Which registers were used. Recorded so a reply-rate difference between
    #: variants is attributable rather than folklore.
    variant: str = ""


# --------------------------------------------------------------------------
# Registers
#
# Each entry is a *phrasing* of the same evidenced fact. Swapping between them
# changes the sentence and not the claim, which is what keeps the claim map
# honest while stopping every recipient getting the same paragraph.
# --------------------------------------------------------------------------

_OBSERVATION_REGISTERS: tuple[str, ...] = (
    "I was looking at {domain} and noticed {description}.",
    "Something on {domain} looks unintentional: {description}.",
    "While going through {domain} I found that {description}.",
    "A quick note about {domain} -- {description}.",
)

_IMPACT_REGISTERS: tuple[str, ...] = (
    "{impact}",
    "In practice that means {impact_lower}",
    "The cost of that is straightforward: {impact_lower}",
    "That matters because {impact_lower}",
)

_OFFER_REGISTERS: tuple[str, ...] = (
    "I build {solution} for businesses of this size.",
    "Fixing this sort of thing is what I do -- {solution}, mostly for firms your size.",
    "I work on {solution} with businesses at about your scale.",
    "My work is {solution}, usually for teams around your size.",
)

#: One ask, small, and answerable in a word. A cold email that asks for a
#: 30-minute discovery call is asking a stranger for something they have no
#: reason to give yet; a yes/no question costs them nothing to answer.
_ASK_REGISTERS: tuple[str, ...] = (
    "Worth a short call next week?",
    "Would it help if I sent over what I would change?",
    "Happy to sketch out what fixing it involves -- want me to?",
    "Would ten minutes next week be useful?",
)

_SUBJECT_REGISTERS: tuple[str, ...] = (
    "{short_description} on {domain}",
    "{domain}: {short_description}",
    "Noticed this on {domain}",
    "A broken step on {domain}",
)

#: Follow-ups open by acknowledging the earlier message. Re-sending the same
#: observation to somebody who did not reply is the single most common way an
#: automated sequence announces that nobody is reading the replies.
_FOLLOWUP_OPENERS: tuple[str, ...] = (
    "Following up on the note I sent about {domain} -- ",
    "Circling back on this one. ",
    "I wrote last week about {domain}. In case it got buried: ",
)

#: Plain-language renderings of each machine issue_type. The fallback is the
#: finding's own title, which is always populated.
_DESCRIPTIONS: dict[str, str] = {
    "broken_primary_cta": "the main call-to-action button returns {observed}",
    "no_booking_or_enquiry_path": "there is no booking link or enquiry form anywhere on the site",
    "high_friction_contact_form": "the enquiry form asks for {observed} separate fields",
    "missing_mobile_viewport": "the homepage has no mobile viewport tag, so it renders at desktop width on phones",
    "broken_internal_link": "a navigation link points at a page that returns {observed}",
    "javascript_console_errors": "the page raises JavaScript errors as it loads",
    "no_visible_phone_number": "there is no phone number on any page I looked at",
}

#: Shorter forms for the subject line, where the sentence version would be cut.
_SHORT_DESCRIPTIONS: dict[str, str] = {
    "broken_primary_cta": "Your booking button is broken",
    "no_booking_or_enquiry_path": "No way to enquire",
    "high_friction_contact_form": "Your enquiry form is losing people",
    "missing_mobile_viewport": "The site breaks on mobile",
    "broken_internal_link": "A broken link",
    "javascript_console_errors": "JavaScript errors",
    "no_visible_phone_number": "No phone number listed",
}


def compose(ctx: ComposerContext) -> ComposedMessage:
    """Build a message and its claim map together.

    The two are produced in the same expression for each factual sentence
    because building them separately is how they drift: someone edits a
    template, the claim map still describes the old wording, and the validator
    passes a sentence nothing actually supports.
    """
    index = _register_index(
        ctx.variant_seed or ctx.org_domain, len(_OBSERVATION_REGISTERS)
    )
    finding = ctx.finding

    description = _describe(finding)
    observation = _OBSERVATION_REGISTERS[index].format(
        domain=ctx.org_domain, description=description
    )
    if ctx.step_number > 0:
        opener = _FOLLOWUP_OPENERS[index % len(_FOLLOWUP_OPENERS)].format(
            domain=ctx.org_domain
        )
        # Lowercased join: "In case it got buried: I was looking at..." reads as
        # two sentences colliding.
        observation = opener + observation[0].lower() + observation[1:]

    impact_text = (
        (finding.business_impact or _default_impact(finding)).strip().rstrip(".")
    )
    impact = _IMPACT_REGISTERS[index].format(
        impact=impact_text + ".",
        impact_lower=impact_text[0].lower() + impact_text[1:] + ".",
    )

    offer = _OFFER_REGISTERS[index].format(solution=ctx.solution.lower())
    ask = _ASK_REGISTERS[index]

    greeting = (
        f"Hi {ctx.contact_first_name}" if ctx.contact_first_name else FALLBACK_GREETING
    )

    body = (
        f"{greeting},\n\n"
        f"{observation}\n\n"
        f"{impact} {offer}\n\n"
        f"{ask}\n\n"
        f"{ctx.owner_name}\n"
        f"{ctx.portfolio_url}\n"
        f"{ctx.mailing_address}\n"
        f"Unsubscribe: {ctx.unsubscribe_url}\n"
    )

    subject = _SUBJECT_REGISTERS[index].format(
        domain=ctx.org_domain,
        short_description=_short_description(finding),
    )[:120]

    # Both factual sentences map to the same finding. The impact line is Titan's
    # own characterisation of that finding, so it is a claim about the
    # recipient's business and belongs here -- omitting it is what the validator
    # correctly rejected the first version of this for.
    claim_map = [
        {
            "sentence": observation,
            "claim": finding.issue_type,
            "finding_id": _finding_id(finding),
            "evidence_ids": list(ctx.evidence_ids),
            "source_url": finding.page_url,
        },
        {
            "sentence": impact,
            "claim": f"{finding.issue_type}:business_impact",
            "finding_id": _finding_id(finding),
            "evidence_ids": list(ctx.evidence_ids),
            "source_url": finding.page_url,
        },
    ]

    return ComposedMessage(
        subject=subject,
        body=body,
        claim_map=claim_map,
        variant=f"v{index}" + (f":step{ctx.step_number}" if ctx.step_number else ""),
    )


def _register_index(seed: str, modulo: int) -> int:
    """Pick a register from a stable hash of the lead.

    Not random: the same lead must compose to the same message on every retry,
    replay and re-run, or an activity retry would produce a second, differently
    worded draft for a person who has already been written to.

    Not ``hash()`` either -- Python randomises string hashing per process, so
    the "stable" choice would change on every restart.
    """
    digest = hashlib.sha256(seed.encode("utf-8", "replace")).digest()
    return digest[0] % modulo


def _describe(finding: FindingLike) -> str:
    observed = (finding.observed_value or "").strip()
    template = _DESCRIPTIONS.get(finding.issue_type)
    if template is None:
        # The title is always populated and was written by the detector, which
        # saw the page. Better than a generic sentence about an unknown problem.
        return finding.title.lower().rstrip(".")
    return template.format(observed=observed or "an error")


def _short_description(finding: FindingLike) -> str:
    return _SHORT_DESCRIPTIONS.get(finding.issue_type, "Something looks broken")


def _default_impact(finding: FindingLike) -> str:
    """Used only when the detector recorded no business impact.

    Deliberately vague about magnitude. A specific number here would be a
    fabricated metric, which the validator rejects and which would deserve to be
    rejected: nobody has measured this business's conversion rate.
    """
    return "That is the step most likely to be used by someone ready to get in touch"


def _finding_id(finding: FindingLike) -> str:
    return str(getattr(finding, "id", ""))


__all__ = [
    "FALLBACK_GREETING",
    "ComposedMessage",
    "ComposerContext",
    "FindingLike",
    "compose",
]
