"""Work the sender has actually done, chosen to match the work being proposed.

The credibility paragraph used to be a sentence about what the sender works on.
That is a category, and a category is what everybody claims. What earns a reply
is a specific previous job the reader recognises as their own situation: the
same industry, or the same defect, ideally both.

**Nothing in here is invented, and that is enforced by it being empty.**

This module ships with no case studies. It is a registry with a loader, and it
resolves to ``None`` until a human writes real projects into the file it reads.
When it resolves to ``None`` the composer keeps the generic credential sentence
it has always used -- a weaker paragraph, and a true one.

That decision is not fastidiousness. A fabricated case study in a cold email is
a false statement about a third party made for commercial gain, which is the
one category of content in this system that cannot be walked back by an
apology: the recipient can check, the named client can sue, and every genuine
claim in the message is retrospectively worthless. There is no version of this
module that guesses.

Entries come in two shapes. **A system you built** is the stronger one,
because the reader can open it::

    {
      "reference": "voxcircuit",
      "name": "VoxCircuit",
      "summary": "a voice and scheduling platform that answers enquiry calls",
      "url": "https://arslanvuzmallone.com/projects/voxdesk-ai",
      "families": ["conversion"],
      "issue_types": ["no_visible_phone_number"]
    }

**A client engagement** is the other, for work done for somebody else::

    {
      "reference": "dental-booking-2025",
      "descriptor": "a two-site dental practice in Leeds",
      "work": "rebuilt a booking flow after a site migration broke it",
      "outcome": "enquiries arrived again the same week",
      "industries": ["dentist"],
      "families": ["technical"]
    }

``descriptor`` may be anonymous -- "a two-site dental practice in Leeds" is
both truthful and safe where a named client has not consented to being a
reference. Naming a client who has not agreed to it is its own problem, so the
anonymous form is the default this documentation shows.

Note the indefinite article in ``work``: "rebuilt **a** booking flow", not
"the". An entry whose rendered sentence pairs an article with a site noun reads
to the claim validator as an assertion about the *recipient's* booking flow,
and is rejected at load time with a message saying so. See ``_parse``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from titan.intelligence.message_validator import reads_as_recipient_claim

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CaseStudy:
    """One previous job, and what it is evidence of."""

    #: Stable id, stamped into the draft so a reply rate is attributable to the
    #: case study that was shown rather than to the offer in general.
    reference: str
    #: The system's name, when this is something the sender built themselves --
    #: "VoxCircuit". Carries the link in the rendered message.
    name: str | None = None
    #: What that system does, as a noun phrase continuing "I built X, ...".
    #: Present tense: the thing exists and can be looked at.
    summary: str | None = None
    #: How the client is described, when this is a client engagement instead.
    #: Anonymous is fine; false is not. Rendered verbatim to a stranger.
    descriptor: str | None = None
    #: What was done for them. A clause continuing "I recently ...".
    work: str | None = None
    #: What happened afterwards. Optional, because "we do not have a clean
    #: number for that one" is a normal state of affairs and an outcome
    #: invented to fill the field is the exact failure this module exists to
    #: prevent.
    outcome: str | None = None
    #: Industries this is relevant to, matched against the lead's.
    industries: frozenset[str] = field(default_factory=frozenset)
    #: Quality families (``speed``, ``accessibility``, ``search``,
    #: ``technical``, ``conversion``) this job is evidence of competence in.
    families: frozenset[str] = field(default_factory=frozenset)
    #: Specific issue types, when the job was precisely this defect.
    issue_types: frozenset[str] = field(default_factory=frozenset)
    #: The page a reader can go and look at. Required for a built system --
    #: the whole advantage of citing one is that it is inspectable -- and
    #: optional for a client engagement, where there may be nothing public.
    url: str | None = None

    @property
    def is_own_system(self) -> bool:
        return bool(self.name and self.summary)

    def sentence(self) -> str:
        """The paragraph, as the recipient reads it.

        Two shapes, because there are two kinds of evidence. A system the
        sender built is named and described in the present tense, because it
        exists and the reader can open it. A client engagement is past tense
        and describes work done for somebody else.
        """
        if self.is_own_system:
            return f"I built {self.name}, {self.summary}."
        opening = f"I recently {self.work} for {self.descriptor}"
        if self.outcome:
            return f"{opening}; {self.outcome}."
        return f"{opening}."

    def link_text(self) -> str | None:
        """The phrase in ``sentence()`` that should carry the link.

        The system's name, so the anchor sits on "VoxCircuit" rather than being
        appended as a bare URL after the full stop. None when there is nothing
        to link or no name to hang it on.
        """
        if self.url and self.name:
            return self.name
        return None


def _as_set(value: Any) -> frozenset[str]:
    if not value:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value.strip().lower()})
    return frozenset(str(item).strip().lower() for item in value if str(item).strip())


def _parse(entry: dict[str, Any]) -> CaseStudy | None:
    """One JSON object into a case study, or ``None`` if it is unusable.

    Skipped rather than raised: one malformed entry in an operator-edited file
    must not take the composer down, and a message with the generic credential
    is a working message.
    """
    reference = str(entry.get("reference") or "").strip()
    name = str(entry.get("name") or "").strip() or None
    summary = str(entry.get("summary") or "").strip() or None
    descriptor = str(entry.get("descriptor") or "").strip() or None
    work = str(entry.get("work") or "").strip() or None
    url = str(entry.get("url") or "").strip() or None
    # One of the two shapes, fully. A half-filled entry of either kind renders
    # a sentence with a hole in it, so it is skipped rather than patched.
    own_system = bool(name and summary and url)
    engagement = bool(descriptor and work)
    if not reference or not (own_system or engagement):
        logger.warning(
            "case study skipped: needs a reference plus either "
            "(name, summary, url) or (descriptor, work): %r",
            entry,
        )
        return None
    outcome = str(entry.get("outcome") or "").strip() or None
    study = CaseStudy(
        reference=reference,
        name=name,
        summary=summary,
        descriptor=descriptor,
        work=work,
        outcome=outcome,
        industries=_as_set(entry.get("industries")),
        families=_as_set(entry.get("families")),
        issue_types=_as_set(entry.get("issue_types")),
        url=url,
    )
    # The sentence has to survive the same claim check the finished message
    # does. "rebuilt *the* booking flow" pairs an article with a site noun, and
    # the validator reads that as an assertion about the *recipient's* booking
    # flow -- which it cannot be, since this sentence is about somebody else.
    #
    # Rejected here rather than exempted there. The alternative is a validator
    # that stops reading one paragraph, and that paragraph is the one a
    # generated message would most like to smuggle an unevidenced claim into.
    # Reworded to the indefinite article ("rebuilt a booking flow") it passes,
    # says the same thing, and the operator finds out now instead of watching
    # drafts fail silently.
    if reads_as_recipient_claim(study.sentence()):
        logger.warning(
            "case study %r rejected: its sentence reads as a claim about the "
            "recipient's own site. Prefer 'a booking flow' to 'the booking "
            "flow'. Sentence: %s",
            reference,
            study.sentence(),
        )
        return None
    return study


def load(path: Path | str | None) -> tuple[CaseStudy, ...]:
    """Read the registry. An absent or broken file is an empty registry."""
    if not path:
        return ()
    file = Path(path)
    if not file.is_file():
        return ()
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("case study file unreadable (%s): %s", file, error)
        return ()
    if not isinstance(raw, list):
        logger.warning("case study file is not a list of objects: %s", file)
        return ()
    parsed = [_parse(item) for item in raw if isinstance(item, dict)]
    return tuple(study for study in parsed if study is not None)


@lru_cache(maxsize=1)
def _cached(path: str) -> tuple[CaseStudy, ...]:
    return load(path)


def registry(path: Path | str | None) -> tuple[CaseStudy, ...]:
    """The loaded registry, cached per path.

    Cached because the composer runs per draft and the file does not change
    between them. ``registry.cache_clear()`` after editing the file, or restart
    the worker -- which is what a deploy does anyway.
    """
    return _cached(str(path)) if path else ()


registry.cache_clear = _cached.cache_clear  # type: ignore[attr-defined]


def select(
    studies: tuple[CaseStudy, ...],
    *,
    industry: str | None,
    family: str | None,
    issue_type: str | None,
) -> CaseStudy | None:
    """The most relevant case study, or ``None``.

    Relevance is ranked, not scored: a job in the reader's industry *and* on
    the reader's exact defect beats one that merely shares the industry, and
    both beat a generic entry. Ties break on ``reference`` so the same lead
    gets the same study on a retry -- a follow-up that cites different previous
    work than the first message reads as two different senders.

    An entry with no industries and no families matches nothing here. Blank
    targeting means "I did not say what this is evidence of", and the whole
    point of the paragraph is that it is evidence of something specific.
    """
    industry_key = (industry or "").strip().lower()
    family_key = (family or "").strip().lower()
    issue_key = (issue_type or "").strip().lower()

    def rank(study: CaseStudy) -> tuple[int, str] | None:
        industry_hit = bool(industry_key and industry_key in study.industries)
        issue_hit = bool(issue_key and issue_key in study.issue_types)
        family_hit = bool(family_key and family_key in study.families)
        if not (industry_hit or issue_hit or family_hit):
            return None
        if industry_hit and issue_hit:
            tier = 0
        elif industry_hit and family_hit:
            tier = 1
        elif issue_hit:
            tier = 2
        elif industry_hit:
            tier = 3
        else:
            tier = 4
        return (tier, study.reference)

    ranked = [(rank(study), study) for study in studies]
    candidates = [(key, study) for key, study in ranked if key is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda pair: pair[0])[1]  # type: ignore[arg-type]


__all__ = ["CaseStudy", "load", "registry", "select"]
