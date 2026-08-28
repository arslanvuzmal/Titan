"""One canonical, currently-sendable message, for every fixture that needs one.

Three separate fixtures used to carry their own hand-written body -- the
validator suite, the delivery conftest, and the redraft suite. Every change to
the message shape or the word band broke all three, one test run at a time,
and each was repaired by hand into a slightly different shape. The bodies then
disagreed with each other about what a good message looked like.

This is the single source. A fixture that needs a message that passes today's
rules asks for one here; when the rules change, this file changes and the
fixtures follow.

Nothing here is generated from :mod:`titan.intelligence.composer`. A fixture
built by the code under test cannot catch that code writing something the
validator refuses -- which is the entire point of the drafting tests.
"""

from __future__ import annotations

#: Paragraphs that assert something about the recipient's own pages. Each has
#: to appear in a claim map, sentence by sentence, or the validator is right to
#: refuse the message.
CLAIMED_PARAGRAPHS: tuple[str, ...] = (
    'I was looking at bellrose-dental.test and noticed the "Book an '
    'appointment" button on your homepage opens a page that returns a 404, so '
    "anyone who clicks it cannot get through to your booking form.",
    "The button itself is fine -- it is the address behind it that no longer "
    "exists, so the server answers with a not-found page instead of the form. "
    "That usually happens after a page is renamed and the old link is left "
    "pointing at where it used to be.",
    "That is worth fixing because someone clicking there has already moved "
    "past browsing treatments and is actively trying to book.",
    "The repair is to point the button at the page that exists now and put a "
    "redirect on the old address so anything still linking to it keeps "
    "working. Then a sweep of the rest of the site for the same pattern, "
    "because a rename rarely breaks only one link.",
)

#: Paragraphs that say nothing about this recipient, and so need no evidence:
#: how people behave in general, who the sender is, and the ask.
UNCLAIMED_PARAGRAPHS: tuple[str, ...] = (
    "Most people now look a business up online before they ever pick up the "
    "phone, so first impressions are made here -- and if it fails at that "
    "point, they move on to the next result instead of calling.",
    "I build and repair patient-booking flows. You can see how I approach "
    "this kind of work on my portfolio ({portfolio}).",
    "If that sounds worth doing, we could book twenty minutes and I will take "
    "you through it properly.",
)


def paragraphs(*, portfolio: str) -> list[str]:
    """The message body's paragraphs, in the order they are sent."""
    claimed = list(CLAIMED_PARAGRAPHS)
    context, credential, ask = UNCLAIMED_PARAGRAPHS
    return [
        claimed[0],  # what was found
        claimed[1],  # what is mechanically wrong
        claimed[2],  # what it costs, in trade language
        context,  # why that bites now
        claimed[3],  # what the repair involves
        credential.format(portfolio=portfolio),
        ask,
    ]


def body(
    *,
    owner: str = "Arslan Vuzmal Lone",
    portfolio: str = "https://arslanvuzmallone.dev",
    address: str = "12 Fictional Row, Testville, TE1 1ST",
    unsubscribe: str = "https://arslanvuzmallone.dev/unsubscribe?t=abc",
    greeting: str = "Hi there",
) -> str:
    """A complete body that passes today's rules.

    The signature carries no portfolio link: it was already spent in the
    credential paragraph, and two links is one more than the rules allow.
    """
    parts = "\n\n".join(paragraphs(portfolio=portfolio))
    return f"{greeting},\n\n{parts}\n\n{owner}\n{address}\nUnsubscribe ({unsubscribe})\n"


def claimed_text(*, portfolio: str = "https://arslanvuzmallone.dev") -> str:
    """Just the paragraphs a claim map has to cover, joined for splitting."""
    return "\n\n".join(CLAIMED_PARAGRAPHS)


__all__ = [
    "CLAIMED_PARAGRAPHS",
    "UNCLAIMED_PARAGRAPHS",
    "body",
    "claimed_text",
    "paragraphs",
]
