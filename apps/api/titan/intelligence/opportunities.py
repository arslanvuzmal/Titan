"""What is wrong with a business, and which of it we can actually fix.

:mod:`titan.intelligence.findings` establishes *what was measured*. This module
answers the commercial question sitting on top of it: given these findings and
the offers this owner sells, what is worth proposing, what is it worth, and how
would it be delivered.

**The playbook constrains, it does not invent.** An offer is selectable only
when a finding that justifies it was actually evidenced on this site
(:meth:`Playbook.selectable_offers`). Nothing here relaxes that. What it adds is
the roll-up: which findings support which offer, in what order, and with what
implementation outline -- so the operator sees a proposal rather than a list of
defects.

**Problems with no offer are recorded, not hidden.** A site can be measurably
broken in a way this owner does not sell a fix for. Dropping those on the floor
would leave the audit looking like the site was fine in that respect, and would
quietly bias the whole system towards only ever noticing what it can bill for.
They are written with ``deliverable=False`` and a reserved ``unserved:`` offer
key, which nothing may pitch -- see :func:`is_unserved`. That is also the honest
answer to "find deformities and solve them": some deformities are real and we
cannot solve them, and saying so is worth more than a catalogue that always
happens to match.

Pure, no I/O, no model inference. Persistence lives in
:func:`titan.activities.pipeline.analyse_evidence`.
"""

from __future__ import annotations

from dataclasses import dataclass

from titan.db.enums import FindingCategory, Industry, Severity
from titan.intelligence.findings import DetectedFinding
from titan.intelligence.playbooks import Offer, select_offers

#: Prefix for opportunities no offer covers. Reserved: no playbook offer key may
#: begin with it, so ``offer_key.startswith(UNSERVED_PREFIX)`` is a reliable
#: "never pitch this" test even after the row has been round-tripped through the
#: database and lost its ``deliverable`` flag to a careless query.
UNSERVED_PREFIX = "unserved:"

#: Below this, an unmatched finding is not worth recording as a gap. A missing
#: meta description is a real observation and a bad reason to tell an operator
#: their catalogue has a hole in it.
MIN_UNSERVED_SEVERITY = Severity.MEDIUM

#: Deliverable work always sorts above gaps, whatever the severities involved:
#: the list exists to decide what to pitch, and a problem nobody can be paid to
#: fix does not belong at the top of it.
DELIVERABLE_PRIORITY_FLOOR = 100

_SEVERITY_RANK: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}

_EFFORT_RANK: dict[str, int] = {"small": 1, "medium": 2, "large": 3}

#: What the owner needs from the client before the work can start, by the
#: category of the problem being fixed. Stated as an access or a decision,
#: because those are the two things that actually delay delivery -- and both are
#: better raised on the proposal than discovered in week two.
_PREREQUISITES: dict[FindingCategory, str] = {
    FindingCategory.TECHNICAL: "Access to the site's hosting, CMS or repository",
    FindingCategory.PERFORMANCE: "Access to the site's hosting, CMS or repository",
    FindingCategory.SECURITY: "Access to the web server or CDN configuration",
    FindingCategory.ACCESSIBILITY: "Access to the site's templates or theme",
    FindingCategory.CONVERSION: "Agreement on where new enquiries should be routed",
    FindingCategory.BOOKING: "A calendar or booking system to connect to, and its owner",
    FindingCategory.FOLLOW_UP: "Sign-off on the follow-up wording and its timing",
    FindingCategory.AUTOMATION: "Credentials for the CRM or inbox the automation writes to",
    FindingCategory.RETENTION: "A list of past customers, and permission to contact them",
    FindingCategory.REPUTATION: "Access to the business's public review profiles",
    FindingCategory.CONTENT: "Sign-off on wording from whoever owns the brand voice",
}

#: Cap on steps in an outline. A proposal that lists twenty tasks is read as a
#: quote for twenty tasks; the rest belong in scoping.
MAX_OUTLINE_STEPS = 8


