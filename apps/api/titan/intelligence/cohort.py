"""Where a business stands against the ones Titan actually looked at.

The persuasive half of the absence design. A measured absence establishes that
somebody read the site; the comparison is what makes not having it feel like a
position rather than a preference. The operator put it plainly: mentioning
genuine faults builds trust, and the fear of getting behind is what moves
somebody to act on them.

**It is a claim about our own reading, and the wording carries that.** We
crawled ten clinics in Manchester. We did not survey Manchester, and a sentence
that implies we did is one we would have to retract to the first recipient who
asks how we know. So: *"six of the ten private clinics I looked at in
Manchester"*. Weaker on paper, and the only version that survives the question.

That is also the difference between this and every other sentence in a Titan
message. Everything else is a claim about the recipient, checkable by the
recipient. This is a claim about third parties, published to a stranger, for
commercial gain -- so it never names anybody, never runs on a sample too small
to mean anything, and never runs on crawls old enough to describe a market that
has moved.

**It states a count and stops.** "You are losing patients to them" is not
measured and is exactly the fabricated claim the validator refuses everywhere
else. The pressure comes from the number being true and checkable, not from an
adjective bolted to it -- and a clinic manager can verify that six of ten peers
book online in about a minute, which is precisely why the true sentence is the
frightening one.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from titan.intelligence.modernisation import Capability

#: How many businesses must have been read before a proportion means anything.
#:
#: Eight. "Three of the four I looked at" is noise wearing the clothes of a
#: finding, and it invites the one reply that ends the conversation: *which
#: four?* Eight is small enough to be reachable in a city Titan has worked and
#: large enough that the proportion is not an accident of who was crawled first.
MIN_COHORT = 8

#: How old the oldest crawl in the cohort may be.
#:
#: Thirty days, matching the reputation window and for a related reason: a
#: comparison built from older crawls describes a market that has moved, and
#: the recipient is the one person in the world able to check it.
MAX_COHORT_AGE = dt.timedelta(days=30)

#: The share of the cohort that must already have the capability.
#:
#: A minority is not a trend. One business in ten having something is not a
#: reason to worry about not having it, and saying so spends the strongest
#: sentence in the message on the weakest available fact. Half is the point at
#: which "most of the ones I looked at" becomes true.
#:
#: This also subsumes the zero case, and deliberately: "none of the ten I
#: looked at do this" tells the recipient they are perfectly normal, which is
#: an argument against buying. A separate guard for it was written first and
#: removed as unreachable -- a test that appeared to cover it was in fact
#: being caught here.
MIN_SHARE = 0.5

#: How each capability is described to a recipient.
#:
#: Plain verbs, because the sentence has to read as an observation rather than
#: as a product category. "Take bookings on the site" is what they would say;
#: "have self-service booking enabled" is what a vendor would say.
_PHRASING: dict[Capability, str] = {
    Capability.SELF_SERVICE_BOOKING: "take bookings on the site itself",
    Capability.CONVERSATIONAL: "have something that answers a visitor out of hours",
    Capability.MARKETING_AUTOMATION: "follow up automatically after an enquiry",
    Capability.REPUTATION_AUTOMATION: "ask for reviews automatically",
}

_NUMBER = {
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
}


@dataclass(frozen=True, slots=True)
class CohortReading:
    """What Titan's own crawls say about one capability in one market."""

    capability: Capability
    #: How the industry is named to the recipient -- "private clinics".
    industry_noun: str
    #: The market as the recipient would name it -- "Manchester".
    market: str
    #: Businesses in this cohort where the capability could be measured.
    measured: int
    #: How many of those already run it.
    present: int
    #: The oldest crawl the cohort is built from.
    oldest_crawl: dt.datetime
    #: Names, carried only so that nothing can accidentally use them. The
    #: comparison never renders a business name, and a field that exists and is
    #: deliberately unused says so more loudly than a comment.
    examples: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class Comparison:
    """One sentence, and the count it was made from."""

    sentence: str
    capability: Capability
    measured: int
    present: int


def _spell(n: int) -> str:
    return _NUMBER.get(n, str(n))


def comparison_for(
    reading: CohortReading, *, now: dt.datetime
) -> Comparison | None:
    """The sentence, or None when no honest one can be made.

    Returning None is the common case and the important one. Four separate
    floors have to clear before Titan says anything about anybody other than
    the recipient.
    """
    if reading.measured < MIN_COHORT:
        return None
    if now - reading.oldest_crawl > MAX_COHORT_AGE:
        return None
    if reading.present / reading.measured < MIN_SHARE:
        return None

    phrase = _PHRASING.get(reading.capability)
    if phrase is None:
        return None

    return Comparison(
        sentence=(
            f"{_spell(reading.present).capitalize()} of the "
            f"{_spell(reading.measured)} {reading.industry_noun} I looked at in "
            f"{reading.market} {phrase}."
        ),
        capability=reading.capability,
        measured=reading.measured,
        present=reading.present,
    )


__all__ = [
    "MAX_COHORT_AGE",
    "MIN_COHORT",
    "MIN_SHARE",
    "CohortReading",
    "Comparison",
    "comparison_for",
]
