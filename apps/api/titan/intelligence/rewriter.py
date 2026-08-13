"""Letting a model phrase the message without letting it decide what is true.

:mod:`titan.intelligence.composer` builds a message and its claim map in the
same expression, so the two cannot drift, and its docstring names the condition
under which a model may join: *"the moment its output can be held to the same
claim-map contract this becomes its post-processor rather than its
replacement."* This is that post-processor.

**Sentence by sentence, never whole-message.** The model is handed one factual
sentence and the claim it must preserve, and returns one sentence. The claim
map entry is then updated in lockstep with the text it describes -- the same
discipline the composer uses, for the same reason. Asking for a whole message
would mean the model also produced the claim map, and the validator would be
checking the model's own account of what it had asserted, which is not a check.

**Three things make a rewrite unusable, and all three fall back silently.**

* It drops an evidenced specific. A sentence that no longer names the domain,
  or no longer carries the observed value, has stopped being about the thing
  the evidence supports.
* It adds a sentence. One in, one out: a second sentence has no claim-map entry
  and would either be rejected downstream or, worse, read as evidenced.
* It fails the validator once reassembled.

Falling back is not a degraded mode. The deterministic text is the text this
system sent for its entire life before now, it passes every rule, and it is
what a rewrite has to beat rather than merely differ from.

**The input is untrusted.** A finding's ``observed_value`` and ``title`` are
derived from the prospect's own page, so a page can contain text written to be
read as an instruction. It travels in the bundle's untrusted channel, inside a
nonce fence, and the output is treated as hostile regardless: every check below
runs on what came back, not on what was asked for.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from titan.intelligence.composer import ComposedMessage
from titan.intelligence.message_validator import prohibited_content

logger = logging.getLogger(__name__)

#: The model may not lengthen a sentence without limit. Cold email dies of
#: length, and an unbounded rewrite is also the cheapest way for an injected
#: instruction to smuggle a paragraph in.
MAX_SENTENCE_GROWTH = 1.6

#: Below this the rewrite has thrown away rather than rephrased.
MIN_SENTENCE_CHARS = 20

#: What a rewritten sentence must still contain, drawn from the claim it
#: describes. Matching is case-insensitive and punctuation-tolerant, because a
#: rewrite is allowed to move a domain from mid-sentence to the front.
_TOKEN_SPLIT = re.compile(r"[\s,;:!?]+")


class RewriteRefusal(StrEnum):
    """Why a candidate was rejected. Counted, so a bad prompt is visible."""

    DROPPED_SPECIFIC = "dropped_specific"
    ADDED_SENTENCE = "added_sentence"
    TOO_LONG = "too_long"
    TOO_SHORT = "too_short"
    EMPTY = "empty"
    UNCHANGED = "unchanged"
    #: Said something no message may say -- a fabricated metric, manufactured
    #: urgency, flattery. Judged by the validator's own detectors, so there is
    #: one definition of prohibited rather than two that drift.
    PROHIBITED_CONTENT = "prohibited_content"


@dataclass(frozen=True, slots=True)
class SentenceRewrite:
    """One factual sentence, before and after."""

    original: str
    candidate: str
    #: Strings the claim requires the sentence to keep. Usually the domain and
    #: the observed value.
    required: tuple[str, ...] = ()
    refusal: RewriteRefusal | None = None

    @property
    def accepted(self) -> bool:
        return self.refusal is None and bool(self.candidate.strip())


@dataclass(frozen=True, slots=True)
class RewriteOutcome:
    """What the rewrite produced, and whether it was used."""

    message: ComposedMessage
    #: False when the deterministic text was kept, for any reason.
    rewritten: bool = False
    sentences_rewritten: int = 0
    refusals: tuple[str, ...] = field(default_factory=tuple)
    detail: str | None = None


def required_specifics(claim: dict[str, Any], domain: str) -> tuple[str, ...]:
    """What a rewrite of this sentence must still say.

    Deliberately small. Requiring every noun would reject any real rephrasing;
    requiring nothing would accept "your website has an issue", which is true
    of every website and evidence for nothing. The domain and the observed
    value are the two things that make the sentence about *this* business.
    """
    specifics: list[str] = [domain] if domain else []
    observed = str(claim.get("observed_value") or "").strip()
    # Only when it is short enough to be a phrase rather than a paragraph: a
    # multi-line console error cannot survive a rewrite verbatim and should not
    # be required to.
    if observed and len(observed) <= 60:
        specifics.append(observed)
    return tuple(dict.fromkeys(s for s in specifics if s))


def check_candidate(
    original: str, candidate: str, required: tuple[str, ...]
) -> RewriteRefusal | None:
    """Whether a rewritten sentence may replace the original."""
    text = (candidate or "").strip()
    if not text:
        return RewriteRefusal.EMPTY
    if len(text) < MIN_SENTENCE_CHARS:
        return RewriteRefusal.TOO_SHORT
    if len(text) > max(len(original) * MAX_SENTENCE_GROWTH, MIN_SENTENCE_CHARS * 2):
        return RewriteRefusal.TOO_LONG

    # One sentence in, one out. Counted on terminators that end a sentence
    # rather than on any full stop, so "co.uk" and "e.g." do not read as two.
    if len(_sentence_terminators(text)) > 1:
        return RewriteRefusal.ADDED_SENTENCE

    lowered = text.lower()
    for specific in required:
        if specific.lower() not in lowered:
            return RewriteRefusal.DROPPED_SPECIFIC

    # The validator's own detectors, on the sentence rather than the assembled
    # message. Running them here is what makes a bad rewrite fall back to the
    # deterministic text instead of failing the whole draft: by the time
    # validate_message sees it, the good version has been thrown away.
    if prohibited_content(text) is not None:
        return RewriteRefusal.PROHIBITED_CONTENT

    if _normalize(text) == _normalize(original):
        return RewriteRefusal.UNCHANGED
    return None


def apply_rewrites(
    message: ComposedMessage, rewrites: list[SentenceRewrite]
) -> RewriteOutcome:
    """Substitute accepted sentences into the body *and* the claim map.

    Both, in one pass, because updating the body alone is precisely the drift
    the composer builds its claim map inline to avoid: the map would describe
    sentences that are no longer in the message, the validator would find a
    claim-bearing sentence it cannot trace, and the draft would be rejected for
    a reason that looks like a content problem.
    """
    accepted = [r for r in rewrites if r.accepted]
    refusals = tuple(r.refusal.value for r in rewrites if r.refusal is not None)
    if not accepted:
        return RewriteOutcome(
            message=message,
            rewritten=False,
            refusals=refusals,
            detail="no candidate survived the checks; kept the deterministic text",
        )

    body = message.body
    claim_map = [dict(entry) for entry in message.claim_map]

    for rewrite in accepted:
        if rewrite.original not in body:
            # The sentence the model was given is not in the body any more,
            # which means an earlier substitution overlapped it. Skip rather
            # than guess: a partial application is worse than none.
            continue
        body = body.replace(rewrite.original, rewrite.candidate, 1)
        for entry in claim_map:
            if str(entry.get("sentence", "")).strip() == rewrite.original.strip():
                entry["sentence"] = rewrite.candidate

    return RewriteOutcome(
        message=ComposedMessage(
            subject=message.subject,
            body=body,
            claim_map=claim_map,
            variant=f"{message.variant}+model" if message.variant else "model",
        ),
        rewritten=True,
        sentences_rewritten=len(accepted),
        refusals=refusals,
    )


def _sentence_terminators(text: str) -> list[str]:
    """Sentence-ending punctuation, ignoring abbreviations and domains."""
    return re.findall(r"[.!?](?=\s+[A-Z]|\s*$)", text.strip())


def _normalize(text: str) -> str:
    return " ".join(_TOKEN_SPLIT.split(text.strip().lower())).strip(" .")


__all__ = [
    "MAX_SENTENCE_GROWTH",
    "MIN_SENTENCE_CHARS",
    "RewriteOutcome",
    "RewriteRefusal",
    "SentenceRewrite",
    "apply_rewrites",
    "check_candidate",
    "required_specifics",
    "rewrite_message",
]


# ==========================================================================
# Calling the model
# ==========================================================================
#: What the model is told it is for. Narrow on purpose: a system prompt that
#: describes the whole product invites the model to help with the whole product.
_SYSTEM = (
    "You rephrase a single sentence from a business email. You are given the "
    "sentence and the evidence it is based on. Return one sentence that says "
    "the same thing in more natural English."
)

_POLICY = (
    "RULES, in order of importance:\n"
    "1. Do not add any fact that is not in the evidence. No numbers, no "
    "   percentages, no claims about the recipient's revenue, traffic or "
    "   competitors, no guesses about their business.\n"
    "2. Keep every specific listed in 'must_keep' exactly as written.\n"
    "3. Return exactly one sentence. Never two.\n"
    "4. Do not flatter, do not manufacture urgency, do not open with a "
    "   pleasantry. Plainness is the point.\n"
    "5. Do not use the words 'unfortunately', 'exciting', 'leverage', "
    "   'synergy', or 'I hope this finds you well'.\n"
    "If you cannot obey every rule, return the original sentence unchanged."
)


class _Rephrased(BaseModel):
    """The response schema. One field, so there is nothing else to fill in."""

    sentence: str


async def rewrite_message(
    message: ComposedMessage,
    *,
    gateway: Any,
    domain: str,
    observed_value: str | None = None,
    source_url: str | None = None,
    campaign_id: str | None = None,
    lead_id: str | None = None,
) -> RewriteOutcome:
    """Ask a model to rephrase each factual sentence, and keep what survives.

    Never raises. A model that is down, over budget, or returning nonsense
    produces the deterministic message and a reason -- because the alternative
    is a pipeline whose drafting stage fails when a third party has a bad
    minute, for a step that is an improvement rather than a requirement.
    """
    from titan.db.enums import ModelTask
    from titan.models.channels import PromptBundle, UntrustedBlock

    rewrites: list[SentenceRewrite] = []
    for entry in message.claim_map:
        original = str(entry.get("sentence", "")).strip()
        if not original:
            continue
        claim = {**entry, "observed_value": observed_value}
        required = required_specifics(claim, domain)

        bundle = PromptBundle(
            system=_SYSTEM,
            policy=_POLICY,
            evidence=[
                {
                    "claim": entry.get("claim"),
                    "source_url": entry.get("source_url"),
                    "must_keep": list(required),
                }
            ],
            # The sentence is built from the prospect's own page: a title and an
            # observed value that a site can choose. It travels as data.
            untrusted=[
                UntrustedBlock(
                    label="sentence_to_rephrase",
                    content=original,
                    source_url=source_url,
                )
            ],
            task=(
                "Rephrase the sentence in the untrusted block. Return JSON with "
                "one key, 'sentence'."
            ),
        )

        try:
            parsed, _ = await gateway.complete_typed(
                ModelTask.MESSAGE,
                _Rephrased,
                bundle,
                campaign_id=campaign_id,
                lead_id=lead_id,
                max_tokens=200,
                temperature=0.4,
            )
        except Exception as exc:
            logger.info(
                "sentence rewrite unavailable; keeping the deterministic text",
                extra={"error_code": type(exc).__name__},
            )
            return RewriteOutcome(
                message=message,
                rewritten=False,
                detail=f"model unavailable: {type(exc).__name__}",
            )

        candidate = (parsed.sentence or "").strip()
        rewrites.append(
            SentenceRewrite(
                original=original,
                candidate=candidate,
                required=required,
                refusal=check_candidate(original, candidate, required),
            )
        )

    return apply_rewrites(message, rewrites)
