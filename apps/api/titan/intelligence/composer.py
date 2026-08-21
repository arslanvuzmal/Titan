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

Four lines, in this order, and never more:

1. an observation, drawn from a finding that carries evidence rows;
2. what it means commercially, in the recipient's own vocabulary;
3. one sentence about fixing *this* problem;
4. an ask small enough to answer with a word.

Two things decide the wording, and both live in :mod:`titan.intelligence.
vernacular`. The **engine** -- is this a conversion problem, a quality problem
or an operational gap -- sets the shape and the size of the claim. The
**vernacular** supplies every sentence that touches the recipient's business,
because a solicitor has enquiries and consultations, a gym has trials and
members, and writing "conversion path" at either of them is writing at them.

So the variation this module produces is **structural, never factual**. Four
registers exist for each line; the *evidence* they are built from is identical
in each, and every factual sentence is emitted together with its claim-map entry
so the two cannot drift apart. A register is chosen by hashing the lead id,
which means the same lead always gets the same message -- an activity retry, a
workflow replay, or a second look a week later all produce byte-identical
output, and the outbox dedupe key keeps meaning what it says.

Deliberately still deterministic rather than model-generated. The model gateway
is wired and available, and the moment its output can be held to the same
claim-map contract this becomes its post-processor rather than its replacement.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from titan.db.enums import Industry
from titan.intelligence.vernacular import (
    Engine,
    Vernacular,
    ask_for,
    capability_for,
    consequence_for,
    engine_for,
    family_capability,
    family_consequence,
    vernacular_for,
)

#: How the greeting is written when nobody's name was published. "Hi there" is
#: the least-bad of a bad set: "Dear Sir/Madam" is a form letter, "Hey!" is
#: presumptuous, and inventing a name is invariant 6 territory.
FALLBACK_GREETING = "Hi there"

#: The band the four lines have to land in, before the signature.
#:
#: Under the floor there is not enough of an observation for a stranger to act
#: on; over the ceiling the message is asking for reading time it has not earned
#: yet. The validator enforces this on the assembled body -- the numbers live
#: here because this is the module that has to hit them.
PITCH_MIN_WORDS = 55
PITCH_MAX_WORDS = 90


class FindingLike(Protocol):
    """The fields the composer reads. Kept narrow so tests need no ORM row."""

    issue_type: str
    title: str
    page_url: str | None
    observed_value: str | None


@dataclass(frozen=True, slots=True)
class ComposerContext:
    org_domain: str
    finding: FindingLike
    evidence_ids: list[str]
    owner_name: str
    portfolio_url: str
    mailing_address: str
    unsubscribe_url: str
    #: Which offer the *headline* finding justified.
    #:
    #: Required, and never written into the prose. It used to be the offer's
    #: prose blurb, interpolated into a sentence about what the sender does --
    #: which is how a message about a broken navigation link ended up pitching
    #: "follow-up for enquiries that do not book immediately". The offer was
    #: real; it was justified by some *other* finding on the same site, and the
    #: reader saw a pitch that had nothing to do with the paragraph above it.
    #:
    #: The sentence about the fix now comes from the vernacular and is about the
    #: finding that was actually cited. This field stays required so a caller
    #: still cannot compose without having found an offer the evidence supports,
    #: and it is stamped into ``template_key`` so reply rates are attributable
    #: to an engine and an offer rather than to folklore.
    offer_key: str
    #: The business's own name, for the subject line. Falls back to the domain,
    #: which is never wrong, only less warm.
    business_name: str | None = None
    #: Decides the vocabulary. ``None`` is honest and gets the general voice --
    #: it does not get a weaker message, only one without an industry noun in it.
    industry: Industry | str | None = None
    #: Published first name, when one was actually found. Never inferred from an
    #: email local part: "sam@" might be Samantha, Samuel, or the sales team.
    contact_first_name: str | None = None
    #: What the register hash is taken over. The lead id, so a retry composes
    #: the same message.
    variant_seed: str = ""
    #: Zero for the first message; above that the opener acknowledges the
    #: earlier one instead of opening cold.
    step_number: int = 0
    #: Set only when the campaign manager has promoted a register on measured
    #: evidence, in which case every lead gets it rather than the one its id
    #: happened to select.
    promoted_variant: int | None = None


