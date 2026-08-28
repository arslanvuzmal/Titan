"""The salutation, resolved against the recipient's clock rather than ours.

A cold message opens with a greeting, and a greeting that names a time of day
is either a small courtesy or a small tell. "Good morning" arriving at four in
the afternoon is the second one -- it says the sender was not present when it
was sent, which is the single thing a cold approach cannot afford to advertise.

So the greeting is decided **at send time, in the recipient's timezone**, and
not when the draft is written. Those are different moments: a draft composed on
Monday can sit in the outbox until Thursday, and the send window that finally
releases it spans 08:00 to 17:00 local -- morning and afternoon both. Choosing
the word at compose time would be choosing it for a clock nobody will be
reading it on.

The rewrite is deliberately conservative. It replaces a salutation it
recognises and otherwise leaves the body exactly as composed, because the
failure it is guarding against is not a stale greeting -- it is a mangled first
line, which is worse than any wording this module could pick.
"""

from __future__ import annotations

import datetime as dt
import re

#: Local-hour boundaries, half-open, on the recipient's clock.
#:
#: Chosen to be defensible rather than precise. The awkward edges are the ends
#: of the day: "good evening" at 17:30 is normal in British English and early
#: in American, and "good morning" at 05:00 is a message from somebody who has
#: not slept. Both extremes fall back to the neutral form instead, which is
#: never wrong in any market -- and every send window Titan runs sits inside
#: 07:00-18:00 local anyway, so the neutral branches are reached mainly by
#: messages that took an unusual path to the wire.
MORNING_FROM = 5
AFTERNOON_FROM = 12
EVENING_FROM = 18
NIGHT_FROM = 22

#: The form used when the local hour is unknown or outside civil hours.
#:
#: "Hi there" was the old unconditional opener and stays the floor: it is warm,
#: it names no time, and it is what the corpus of already-sent messages used.
NEUTRAL = "Hi there"
NEUTRAL_NAMED = "Hi"

#: Salutations this module is willing to overwrite.
#:
#: An allow-list, not a general first-line matcher. A body whose opening line
#: is not one of these is left alone -- the alternative is a regex that one day
#: eats a sentence and sends a message beginning mid-clause.
_SALUTATIONS = (
    "good morning",
    "good afternoon",
    "good evening",
    "hi there",
    "hello there",
    "hi",
    "hello",
    "dear",
)

#: The opening line: a known salutation, an optional name, then a comma.
#:
#: Anchored at the start of the body and bounded to one line. The name group is
#: kept verbatim and re-emitted, so a rewrite never changes who is being
#: addressed -- only the words in front of them.
_OPENER = re.compile(
    r"^(?P<salutation>" + "|".join(_SALUTATIONS) + r")"
    r"(?P<name>[^\S\n]+[^\n,]+?)?"
    r"(?P<punctuation>,|!)?"
    r"(?=\n|$)",
    re.IGNORECASE,
)


def greeting_for_hour(hour: int | None, *, name: str | None = None) -> str:
    """The salutation for a local hour, with a name when one is known.

    ``None`` -- an unresolvable timezone -- returns the neutral form. That is
    the same answer an unknown hour has always produced, and it is the reason
    this can be applied unconditionally: there is no input for which it has to
    refuse.
    """
    # Narrowed once, into a local: `bool(name and name.strip())` tells a
    # reader that `name` is present but tells the type checker nothing, so the
    # `name.strip()` in the return read as `str | None`.
    trimmed = (name or "").strip()
    if hour is None or not (MORNING_FROM <= hour < NIGHT_FROM):
        base = NEUTRAL_NAMED if trimmed else NEUTRAL
    elif hour < AFTERNOON_FROM:
        base = "Good morning"
    elif hour < EVENING_FROM:
        base = "Good afternoon"
    else:
        base = "Good evening"
    return f"{base} {trimmed}" if trimmed else base


def greeting_at(local: dt.datetime | None, *, name: str | None = None) -> str:
    """``greeting_for_hour`` for a local datetime."""
    return greeting_for_hour(None if local is None else local.hour, name=name)


def retimed_pair(body: str, local: dt.datetime | None) -> tuple[str, str] | None:
    """``(existing salutation, replacement)``, or ``None`` to leave it alone.

    Exposed separately from ``retime_greeting`` because the message has two
    bodies. The plain-text part can be rewritten from the front, but the HTML
    part opens with markup and the salutation sits inside the first paragraph
    -- so the caller needs the exact pair of strings to substitute rather than
    a rewritten body. Returning the pair keeps one implementation of *which*
    salutation is correct, instead of a second regex that walks HTML.

    ``None`` covers every uncertain case: no body, no resolvable local time, an
    opening line this module does not recognise, or a salutation that is
    already the right one. Callers may apply the result unconditionally.
    """
    if not body or local is None:
        return None
    match = _OPENER.match(body)
    if match is None:
        return None
    existing = body[match.start() : match.end()]
    name = match.group("name")
    replacement = greeting_for_hour(local.hour, name=name)
    punctuation = match.group("punctuation") or ""
    replacement += punctuation
    if replacement == existing:
        return None
    return existing, replacement


def retime_greeting(body: str, local: dt.datetime | None) -> str:
    """Rewrite the opening salutation of ``body`` for ``local``.

    Idempotent, and a no-op in every case it is not certain about: an empty
    body, an opening line that is not a recognised salutation, or a local time
    that could not be resolved. The name already in the line is preserved
    exactly -- this decides the words before it and nothing else.

    Returns the body unchanged rather than raising, because the caller is the
    outbox worker holding a message that has already passed the policy engine.
    A greeting is not worth failing a send over.
    """
    pair = retimed_pair(body, local)
    if pair is None:
        return body
    existing, replacement = pair
    return replacement + body[len(existing) :]


__all__ = [
    "AFTERNOON_FROM",
    "EVENING_FROM",
    "MORNING_FROM",
    "NEUTRAL",
    "NIGHT_FROM",
    "greeting_at",
    "greeting_for_hour",
    "retime_greeting",
    "retimed_pair",
]
