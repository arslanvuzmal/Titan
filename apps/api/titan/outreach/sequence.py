"""The four-step outreach sequence: its wording, its cadence, its limits.

Every step is rendered *here*, by Titan, before anything is handed to a
provider. That is the difference between this module and the version it is
ported from, and it is not a stylistic one.

The earlier design held steps 2 to 4 as templates inside Smartlead and shipped
their blanks along as custom fields, letting Smartlead schedule and render them
days later. It produced the right words, but it put three of the four messages
outside every gate in this repository: no claim map, no validator, no policy
evaluation, no approval. :class:`titan.delivery.providers.smartlead.SmartleadProvider`
refuses a campaign with more than one sequence step for exactly that reason, and
it is right to.

So the templates here take real values, not ``{{placeholders}}``. A follow-up
becomes an ordinary :class:`~titan.db.models.messaging.MessageDraft` composed by
the pipeline, validated against the evidence that justifies it, and delivered by
the outbox worker after the policy engine has agreed twice -- the same path a
first message takes. :mod:`titan.intelligence.sequencing` decides *which* step is
owed and *when*; this module only knows what each one says.

Length: every step's conversational body is held to at most 80 words, and step 1
to at least 45. The nudge and the sign-off are far shorter; the day-8 note can
reach the pitch's length, because the observation it exists to carry is the
whole point of sending it.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

from titan.outreach.variables import FindingVariables

#: Subject lines, rotated deterministically per lead. No urgency, no fake Re:,
#: no claim -- a subject that overpromises is the fastest route to a complaint.
SUBJECT_ROTATION: Final[tuple[str, ...]] = (
    "quick note about {company}",
    "noticed this on {company}",
    "{company}",
    "small thing I noticed",
    "question about {company}",
    "{short}",
)

#: Separates the conversational message from the legal footer. Personalization
#: rule 12: the compliance block is appended automatically and kept out of the
#: body a human reads as the message.
FOOTER_SEPARATOR: Final = "--"

_WORD = re.compile("[A-Za-z0-9][A-Za-z0-9'-]*")

#: Typographic apostrophe, by code point. Normalised away before counting so
#: "don't" is one word whichever apostrophe the copy happens to use, without
#: putting a character in the pattern that reviewers can mistake for a backtick.
_CURLY_APOSTROPHE: Final = chr(0x2019)

MIN_WORDS: Final = 45
MAX_WORDS: Final = 80

#: Gaps in days from the message before, so the steps land on days 1, 4, 8, 13.
#: Expressed as gaps rather than absolute days because that is what a scheduler
#: comparing against ``last_contacted_at`` can act on without also knowing when
#: the sequence began.
STEP_DELAYS_IN_DAYS: Final[tuple[int, ...]] = (0, 3, 4, 5)

#: ``template_key`` values, matching :class:`titan.intelligence.sequencing.Step`.
#: Step 1 is composed rather than templated, so it has no follow-up key.
TEMPLATE_KEYS: Final[tuple[str, ...]] = (
    "outreach_v2_step1",
    "outreach_v2_followup1",
    "outreach_v2_followup2",
    "outreach_v2_followup3",
)


def count_words(text: str) -> int:
    """Words in the conversational body, ignoring the footer and signature."""
    return len(_WORD.findall(text.replace(_CURLY_APOSTROPHE, "'")))


def salutation(first_name: str | None) -> str:
    """Greeting that cannot render as "Hi ,".

    Most leads are reached on a role address with no person attached. Inventing
    a name is not an option, so the unnamed case drops to a plain greeting
    rather than a personalised-looking one.
    """
    name = (first_name or "").strip()
    return f"Hi {name}," if name else "Hello,"


def rotate_subject(*, lead_id: str, company: str, short: str) -> str:
    """Pick a subject line for this lead, stably.

    Keyed on the lead id rather than a counter or the clock so a retry composes
    the identical subject. A subject that changed between attempts would defeat
    the idempotency the outbox depends on and could show one recipient two
    different first emails.
    """
    digest = hashlib.sha256(lead_id.encode("utf-8")).digest()
    template = SUBJECT_ROTATION[digest[0] % len(SUBJECT_ROTATION)]
    rendered = template.format(company=company or "your site", short=short)
    # A bare "{short}" subject starts lower-case ("the broken booking button"),
    # which reads as a fragment in an inbox list.
    return (rendered[:1].upper() + rendered[1:])[:120]


def compose_first_email(
    *,
    first_name: str | None,
    company_name: str,
    verified_finding: str,
    likely_consequence: str,
) -> str:
    """The day-1 conversational body. No footer, no signature block.

    Every sentence is either a statement about the recipient's site that the
    validator gates, or a statement about what the sender will do next. There is
    no compliment, no metric, and no claim about results elsewhere.
    """
    company = company_name.strip() or "your site"
    return "\n\n".join(
        (
            salutation(first_name),
            f"I was looking through {company} and noticed {verified_finding}.",
            f"It could be making {likely_consequence} harder than it needs to be.",
            ("I mapped a simple way to fix it without replacing your current setup."),
            "Would you like me to send the short breakdown?",
            "Arslan",
        )
    )


def compliance_footer(
    *,
    sender_name: str,
    portfolio_url: str,
    mailing_address: str,
    unsubscribe_line: str,
) -> str:
    """The block appended after ``FOOTER_SEPARATOR``.

    Kept assembled in one place so a message can never ship with the address but
    not the opt-out, or the reverse.

    Carries the sender's full legal name and site even though the message itself
    signs off with a first name only. That is deliberate on both counts: a note
    from one engineer to another is signed "Arslan", while the block that exists
    to satisfy CAN-SPAM identifies the sender in full. It is also what keeps the
    message validator's sender-identity and portfolio-URL checks satisfied
    without pushing formal identification into the conversational text.
    """
    return "\n".join(
        part
        for part in (sender_name, portfolio_url, mailing_address, unsubscribe_line)
        if part
    )


def with_footer(body: str, footer: str) -> str:
    """Attach the compliance block to a conversational body."""
    if not footer:
        return body + "\n"
    return f"{body}\n\n{FOOTER_SEPARATOR}\n{footer}\n"


# ---------------------------------------------------------------------------
# Follow-up bodies
# ---------------------------------------------------------------------------
# Rendered by Titan with values already derived from a verified finding. Each
# takes the same evidence-backed context the first message was justified by, so
# a follow-up can never introduce a claim the first message did not carry.


def compose_follow_up_1(*, first_name: str | None) -> str:
    """Day 4. Asks nothing new and asserts nothing at all.

    Deliberately references no finding: the only claim it makes is about a
    message the sender already sent, so it is safe for any lead that received
    step 1 -- including one whose finding has no phrase mapping.
    """
    return "\n\n".join(
        (
            salutation(first_name),
            "Just checking whether you wanted me to send over the short "
            "breakdown I mentioned.",
            "Arslan",
        )
    )


def compose_follow_up_2(*, first_name: str | None, variables: FindingVariables) -> str:
    """Day 8. One narrower observation, and never a restatement of the pitch.

    Raises rather than degrading when the finding has no mapping. A blank in
    this template renders as "One additional thought on :", which is the exact
    failure the ``supported`` flag exists to prevent.
    """
    if not variables.supported:
        raise ValueError(
            "follow-up 2 needs a mapped finding; an unmapped one would render "
            "its one specific detail as a blank"
        )
    return "\n\n".join(
        (
            salutation(first_name),
            f"One additional thought on {variables.short}:",
            f"{variables.insight}.",
            "That's the part I'd prioritise first rather than rebuilding the "
            "entire process.",
            "Happy to send the workflow if useful.",
            "Arslan",
        )
    )


def compose_follow_up_3(*, first_name: str | None, variables: FindingVariables) -> str:
    """Day 13. Closes the thread and leaves the door open."""
    if not variables.supported:
        raise ValueError(
            "follow-up 3 needs a mapped finding; an unmapped one would render "
            "the reason for the message as a blank"
        )
    return "\n\n".join(
        (
            salutation(first_name),
            "I'll close this out for now.",
            f"I only reached out because {variables.short} still looked worth "
            "addressing.",
            "If it becomes a priority later, happy to share what I found.",
            "Arslan",
        )
    )


__all__ = [
    "FOOTER_SEPARATOR",
    "MAX_WORDS",
    "MIN_WORDS",
    "STEP_DELAYS_IN_DAYS",
    "SUBJECT_ROTATION",
    "TEMPLATE_KEYS",
    "compliance_footer",
    "compose_first_email",
    "compose_follow_up_1",
    "compose_follow_up_2",
    "compose_follow_up_3",
    "count_words",
    "rotate_subject",
    "salutation",
    "with_footer",
]