@dataclass(frozen=True, slots=True)
class ComposedMessage:
    subject: str
    body: str
    #: One entry per factual sentence. Built beside the sentence, never after.
    claim_map: list[dict[str, Any]] = field(default_factory=list)
    #: Which registers were used. Recorded so a reply-rate difference between
    #: variants is attributable rather than folklore.
    variant: str = ""
    #: Which engine wrote it. The three are different products in the same
    #: envelope and their reply rates have no business being averaged together.
    engine: Engine = Engine.QUALITY
    #: ``engine:offer_key``, stored on the draft row. What makes "conversion
    #: messages reply at four times the rate of quality ones" a query rather
    #: than an opinion.
    template_key: str = ""
    #: Words before the signature. Carried out so the caller can assert the
    #: band rather than counting the body again with a different rule.
    pitch_words: int = 0


# --------------------------------------------------------------------------
# Registers
#
# Each entry is a *phrasing* of the same evidenced fact. Swapping between them
# changes the sentence and not the claim, which is what keeps the claim map
# honest while stopping every recipient getting the same paragraph.
# --------------------------------------------------------------------------

_OBSERVATION_REGISTERS: tuple[str, ...] = (
    "I was looking through {domain} and noticed {description}.",
    "I came across {domain} and noticed {description}.",
    "I was reviewing {domain} and noticed {description}.",
    "Quick note on {domain}: I noticed {description}.",
)

#: Follow-ups open by acknowledging the earlier message. Re-sending the same
#: observation to somebody who did not reply is the single most common way an
#: automated sequence announces that nobody is reading the replies.
_FOLLOWUP_OPENERS: tuple[str, ...] = (
    "Following up on the note I sent about {domain} -- ",
    "Circling back on this one. ",
    "I wrote last week about {domain}. In case it got buried: ",
)


# --------------------------------------------------------------------------
# Subjects
#
# The old set was four shapes applied to every finding in the database, and two
# of them were "A broken step on {domain}" and "Something looks broken" --
# subject lines that could have been sent to any business on the internet by
# somebody who looked at nothing.
#
# A subject now names the thing that is wrong, in the words the owner uses for
# it. No fake Re:, no manufactured urgency, no invented percentage.
# --------------------------------------------------------------------------

#: What a conversion defect is called to the person who owns it. ``None`` means
#: "use the industry's own name for its money page", which is how a broken link
#: on ``/consultation`` becomes "consultation page" at a firm and "booking page"
#: at a clinic.
_CONVERSION_TOPICS: dict[str, str | None] = {
    "broken_primary_cta": None,
    "broken_internal_link": None,
    "high_friction_contact_form": "enquiry form",
    "no_visible_phone_number": "phone number",
}

_CONVERSION_SUBJECTS: tuple[str, ...] = (
    "{business} -- {topic} issue",
    "Quick note about {business_possessive} {topic}",
    "{business_possessive} {topic}",
    "{topic} on {domain}",
)

#: Quality findings sort into four families, and each gets its own subjects.
#: "Small accessibility issue" is honest about the size of an alt-text problem;
#: applying it to a twenty-four-second load time would not be.
_QUALITY_FAMILIES: dict[str, str] = {
    "images_missing_alt_text": "accessibility",
    "serious_accessibility_violations": "accessibility",
    "slow_largest_contentful_paint": "speed",
    "missing_meta_description": "search",
    "no_structured_data": "search",
    "javascript_console_errors": "technical",
    "failed_network_requests": "technical",
    "missing_mobile_viewport": "technical",
    "missing_security_headers": "technical",
}

_QUALITY_SUBJECTS: dict[str, tuple[str, ...]] = {
    "accessibility": (
        "Small accessibility issue on {domain}",
        "Quick website note for {business}",
        "Accessibility note for {business}",
        "A website note for {business}",
    ),
    "speed": (
        "{business} -- page speed note",
        "Quick note about how {domain} loads",
        "Load time on {domain}",
        "{business_possessive} homepage load time",
    ),
    "search": (
        "Quick note about how {business} shows in search",
        "{business} in search results",
        "A search listing note for {business}",
        "How {domain} appears in search",
    ),
    "technical": (
        "Quick website note for {business}",
        "Small technical note on {domain}",
        "{business} -- a small site issue",
        "A note on {domain}",
    ),
}

_AUTOMATION_SUBJECTS: tuple[str, ...] = (
    "Idea for {business_possessive} {workflow}",
    "Small idea for {business_possessive} {workflow}",
    "A thought on {business_possessive} {workflow}",
    "{workflow} at {business}",
)

#: Longer than this and the client truncates it mid-word, so the recipient sees
#: a sentence that stops. Shorter than the validator's own ceiling on purpose.
MAX_SUBJECT_CHARS = 78
#: A business name long enough to eat the whole subject line is cut back to the
#: domain instead, which is short and always true.
_MAX_BUSINESS_NAME_CHARS = 34


