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
    #:
    #: Required, and it used to carry a default. The default was the problem: it
    #: read "enquiry capture and follow-up automation", so a lead whose evidence
    #: matched no offer in its industry's playbook still got a message claiming
    #: that is what the sender builds -- a pitch unrelated to the finding it had
    #: just cited, and a capability claim nobody checked. ``select_offers``
    #: already says an empty result means "there is nothing truthful to offer";
    #: a default here quietly overrode that. Composing now requires the caller to
    #: have found a real offer, so the refusal happens where the evidence is,
    #: rather than being papered over here.
    solution: str
    #: Published first name, when one was actually found. Never inferred from an
    #: email local part: "sam@" might be Samantha, Samuel, or the sales team.
    contact_first_name: str | None = None
    #: The business's public review count and rating, when Google published
    #: them. ``None`` means unknown, and the stakes sentence is then left out
    #: rather than guessed at -- there is no honest way to imply an audience
    #: nobody measured.
    review_count: int | None = None
    rating: float | None = None
    #: Seeds register selection. The lead id, so the choice is stable per lead.
    variant_seed: str = ""
    #: A register the campaign manager has promoted on measured evidence. None
    #: means no opinion, and selection stays per lead as it always has.
    #:
    #: Promotion narrows what gets sent to one already-written register; it
    #: never supplies wording. Out-of-range values are ignored rather than
    #: wrapped, because a modulo here would quietly promote a register nobody
    #: measured.
    promoted_variant: int | None = None
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

#: What the defect is costing, said with the only audience figure that is
#: actually known.
#:
#: This is the sentence that makes the message land, and it is the one that has
#: to be handled most carefully. The temptation is a number -- "you are losing
#: £4,000 a month" -- which nobody has measured and which the validator rejects
#: as a fabricated metric, correctly: the recipient can tell, and a stranger who
#: invents a figure about your business has told you everything about
#: themselves.
#:
#: What *is* known is the review count on their own Google listing. Naming it
#: alongside the broken step does the same work honestly: it puts a real,
#: checkable number next to a real, checkable fault and lets the owner do the
#: arithmetic. That is the difference between pressure and pretence.
#:
#: Below the floor the sentence is omitted entirely. "Your 3 reviews" argues
#: against the pitch.
MIN_REVIEWS_WORTH_CITING = 40