@dataclass(frozen=True, slots=True)
class SolutionOutline:
    """How the owner would deliver an opportunity. Internal, never sent.

    Every step is traceable to a finding's ``recommended_solution``, which was
    written against a measured observation. Nothing here is generated prose
    about what the client's business needs.
    """

    summary: str
    implementation_outline: tuple[str, ...]
    estimated_effort: str | None
    prerequisites: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DerivedOpportunity:
    """A commercial opportunity, before persistence. Mirrors the columns."""

    offer_key: str
    title: str
    rationale: str
    #: Fingerprints, not ids -- this module never sees the database. The caller
    #: maps them to ``audit_findings.id`` values.
    supporting_fingerprints: tuple[str, ...]
    estimated_value_usd: float | None
    priority: int
    deliverable: bool
    #: Absent for gaps. There is no outline for work the owner does not do, and
    #: inventing one would be the first fabricated thing in the pipeline.
    solution: SolutionOutline | None = None

    @property
    def is_unserved(self) -> bool:
        return is_unserved(self.offer_key)


def is_unserved(offer_key: str) -> bool:
    """Whether this key names a gap rather than something sellable.

    The test a message-building path should use. It reads the key, which travels
    with the row, rather than the ``deliverable`` flag, which a projection or a
    partial select can drop without anything failing.
    """
    return offer_key.startswith(UNSERVED_PREFIX)


def derive_opportunities(
    industry: Industry,
    findings: list[DetectedFinding],
    *,
    min_confidence: float = 0.7,
) -> list[DerivedOpportunity]:
    """Roll findings up into what may be proposed, highest priority first.

    Only *pitchable* findings participate. A finding a model inferred, or one
    with no evidence excerpt, cannot support an offer for the same reason it
    cannot support a sentence in an email: there is nothing to show if the
    recipient asks how we know. This is the identical gate ``generate_draft``
    applies, applied one stage earlier so the opportunity and the message can
    never disagree about what was established.

    Returns an empty list when nothing was evidenced, which is the intended
    outcome rather than a failure -- there is then nothing truthful to sell.
    """
    pitchable = [f for f in findings if f.is_pitchable(min_confidence)]
    if not pitchable:
        return []

    evidenced = {f.issue_type for f in pitchable}
    by_issue_type: dict[str, list[DetectedFinding]] = {}
    for finding in pitchable:
        by_issue_type.setdefault(finding.issue_type, []).append(finding)

    opportunities: list[DerivedOpportunity] = []
    served: set[str] = set()

    for offer in select_offers(industry, evidenced):
        supporting = [
            finding
            for issue_type in sorted(offer.requires_finding_types & evidenced)
            for finding in by_issue_type[issue_type]
        ]
        if not supporting:  # pragma: no cover - select_offers guarantees otherwise
            continue
        served.update(f.issue_type for f in supporting)
        opportunities.append(_deliverable(offer, supporting))

    for issue_type in sorted(evidenced - served):
        group = by_issue_type[issue_type]
        if _severity_of(group) < _SEVERITY_RANK[MIN_UNSERVED_SEVERITY]:
            continue
        opportunities.append(_gap(issue_type, group))

    opportunities.sort(key=lambda o: (-o.priority, o.offer_key))
    return opportunities


def _deliverable(offer: Offer, supporting: list[DetectedFinding]) -> DerivedOpportunity:
    top = _highest_severity(supporting)
    return DerivedOpportunity(
        offer_key=offer.key,
        title=offer.label,
        rationale=(
            f"{len(supporting)} evidenced finding(s) justify this offer; the most "
            f"severe is {top.title!r} ({top.severity.value}, observed on "
            f"{top.page_url or 'the site'})."
        ),
        supporting_fingerprints=tuple(f.fingerprint for f in supporting),
        estimated_value_usd=offer.estimated_value_usd,
        priority=DELIVERABLE_PRIORITY_FLOOR + _priority(supporting),
        deliverable=True,
        solution=_outline(offer, supporting),
    )