# --------------------------------------------------------------------------
# Descriptions
#
# One clause per finding type, in the present tense, starting lower case so it
# slots into any of the observation registers. The fallback is the finding's own
# title, which is always populated and was written by the detector that saw the
# page.
# --------------------------------------------------------------------------

_LEADING_COUNT = re.compile(r"^\s*(\d+)")
_MILLISECONDS = re.compile(r"(\d+)\s*ms", re.IGNORECASE)
_RATIO = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


def _page_name(finding: FindingLike) -> str:
    """How a page is named to somebody who owns it.

    The finding has always carried the exact URL and the message threw it away:
    "a navigation link points at a page that returns HTTP 404" was written to
    the owner of a site whose ``/book`` page was down. It is the single most
    checkable fact available and the one that makes the difference between a
    stranger's generic warning and something that could only have been written
    about this business.

    Rendered as the path rather than the whole URL, because "your /book page"
    reads as English and "your https://example.com/book page" does not.
    """
    url = (finding.page_url or "").strip()
    if not url:
        return "the page"
    path = url.split("://", 1)[-1]
    path = path[path.find("/") :] if "/" in path else "/"
    path = path.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if not path:
        return "your home page"
    return f"your {path} page"


def _count(finding: FindingLike, default: int = 0) -> int:
    """The integer the detector put at the front of its own title.

    Titles read "4 internal page(s) return an error" and "50 resource(s) failed
    to load". The count is the specific part and the ``(s)`` is not something to
    send to a stranger, so the number is taken and the noun is written here.
    """
    match = _LEADING_COUNT.match(finding.title or "")
    return int(match.group(1)) if match else default


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular}" if count == 1 else f"{count} {plural}"


