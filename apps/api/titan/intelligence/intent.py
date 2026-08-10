"""What a human reply actually wants.

:mod:`titan.intelligence.replies` answers a narrower question -- is this a
person, a machine, a bounce, or an opt-out -- and stops there, deliberately. It
never guesses intent, so every human reply lands as ``ReplyClass.UNKNOWN``.

This module refines that one step: given a message already established as
human-written, decide whether they said yes, said no, asked a question, or
pointed at somebody else. That is what turns "a reply arrived" into "a client
agreed", which is the thing an operator wants woken up for.

**Two failure directions, both expensive.**

* Reading a rejection as agreement produces a delighted notification about a
  prospect who said no, and an operator who stops trusting the notifications.
* Reading agreement as a rejection buries the one reply that was worth the
  entire campaign.

So the rules are ordered negative-first, and *conflict resolves to UNKNOWN*
rather than to whichever pattern happened to be checked first. A reply saying
"not interested in the SEO work but very interested in the booking fix" is
genuinely ambiguous to a regex, and saying so is more useful than a coin flip
dressed up as a classification. Ambiguity still notifies -- it just notifies as
"needs reading" instead of "they agreed".

Pure, no I/O. A model may overrule any of this later and records
``decided_by='model'`` when it does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from titan.db.enums import ReplyClass

#: Ordered. The first group whose pattern matches wins, except that a positive
#: match alongside a negative one collapses to UNKNOWN -- see :func:`detect_intent`.
#:
#: Negatives come first in the list for readability, not for precedence: the
#: conflict rule makes precedence explicit rather than positional.
_PATTERNS: tuple[tuple[ReplyClass, tuple[re.Pattern[str], ...]], ...] = (
    (
        ReplyClass.NOT_INTERESTED,
        (
            re.compile(
                r"\b(not|no longer|aren'?t|isn'?t|we'?re not)\s+"
                r"(interested|keen|looking|in the market)\b",
                re.I,
            ),
            re.compile(r"\bno,?\s*thank(s| you)\b", re.I),
            # "we'll pass" is a contraction, so \s+ between the pronoun and the
            # verb never matches it -- the most common polite refusal in
            # British business email would have gone unrecognised.
            re.compile(r"\b(we|i)\s*(?:'ll| will| shall)\s+pass\b", re.I),
            re.compile(r"\bnot (something|a priority|for us)\b", re.I),
            re.compile(r"\b(please )?do ?n[o']?t (contact|email|write)\b", re.I),
        ),
    ),
    (
        # Checked before NOT_NOW so "we already have someone" is not read as a
        # deferral. It is a rejection with a reason, and the reason is the part
        # an operator can actually act on.
        ReplyClass.OBJECTION,
        (
            re.compile(
                r"\b(we|i) already (have|use|work with|_)\b|\balready (have|using|"
                r"working with) (a|an|our|someone)\b",
                re.I,
            ),
            re.compile(
                r"\b(too (expensive|costly|much)|out of (our )?budget|"
                r"no budget|can'?t afford)\b",
                re.I,
            ),
            re.compile(
                r"\b(in|we have an?) (in-?house|internal) (team|developer|agency)\b", re.I
            ),
        ),
    ),
    (
        ReplyClass.WRONG_PERSON,
        (
            re.compile(
                r"\b(not|wrong) (the )?(right )?person\b|\byou (want|need) to (speak|"
                r"talk) to\b|\bi'?ll (forward|pass) (this|it) (on|to)\b",
                re.I,
            ),
            re.compile(
                r"\b(no longer|not) (with|at) (the |this )?(company|us|firm)\b|"
                r"\b(has|have) left (the company|us)\b",
                re.I,
            ),
        ),
    ),
    (
        ReplyClass.NOT_NOW,
        (
            re.compile(
                r"\bnot (right now|at the moment|currently|this (year|quarter|month))\b",
                re.I,
            ),
            re.compile(
                r"\b(check|circle|come|get) back (with )?(to )?(us|me)?\s*"
                r"(in|after|next|later)\b",
                re.I,
            ),
            re.compile(
                r"\b(revisit|reconsider|look at (this|it) again) (in|next|later)\b", re.I
            ),
            re.compile(r"\b(bad|not a good) time\b", re.I),
        ),
    ),
    (
        ReplyClass.WANTS_CALL,
        (
            re.compile(
                r"\b(book|schedule|set ?up|arrange|jump on|hop on) a "
                r"(call|meeting|chat|time)\b",
                re.I,
            ),
            re.compile(
                r"\b(happy|glad|keen) to (talk|chat|speak|meet)\b|"
                r"\bwhen (are|were) you (free|available)\b",
                re.I,
            ),
            re.compile(
                r"\b(send|share) (me |over |through )?(a |your )?"
                r"(calendar|calendly|booking) link\b",
                re.I,
            ),
        ),
    ),
    (
        ReplyClass.WANTS_PRICING,
        (
            re.compile(
                r"\b(how much|what (does|would) (it|this) cost|what'?s the (cost|price)|"
                r"send (me |over |through )?(the |your |a )?(pricing|quote|rates?|costs?)|"
                r"ballpark|price list)\b",
                re.I,
            ),
        ),
    ),
    (
        ReplyClass.WANTS_MORE_INFO,
        (
            re.compile(
                r"\b(tell me more|more (info|information|detail)|can you (explain|"
                r"elaborate|clarify)|what (exactly )?(do|would) you (do|mean)|"
                r"send (me )?(more|some) (info|details))\b",
                re.I,
            ),
            re.compile(r"\bhow (does|would) (it|that|this) work\b", re.I),
        ),
    ),
    (
        ReplyClass.INTERESTED,
        (
            re.compile(
                r"\b(yes|yep|yeah|sure)\b.{0,40}\b(please|interested|let'?s|go ahead|"
                r"sounds|do it)\b",
                re.I | re.S,
            ),
            re.compile(
                r"\b((very|quite|definitely|certainly) )?interested\b|"
                r"\b(sounds|looks) (good|great|interesting|useful)\b|"
                r"\b(let'?s|lets) (do|go|talk|get) (it|ahead|started|this)\b|"
                r"\bcount (us|me) in\b|\bwe'?d like to (proceed|go ahead|move forward)\b",
                re.I,
            ),
            re.compile(r"\bgo ahead\b|\bplease proceed\b", re.I),
        ),
    ),
    (
        ReplyClass.REFERRAL,
        (
            re.compile(
                r"\b(speak|talk|reach out) to (my |our )?(colleague|partner|manager|"
                r"director|boss)\b|\bcopying in\b|\bcc'?ing\b",
                re.I,
            ),
        ),
    ),
)

#: Classes that mean the prospect said yes in some form. These are what
#: "a client agreed" is defined as, and what wakes an operator.
POSITIVE_CLASSES: frozenset[ReplyClass] = frozenset(
    {
        ReplyClass.INTERESTED,
        ReplyClass.WANTS_PRICING,
        ReplyClass.WANTS_CALL,
        ReplyClass.WANTS_MORE_INFO,
    }
)

#: Classes that mean the prospect said no. Recorded, never notified as urgent.
NEGATIVE_CLASSES: frozenset[ReplyClass] = frozenset(
    {
        ReplyClass.NOT_INTERESTED,
        ReplyClass.OBJECTION,
        ReplyClass.NOT_NOW,
    }
)


@dataclass(frozen=True, slots=True)
class IntentVerdict:
    reply_class: ReplyClass
    #: Which named rules fired, so a misread can be diagnosed rather than argued
    #: about. Populated even when the verdict is UNKNOWN -- especially then.
    signals: tuple[str, ...] = ()
    confidence: float = 0.0
    detail: str | None = None
    #: The sentence that produced the verdict, verbatim from the reply.
    #:
    #: A rule name tells an operator which regex fired; this tells them what the
    #: person wrote, which is the thing they actually need before answering. It
    #: is quoted, never paraphrased -- a summary of somebody's words in a CRM
    #: becomes the record of what they said, and this way the record is true.
    excerpt: str | None = None

    @property
    def is_positive(self) -> bool:
        return self.reply_class in POSITIVE_CLASSES

    @property
    def is_negative(self) -> bool:
        return self.reply_class in NEGATIVE_CLASSES

    @property
    def needs_a_human(self) -> bool:
        """Whether an operator should look at this before anything is sent.

        Everything except a clean rejection. A rejection is filed; anything a
        prospect might read as a conversation is not answered without a person
        having seen it.
        """
        return self.reply_class is not ReplyClass.NOT_INTERESTED


def detect_intent(subject: str, body: str) -> IntentVerdict:
    """Refine a human reply into what it is asking for.

    Only meaningful for messages :func:`titan.intelligence.replies.classify_reply`
    has already established are human. Running it on a bounce or an
    out-of-office would classify quoted text from the original message, which is
    Titan's own words coming back -- the most reliable way to detect enthusiasm
    for your own pitch.
    """
    haystack = _normalise(_quotable(f"{subject}\n{body}"))

    # Negatives run first and *consume what they matched*, because a negation
    # contains its own object: "not interested" holds the word "interested", and
    # a positive pass over the raw text finds it there. Without this every plain
    # rejection produces one negative and one positive signal, resolves to
    # UNKNOWN by the conflict rule, and never suppresses -- the rejection is not
    # misread as agreement, but it is not recognised as a rejection either.
    #
    # Blanking the span rather than returning early is what keeps genuine
    # ambiguity detectable: "not interested in the SEO work, but very interested
    # in the booking fix" loses only the first "interested", and the second
    # still fires.
    # Spans are recorded alongside each match so the winning rule can quote the
    # sentence it fired on. Blanking below preserves length, so an offset taken
    # from ``remaining`` still addresses the same characters in ``haystack``.
    matched: list[tuple[ReplyClass, str, tuple[int, int]]] = []
    remaining = haystack
    for reply_class, patterns in _PATTERNS:
        if reply_class not in NEGATIVE_CLASSES:
            continue
        for index, pattern in enumerate(patterns):
            found = pattern.search(remaining)
            if found:
                matched.append(
                    (reply_class, f"{reply_class.value}:{index}", found.span())
                )
                remaining = (
                    remaining[: found.start()]
                    + " " * (found.end() - found.start())
                    + remaining[found.end() :]
                )
                break

    for reply_class, patterns in _PATTERNS:
        if reply_class in NEGATIVE_CLASSES:
            continue
        for index, pattern in enumerate(patterns):
            found = pattern.search(remaining)
            if found:
                matched.append(
                    (reply_class, f"{reply_class.value}:{index}", found.span())
                )
                break

    if not matched:
        return IntentVerdict(
            ReplyClass.UNKNOWN,
            confidence=0.0,
            detail="a person replied; no intent rule matched",
        )

    classes = {reply_class for reply_class, _, _ in matched}
    signals = tuple(signal for _, signal, _ in matched)

    positive = classes & POSITIVE_CLASSES
    negative = classes & NEGATIVE_CLASSES

    if positive and negative:
        # Genuinely ambiguous: "not interested in the SEO work, but very
        # interested in the booking fix". Picking a side here would either
        # celebrate a rejection or bury a sale, and both are worse than saying
        # the message needs reading.
        return IntentVerdict(
            ReplyClass.UNKNOWN,
            signals=signals,
            confidence=0.0,
            detail="both positive and negative signals; needs a human read",
            # Quote the *positive* span, not the first match. An operator opening
            # an ambiguous reply is deciding whether there is a sale in it, and
            # the sentence that suggests there might be is the one to show them.
            excerpt=_sentence_at(haystack, _first_span(matched, POSITIVE_CLASSES)),
        )

    # Single-class matches are the confident case. Several classes on the same
    # side of the line agree on the direction, which is what drives the
    # notification, so they stay confident but slightly less certain.
    reply_class = matched[0][0]
    confidence = 0.8 if len(classes) == 1 else 0.6

    return IntentVerdict(
        reply_class,
        signals=signals,
        confidence=confidence,
        detail=f"matched {', '.join(sorted(c.value for c in classes))}",
        excerpt=_sentence_at(haystack, matched[0][2]),
    )


def _first_span(
    matched: list[tuple[ReplyClass, str, tuple[int, int]]],
    wanted: frozenset[ReplyClass],
) -> tuple[int, int] | None:
    for reply_class, _, span in matched:
        if reply_class in wanted:
            return span
    return None  # pragma: no cover - callers only ask for a class they matched


#: Where a quoted excerpt is allowed to stop. Newline included because plenty of
#: replies are a single unpunctuated line.
_SENTENCE_END = re.compile(r"[.!?\n]")

#: Long enough for a real sentence, short enough that a wall of unpunctuated
#: text does not become the CRM's record of what somebody asked for.
MAX_EXCERPT_CHARS = 300


def _sentence_at(text: str, span: tuple[int, int] | None) -> str | None:
    """The sentence containing ``span``, verbatim.

    Expands outwards to sentence boundaries rather than returning the regex
    match, because the match is a fragment ("book a call") and the sentence is
    the thing worth quoting ("Could we book a call for next week?"). Trimmed to
    a cap on either side so a paragraph with no full stops cannot turn into the
    whole message.
    """
    if span is None:
        return None
    start, end = span

    left = 0
    for boundary in _SENTENCE_END.finditer(text, 0, start):
        left = boundary.end()
    # end(), not start(): the terminator belongs to the sentence. A question
    # mark is the difference between quoting a request and quoting a statement,
    # and a trailing newline is stripped below anyway.
    right_match = _SENTENCE_END.search(text, end)
    right = right_match.end() if right_match else len(text)

    left = max(left, start - MAX_EXCERPT_CHARS)
    right = min(right, end + MAX_EXCERPT_CHARS)
    return text[left:right].strip() or None


#: Typographic characters mail clients substitute for the plain ASCII ones, and
#: what to read them as. Applied to every message before the patterns run.
#:
#: Outlook and iOS autocorrect a straight apostrophe to a curly one as you type,
#: so a rule written for ``don't`` silently stops matching the moment a real
#: person sends it from a real mail client -- and the patterns here are dense
#: with contractions: don't, we're, let's, can't, I'll, aren't. Normalising once
#: at the input is the difference between rules that work in a test file and
#: rules that work on mail.
#:
#: The dashes matter for the same reason: "Not right now - maybe next year"
#: arrives with an em dash after autocorrect.
#: Written as escapes, not literals: these characters are visually
#: indistinguishable from their ASCII counterparts in most editors, which is
#: exactly the property that would make a typo here impossible to spot in
#: review -- and a wrong entry would silently disable a rule rather than fail.
_SUBSTITUTIONS = str.maketrans(
    {
        "\u2018": "'",  # left single quotation mark
        "\u2019": "'",  # right single quotation mark: the smart apostrophe
        "\u201c": '"',  # left double quotation mark
        "\u201d": '"',  # right double quotation mark
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u00a0": " ",  # non-breaking space
    }
)


def _normalise(text: str) -> str:
    return text.translate(_SUBSTITUTIONS)


_QUOTE_MARKERS = (
    re.compile(r"^\s*>", re.M),
    re.compile(r"^\s*on .{0,80}wrote:\s*$", re.I | re.M),
    re.compile(r"^\s*-{2,}\s*original message\s*-{2,}\s*$", re.I | re.M),
    re.compile(r"^\s*_{5,}\s*$", re.M),
    re.compile(r"^\s*from:\s.+$", re.I | re.M),
)


def _quotable(text: str) -> str:
    """Drop the quoted original from a reply before reading intent.

    Titan's own message is full of the language these rules look for -- it was
    written to be persuasive. Leaving it in means every reply, including "no
    thanks", carries a glowing pitch underneath it, and the classifier reads
    Titan's enthusiasm as the prospect's.

    Cuts at the earliest quote marker rather than trying to parse the thread.
    Over-trimming costs a little context; under-trimming corrupts the verdict.
    """
    earliest = len(text)
    for marker in _QUOTE_MARKERS:
        found = marker.search(text)
        if found and found.start() < earliest:
            earliest = found.start()
    trimmed = text[:earliest].strip()
    # A reply that is *only* quoted text (a forward, or a client whose mail
    # program top-quotes everything) leaves nothing to read. Fall back to the
    # whole thing rather than returning an empty verdict on a real message.
    return trimmed or text


__all__ = [
    "MAX_EXCERPT_CHARS",
    "NEGATIVE_CLASSES",
    "POSITIVE_CLASSES",
    "IntentVerdict",
    "detect_intent",
]
