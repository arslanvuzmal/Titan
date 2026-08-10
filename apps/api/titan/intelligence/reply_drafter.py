"""Answering a reply.

``reply_classifications.suggested_reply_draft_id`` has existed since the first
migration with nothing to populate it. This populates it.

**Nothing here is ever sent.** The drafter produces a suggestion attached to the
inbound message; a person reads it, edits it, and sends it. That is a deliberate
line, not a missing feature: a first message is written from evidence a crawler
gathered, but a *reply* is written into a conversation, and a conversation is
where an automated system can commit its owner to a price, a deadline or a scope
nobody agreed to.

Two rules keep the suggestions honest.

**It never invents a commercial fact.** A prospect asking "how much?" gets a
draft with the number left as a marked blank, because Titan does not know what
this job is worth and a plausible-looking figure is the most expensive kind of
fabrication. Those drafts fail validation on the unfilled placeholder, which
means :func:`titan.activities.pipeline.queue_message` refuses them -- the draft
physically cannot be sent until a human has put a real number in it. The
validator doing that is the point, not an inconvenience to work around.

**It does not answer a rejection.** Somebody who said "not interested" is
suppressed and gets no draft at all. Writing one would put a ready-to-send
message in front of an operator at the exact moment the right action is silence.
"""

from __future__ import annotations

from dataclasses import dataclass

from titan.db.enums import ReplyClass

#: Marks a spot only a person can fill. Chosen to match the validator's
#: placeholder detection, so a draft carrying one cannot pass validation and
#: therefore cannot be queued.
BLANK = "[TODO: {}]"


@dataclass(frozen=True, slots=True)
class ReplyDraftContext:
    reply_class: ReplyClass
    #: Subject of the message they replied to, for the Re: line.
    original_subject: str
    owner_name: str
    #: Published first name of whoever wrote in, when one is known. Never
    #: inferred from the email local part -- "sam@" might be a shared mailbox.
    contact_first_name: str | None = None
    #: What the original message offered, as a noun phrase.
    solution: str = "the work"
    booking_url: str | None = None


@dataclass(frozen=True, slots=True)
class ReplyDraft:
    subject: str
    body: str
    #: True when the draft is complete enough for a person to send as-is. False
    #: means it carries a blank only they can fill, and validation will refuse
    #: it until they do.
    ready_to_send: bool
    #: Why this draft says what it says, for the operator reading it cold.
    rationale: str


def draft_reply(ctx: ReplyDraftContext) -> ReplyDraft | None:
    """Suggest a response. None when the right response is none.

    The reply classes are handled individually rather than through one template
    because what each one needs is genuinely different: a pricing question needs
    a number, an objection needs an answer, a wrong-person reply needs a
    redirect, and a rejection needs nothing at all.
    """
    builder = _BUILDERS.get(ctx.reply_class)
    if builder is None:
        return None

    greeting = f"Hi {ctx.contact_first_name}" if ctx.contact_first_name else "Hi"
    subject = ctx.original_subject
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    body, ready, rationale = builder(ctx)
    return ReplyDraft(
        subject=subject[:200],
        body=f"{greeting},\n\n{body}\n\n{ctx.owner_name}\n",
        ready_to_send=ready,
        rationale=rationale,
    )


# --------------------------------------------------------------------------
# Per-class builders. Each returns (body, ready_to_send, rationale).
# --------------------------------------------------------------------------


def _interested(ctx: ReplyDraftContext) -> tuple[str, bool, str]:
    # No re-pitching. They already said yes; the only job left is to make the
    # next step effortless, and another paragraph of benefits can only lose it.
    next_step = (
        f"You can grab a time here: {ctx.booking_url}"
        if ctx.booking_url
        else "Let me know a couple of times that suit and I will send an invite."
    )
    return (
        f"Glad it was useful. {next_step}\n\n"
        "Before we talk it would help to know roughly how many enquiries you "
        "get in a normal week -- it changes what I would suggest.",
        True,
        "They agreed. Makes the next step concrete and asks one question that "
        "improves the call without gating it.",
    )


def _wants_pricing(ctx: ReplyDraftContext) -> tuple[str, bool, str]:
    return (
        f"Happy to give you a number. For {ctx.solution} on a site this size it "
        f"is usually {BLANK.format('price or range')}, depending on "
        f"{BLANK.format('what it depends on')}.\n\n"
        "If that is in the right region I can put together a fixed quote once I "
        "have seen the site properly.",
        False,
        "They asked what it costs. The figure is left blank on purpose: Titan "
        "has not seen enough to price this, and an invented number is the most "
        "expensive thing it could put in writing. Validation will refuse this "
        "draft until a real number replaces the blank.",
    )