def _describe(finding: FindingLike) -> str:
    issue = finding.issue_type
    observed = (finding.observed_value or "").strip()
    page = _page_name(finding)

    if issue == "broken_internal_link":
        return f"{page} currently returns {observed or 'an error'}"
    if issue == "broken_primary_cta":
        return f"the main button on {page} returns {observed or 'an error'}"
    if issue == "high_friction_contact_form":
        return f"the enquiry form on {page} asks for {observed or 'a lot of fields'}"
    if issue == "no_booking_or_enquiry_path":
        return "there is no booking link or enquiry form anywhere on the site"
    if issue == "no_visible_phone_number":
        return "there is no phone number on any page I looked at"
    if issue == "images_missing_alt_text":
        ratio = _RATIO.match(observed)
        counted = (
            f"{ratio.group(1)} of {ratio.group(2)} images"
            if ratio
            else _plural(_count(finding, 1), "image", "images")
        )
        return f"{counted} on {page} are missing alternative text"
    if issue == "serious_accessibility_violations":
        rule = observed.split("(", 1)[0].strip().replace("-", " ")
        if rule:
            return f"{page} fails an accessibility check for {rule}"
        return (
            f"{page} has "
            f"{_plural(_count(finding, 1), 'serious accessibility issue', 'serious accessibility issues')}"
        )
    if issue == "slow_largest_contentful_paint":
        ms = _MILLISECONDS.search(observed)
        if ms:
            return f"{page} takes {int(ms.group(1)) / 1000:.1f} seconds to show its main content"
        return f"{page} is slow to show its main content"
    if issue == "javascript_console_errors":
        return (
            f"{page} raises "
            f"{_plural(_count(finding, 1), 'script error', 'script errors')} as it loads"
        )
    if issue == "failed_network_requests":
        return (
            f"{_plural(_count(finding, 1), 'file', 'files')} on {page} "
            "fail to load with the rest of it"
        )
    if issue == "missing_mobile_viewport":
        return (
            "the site has no mobile viewport tag, so it renders at desktop "
            "width on phones"
        )
    if issue == "missing_meta_description":
        # No trailing "so search engines write their own summary" here: the
        # consequence line says exactly that, and the two together read as the
        # same sentence twice.
        return f"{page} carries no meta description"
    if issue == "no_structured_data":
        return "the site carries no structured data for search engines to read"
    if issue == "missing_security_headers":
        return (
            f"{_plural(_count(finding, 1), 'standard security header is', 'standard security headers are')} "
            f"missing from {page}"
        )
    # Always populated, and written by the detector that saw the page.
    return (finding.title or "something looks wrong").rstrip(".")[0].lower() + (
        finding.title or "something looks wrong"
    ).rstrip(".")[1:]


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
    vern = vernacular_for(ctx.industry)
    engine = engine_for(finding.issue_type, finding.page_url)
    family = _QUALITY_FAMILIES.get(finding.issue_type, "technical")

    # 1. The observation. The only sentence that asserts a fact about the site,
    #    and the one the claim map is anchored on.
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

    # 2. What it means to this business, in this business's words -- unless the
    #    industry sentence would understate it, which is true of load time and
    #    of search listings in every industry alike.
    consequence = consequence_for(vern, engine)
    if engine is Engine.QUALITY:
        consequence = family_consequence(family) or consequence
    # 3. What the sender does about this exact problem, and nothing else.
    capability = capability_for(vern, engine)
    if engine is Engine.QUALITY:
        capability = family_capability(family) or capability
    # 4. One ask, answerable in a word, and never a meeting.
    ask = ask_for(engine, index)

    greeting = (
        f"Hi {ctx.contact_first_name}" if ctx.contact_first_name else FALLBACK_GREETING
    )

    pitch = f"{greeting},\n\n{observation}\n\n{consequence}\n\n{capability}\n\n{ask}"
    body = (
        f"{pitch}\n\n"
        f"{ctx.owner_name}\n"
        f"{ctx.portfolio_url}\n"
        f"{ctx.mailing_address}\n"
        f"Unsubscribe: {ctx.unsubscribe_url}\n"
    )

    subject = _subject(ctx, vern, engine, family, index)

    # The observation is a claim about the recipient's site. The consequence is
    # Titan's characterisation of that same finding, so it is a claim about
    # their business and belongs here too -- omitting it is what the validator
    # correctly rejected the first version of this for.
    #
    # The capability sentence is a statement about the *sender* and carries no
    # claim-map entry, which is exactly why it may not name anything on the
    # recipient's site. "I noticed a few other things while I was there" used to
    # sit in that slot: an assertion about their business, unevidenced, made in
    # the one sentence nothing checks.
    claim_map = [
        {
            "sentence": observation,
            "claim": finding.issue_type,
            "finding_id": _finding_id(finding),
            "evidence_ids": list(ctx.evidence_ids),
            "source_url": finding.page_url,
        },
        {
            "sentence": consequence,
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
        engine=engine,
        template_key=f"{engine.value}:{ctx.offer_key}"[:60],
        pitch_words=len(pitch.split()),
    )


def _possessive(name: str) -> str:
    """ "Hartley & Co Solicitors's consultation page" is not English.

    A name already ending in s takes the bare apostrophe. Worth the four lines:
    the subject line is the one string every recipient reads, and a doubled
    possessive in it is the most visible possible sign that nobody proofread.
    """
    name = name.rstrip()
    return f"{name}'" if name.endswith(("s", "S")) else f"{name}'s"


def _subject(
    ctx: ComposerContext, vern: Vernacular, engine: Engine, family: str, index: int
) -> str:
    business = (ctx.business_name or "").strip()
    if not business or len(business) > _MAX_BUSINESS_NAME_CHARS:
        business = ctx.org_domain
    fields = {
        "business": business,
        "business_possessive": _possessive(business),
        "domain": ctx.org_domain,
    }

    if engine is Engine.AUTOMATION:
        template = _AUTOMATION_SUBJECTS[index % len(_AUTOMATION_SUBJECTS)]
        subject = template.format(workflow=vern.automation_workflow, **fields)
    elif engine is Engine.CONVERSION:
        topic = _CONVERSION_TOPICS.get(ctx.finding.issue_type, None) or vern.money_page
        template = _CONVERSION_SUBJECTS[index % len(_CONVERSION_SUBJECTS)]
        subject = template.format(topic=topic, **fields)
    else:
        pool = _QUALITY_SUBJECTS[family]
        subject = pool[index % len(pool)].format(**fields)

    # Registers that open on the topic rather than the name would otherwise send
    # "enquiry form on example.test" -- a sentence fragment starting lower case,
    # which reads as a truncation of something the recipient never saw.
    subject = subject.strip()
    return (subject[:1].upper() + subject[1:])[:MAX_SUBJECT_CHARS]


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


def _finding_id(finding: FindingLike) -> str:
    return str(getattr(finding, "id", ""))


__all__ = [
    "FALLBACK_GREETING",
    "MAX_SUBJECT_CHARS",
    "PITCH_MAX_WORDS",
    "PITCH_MIN_WORDS",
    "VARIANT_REGISTERS",
    "ComposedMessage",
    "ComposerContext",
    "FindingLike",
    "compose",
]