def _gap(issue_type: str, group: list[DetectedFinding]) -> DerivedOpportunity:
    top = _highest_severity(group)
    return DerivedOpportunity(
        offer_key=f"{UNSERVED_PREFIX}{issue_type}"[:80],
        title=f"Unserved: {top.title}",
        rationale=(
            f"Measured and {top.severity.value} severity, but no offer in this "
            f"playbook covers {issue_type}. Recorded so the gap is visible; it "
            f"must never be pitched, because there is nothing to sell against it."
        ),
        supporting_fingerprints=tuple(f.fingerprint for f in group),
        # Deliberately unpriced. Attaching a number to work the owner does not
        # do would put revenue in a forecast that nobody can deliver.
        estimated_value_usd=None,
        priority=_priority(group),
        deliverable=False,
        solution=None,
    )


def _outline(offer: Offer, supporting: list[DetectedFinding]) -> SolutionOutline:
    steps: list[str] = []
    for finding in _by_severity(supporting):
        solution = (finding.recommended_solution or "").strip()
        if not solution:
            continue
        step = f"{finding.title}: {solution}"
        if step not in steps:
            steps.append(step)
        if len(steps) == MAX_OUTLINE_STEPS - 1:
            break

    if steps:
        # Named as a step because it is one, and because it is the step that
        # makes the next audit of this lead report the work as done rather than
        # re-detecting it and proposing the same offer again.
        steps.append("Re-run the audit and confirm each finding no longer fires.")

    prerequisites: list[str] = []
    for finding in _by_severity(supporting):
        requirement = _PREREQUISITES.get(finding.category)
        if requirement and requirement not in prerequisites:
            prerequisites.append(requirement)

    return SolutionOutline(
        summary=(
            f"{offer.delivers}. Addresses {len(supporting)} evidenced finding(s) "
            f"across {len({f.category for f in supporting})} area(s)."
        ),
        implementation_outline=tuple(steps),
        estimated_effort=_effort(supporting),
        prerequisites=tuple(prerequisites),
    )


def _by_severity(findings: list[DetectedFinding]) -> list[DetectedFinding]:
    """Most severe first, then by issue type so the order is reproducible."""
    return sorted(
        findings, key=lambda f: (-_SEVERITY_RANK[f.severity], f.issue_type, f.title)
    )


def _highest_severity(findings: list[DetectedFinding]) -> DetectedFinding:
    return _by_severity(findings)[0]


def _severity_of(findings: list[DetectedFinding]) -> int:
    return max(_SEVERITY_RANK[f.severity] for f in findings)


def _priority(findings: list[DetectedFinding]) -> int:
    """Severity dominates; volume breaks ties.

    One critical finding outranks nine low ones, which is how an operator would
    triage by hand. The count is capped so a rule that fires on every page
    cannot outrank a genuinely worse problem by sheer repetition.
    """
    return _severity_of(findings) * 10 + min(len(findings), 9)


def _effort(findings: list[DetectedFinding]) -> str | None:
    """The largest effort among the findings, not the sum.

    These are delivered as one engagement, and the estimates overlap heavily --
    four small fixes to the same booking flow is one small job, not four. Taking
    the maximum understates a large bundle; summing would overstate every bundle,
    and an inflated estimate loses the work before it is quoted.
    """
    ranks = [
        _EFFORT_RANK[f.estimated_effort]
        for f in findings
        if f.estimated_effort in _EFFORT_RANK
    ]
    if not ranks:
        return None
    peak = max(ranks)
    return next(name for name, rank in _EFFORT_RANK.items() if rank == peak)


__all__ = [
    "DELIVERABLE_PRIORITY_FLOOR",
    "MAX_OUTLINE_STEPS",
    "MIN_UNSERVED_SEVERITY",
    "UNSERVED_PREFIX",
    "DerivedOpportunity",
    "SolutionOutline",
    "derive_opportunities",
    "is_unserved",
]