def _wants_call(ctx: ReplyDraftContext) -> tuple[str, bool, str]:
    next_step = (
        f"Here is my calendar: {ctx.booking_url}"
        if ctx.booking_url
        else "Send me two or three times that work and I will confirm one."
    )
    return (
        f"Yes, happy to. {next_step}\n\n"
        "Twenty minutes should be plenty -- I will walk through what I found "
        "and what I would do about it.",
        True,
        "They asked to talk. Confirms, gives one way to book, and sets a short "
        "duration so agreeing costs them little.",
    )


def _wants_more_info(ctx: ReplyDraftContext) -> tuple[str, bool, str]:
    return (
        f"Of course. {BLANK.format('answer their specific question')}\n\n"
        "The short version of what I do: I find the places where a site loses "
        "people who were ready to get in touch, and I fix them. Usually that is "
        "a broken form, a dead button, or a page that does not work on a phone.\n\n"
        "Anything specific you want me to look at?",
        False,
        "They asked a question. The blank is where their actual question gets "
        "answered -- Titan has the message but cannot reliably know which of "
        "several things they meant, and a generic answer to a specific question "
        "reads worse than no reply.",
    )


def _objection(ctx: ReplyDraftContext) -> tuple[str, bool, str]:
    return (
        f"Understood. {BLANK.format('respond to their specific objection')}\n\n"
        "If it is useful, I am happy to send over what I found either way -- no "
        "obligation, and you can hand it to whoever looks after the site.",
        False,
        "They raised a specific objection -- budget, an existing agency, an "
        "in-house team. Answering it generically dismisses it. The fallback "
        "offer costs nothing and leaves the door open without pressing.",
    )


def _not_now(ctx: ReplyDraftContext) -> tuple[str, bool, str]:
    return (
        "No problem at all -- thanks for letting me know.\n\n"
        f"I will make a note to check back {BLANK.format('when they said')}. "
        "In the meantime I will send over what I found, so you have it whenever "
        "you get to it.",
        False,
        "A deferral is the warmest part of the pipeline and should be treated "
        "as a yes with a date on it. The blank is their timing, which they "
        "usually state and which is worth getting right.",
    )


def _wrong_person(ctx: ReplyDraftContext) -> tuple[str, bool, str]:
    return (
        "Apologies for the misdirect, and thanks for pointing me the right way.\n\n"
        "If you can pass it on, that is great -- otherwise send me a name and I "
        "will go direct rather than bother you again.",
        True,
        "They are not the right contact. Apologises briefly, gives them the "
        "easier of two options, and commits to not writing to them again.",
    )


def _referral(ctx: ReplyDraftContext) -> tuple[str, bool, str]:
    return (
        "Thanks -- I will follow up with them directly.\n\n"
        "Happy to copy you in or leave you out of it, whichever you prefer.",
        True,
        "They pointed at a colleague. Acknowledges and hands them control of "
        "whether they stay in the thread.",
    )


def _unknown(ctx: ReplyDraftContext) -> tuple[str, bool, str]:
    return (
        f"{BLANK.format('read their reply and answer it')}\n\n"
        "Let me know if it would be easier to talk it through.",
        False,
        "The intent rules could not read this one, or read it two ways at once. "
        "The draft is a scaffold, not a suggestion: guessing at an ambiguous "
        "reply is how a warm lead gets an answer to a question they did not ask.",
    )


#: NOT_INTERESTED is deliberately absent. A rejection is answered with silence,
#: and putting a ready-to-send message in front of an operator at that moment
#: invites exactly the reply that turns a polite no into a spam complaint. The
#: address is suppressed on that path anyway.
_BUILDERS = {
    ReplyClass.INTERESTED: _interested,
    ReplyClass.WANTS_PRICING: _wants_pricing,
    ReplyClass.WANTS_CALL: _wants_call,
    ReplyClass.WANTS_MORE_INFO: _wants_more_info,
    ReplyClass.OBJECTION: _objection,
    ReplyClass.NOT_NOW: _not_now,
    ReplyClass.WRONG_PERSON: _wrong_person,
    ReplyClass.REFERRAL: _referral,
    ReplyClass.UNKNOWN: _unknown,
}


__all__ = ["BLANK", "ReplyDraft", "ReplyDraftContext", "draft_reply"]