# NOT YET IN USE. The sentence is written and the numbers are real, and it is
# parked because `message_validator` requires every claim about the recipient to
# name a finding that exists and carries evidence rows -- invariant 7, and the
# rule that stops this system asserting anything it cannot show.
#
# A review count is backed by the stored Places record rather than by a crawl
# finding, so it has no finding to point at. Pointing it at the *defect's*
# finding would assert that the crawler observed the review count, which it did
# not, and that is exactly the kind of quiet untruth the claim map exists to
# prevent.
#
# The right fix is an evidence path for first-party record data, so the
# strongest sentence available can be said with the same backing as every other.
# Loosening the validator to get there would trade the guarantee for a
# paragraph.
_STAKES_REGISTERS: tuple[str, ...] = (
    "Your Google listing carries {reviews} reviews at {rating}, and that is "
    "the audience arriving at it.",
    "That is the page {reviews} reviews' worth of interest is pointed at.",
    "For a practice with {reviews} reviews at {rating}, that is a lot of "
    "people meeting a dead end.",
    "{reviews} reviews at {rating} is a steady stream of people, and this is "
    "where they land.",
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

#: Every register carries the specific. Two of the four used to be "Noticed
#: this on {domain}" and "A broken step on {domain}" -- subject lines that could
#: have been sent to any business on the internet, and which a recipient has
#: seen a hundred times from people who looked at nothing. Which register a lead
#: gets is decided by a hash of its id, so a generic option meant half the
#: portfolio got the weakest possible opening by chance.
_SUBJECT_REGISTERS: tuple[str, ...] = (
    "{short_description}",
    "{domain}: {short_description}",
    "Quick note: {short_description_lower}",
    "{short_description} -- {domain}",
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
    "broken_primary_cta": "the main button on {page} returns {observed}",
    "no_booking_or_enquiry_path": "there is no booking link or enquiry form anywhere on the site",
    "high_friction_contact_form": "the enquiry form on {page} asks for {observed} separate fields",
    "missing_mobile_viewport": "the homepage has no mobile viewport tag, so it renders at desktop width on phones",
    "broken_internal_link": "{page} returns {observed}",
    "javascript_console_errors": "{page} raises JavaScript errors as it loads",
    "no_visible_phone_number": "there is no phone number on any page I looked at",
}

#: Shorter forms for the subject line, where the sentence version would be cut.
_SHORT_DESCRIPTIONS: dict[str, str] = {
    "broken_primary_cta": "Your booking button is broken",
    "no_booking_or_enquiry_path": "No way to enquire",
    "high_friction_contact_form": "Your enquiry form is losing people",
    "missing_mobile_viewport": "The site breaks on mobile",
    # "A broken link" told the reader nothing and could have been about any site
    # on the internet. The path is what makes a subject line worth opening.
    "broken_internal_link": "{page} is down",
    "javascript_console_errors": "JavaScript errors",
    "no_visible_phone_number": "No phone number listed",
}


#: How a page is named to somebody who owns it.
#:
#: The finding has always carried the exact URL and the message threw it away:
#: "a navigation link points at a page that returns HTTP 404" was written to the
#: owner of a site whose ``/book`` page was down. It is the single most
#: checkable fact available and the one that makes the difference between a
#: stranger's generic warning and something that could only have been written
#: about this business.
#:
#: Rendered as the path rather than the whole URL, because "your /book page"
#: reads as English and "your https://example.com/book page" does not.
def _page_name(finding: FindingLike) -> str:
    url = (finding.page_url or "").strip()
    if not url:
        return "the page"
    path = url.split("://", 1)[-1]
    path = path[path.find("/") :] if "/" in path else "/"
    path = path.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if not path:
        return "your home page"
    return f"your {path} page"


def compose(ctx: ComposerContext) -> ComposedMessage:
    """Build a message and its claim map together.

    The two are produced in the same expression for each factual sentence
    because building them separately is how they drift: someone edits a
    template, the claim map still describes the old wording, and the validator
    passes a sentence nothing actually supports.
    """
    if ctx.promoted_variant is not None and (
        0 <= ctx.promoted_variant < len(_OBSERVATION_REGISTERS)
    ):
        index = ctx.promoted_variant
    else:
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
        f"{impact}\n\n"
        f"{offer}\n\n"
        f"{ask}\n\n"
        f"{ctx.owner_name}\n"
        f"{ctx.portfolio_url}\n"
        f"{ctx.mailing_address}\n"
        f"Unsubscribe: {ctx.unsubscribe_url}\n"
    )

    short = _short_description(finding)
    subject = _SUBJECT_REGISTERS[index].format(
        domain=ctx.org_domain,
        short_description=short,
        short_description_lower=short[0].lower() + short[1:] if short else short,
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


#: How many phrasing registers exist. Public because the campaign manager needs
#: to bound a promotion against it, and reaching into the private tuple from
#: outside would couple the actuator's bound to this module's internals.
VARIANT_REGISTERS = len(_OBSERVATION_REGISTERS)


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


def _stakes(ctx: ComposerContext, index: int) -> str:
    """The audience sentence, or nothing at all.

    Omitted rather than softened when the count is unknown or small. A message
    that says "your many reviews" to a business with four of them is worse than
    saying nothing, because it proves nobody looked.
    """
    reviews = ctx.review_count
    if reviews is None or reviews < MIN_REVIEWS_WORTH_CITING:
        return ""
    rating = (
        f"{ctx.rating:.1f} stars" if ctx.rating else "a rating people evidently trust"
    )
    return _STAKES_REGISTERS[index % len(_STAKES_REGISTERS)].format(
        reviews=f"{reviews:,}", rating=rating
    )


def _describe(finding: FindingLike) -> str:
    observed = (finding.observed_value or "").strip()
    template = _DESCRIPTIONS.get(finding.issue_type)
    if template is None:
        # The title is always populated and was written by the detector, which
        # saw the page. Better than a generic sentence about an unknown problem.
        return finding.title.lower().rstrip(".")
    return template.format(observed=observed or "an error", page=_page_name(finding))


def _short_description(finding: FindingLike) -> str:
    template = _SHORT_DESCRIPTIONS.get(finding.issue_type, "Something looks broken")
    text = template.format(page=_page_name(finding))
    return text[0].upper() + text[1:] if text else text


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
    "VARIANT_REGISTERS",
    "ComposedMessage",
    "ComposerContext",
    "FindingLike",
    "compose",
]
