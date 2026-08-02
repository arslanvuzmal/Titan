"""Explainable lead scoring.

Deterministic by construction (mission section 10): the same inputs always
produce the same score, and every point is attributable to a named dimension
with a stated reason. A model may *contribute an input* -- for example an
industry-fit judgement -- but it never assigns the total, because a score that
cannot be explained cannot be defended to the operator deciding whether to
contact a real business.

Scores are bounded to 0-100 and banded:

    85-100  high priority
    70-84   qualified
    55-69   manual review
    <55     archive / reject

Thresholds are configurable per campaign; the bands are not, because they are
what the UI and the policy engine agree on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from titan.db.enums import (
    SEVERITY_WEIGHT,
    ContactSource,
    VerificationStatus,
)
from titan.intelligence.findings import DetectedFinding

#: Bumped whenever a weight or rule changes, and stored on every LeadScore row
#: so a past decision remains interpretable after a policy change.
SCORING_POLICY_VERSION = "2026.08.02-1"


class Band(StrEnum):
    HIGH_PRIORITY = "high_priority"
    QUALIFIED = "qualified"
    MANUAL_REVIEW = "manual_review"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class Component:
    key: str
    raw: float  # 0..1
    weight: float  # points this dimension can contribute
    reason: str
    evidence_ids: tuple[str, ...] = field(default=())

    @property
    def weighted(self) -> float:
        return round(self.raw * self.weight, 3)


@dataclass(frozen=True, slots=True)
class ScoreResult:
    total: int
    band: Band
    components: tuple[Component, ...]
    reasons: tuple[str, ...]
    policy_version: str
    threshold_applied: int
    passed_threshold: bool

    def to_json(self) -> dict[str, object]:
        return {
            "total": self.total,
            "band": self.band.value,
            "policy_version": self.policy_version,
            "threshold_applied": self.threshold_applied,
            "passed_threshold": self.passed_threshold,
            "components": {
                c.key: {
                    "raw": c.raw,
                    "weight": c.weight,
                    "weighted": c.weighted,
                    "reason": c.reason,
                    "evidence_ids": list(c.evidence_ids),
                }
                for c in self.components
            },
            "reasons": list(self.reasons),
        }


@dataclass(slots=True)
class ScoringInput:
    """Everything the scorer looks at. Plain data; the scorer does no I/O."""

    findings: list[DetectedFinding]
    # fit
    industry_matches_campaign: bool
    geography_matches_campaign: bool
    services_deliverable: bool
    # business signals
    review_count: int | None
    rating: float | None
    has_website: bool
    website_reachable: bool
    business_status: str | None
    # contact quality
    contact_source: ContactSource | None
    contact_verification: VerificationStatus
    contact_is_decision_maker: bool
    contact_is_generic_role: bool
    # commercial
    estimated_project_value_usd: float | None
    # risk
    compliance_risk_flags: tuple[str, ...] = field(default=())
    outreach_risk_flags: tuple[str, ...] = field(default=())


#: Weights sum to 100. Kept explicit rather than derived so that a change is a
#: visible diff and forces a policy-version bump.
WEIGHTS: dict[str, float] = {
    "industry_fit": 10.0,
    "geographic_fit": 5.0,
    "service_fit": 10.0,
    "finding_severity": 20.0,
    "finding_confidence": 8.0,
    "opportunity_breadth": 7.0,
    "commercial_impact": 10.0,
    "contact_quality": 15.0,
    "decision_maker": 5.0,
    "business_activity": 10.0,
}

#: Penalties are applied after the weighted sum, so a high-scoring lead with a
#: compliance problem still ends up below the qualification line.
COMPLIANCE_RISK_PENALTY = 25.0
OUTREACH_RISK_PENALTY = 10.0


def _pitchable(findings: list[DetectedFinding]) -> list[DetectedFinding]:
    """Findings that may justify a claim: measured, not model-inferred."""
    from titan.db.enums import PITCHABLE_METHODS

    return [
        f
        for f in findings
        if f.verification_method in PITCHABLE_METHODS
        and f.confidence >= 0.7
        and f.evidence
    ]


def score_lead(data: ScoringInput, threshold: int = 70) -> ScoreResult:
    """Compute an explainable score. Pure function."""
    pitchable = _pitchable(data.findings)
    components: list[Component] = []
    reasons: list[str] = []

    # ---- fit ----------------------------------------------------------
    components.append(
        Component(
            "industry_fit",
            1.0 if data.industry_matches_campaign else 0.3,
            WEIGHTS["industry_fit"],
            "matches the campaign industry"
            if data.industry_matches_campaign
            else "outside the campaign's target industry",
        )
    )
    components.append(
        Component(
            "geographic_fit",
            1.0 if data.geography_matches_campaign else 0.2,
            WEIGHTS["geographic_fit"],
            "inside the target geography"
            if data.geography_matches_campaign
            else "outside the target geography",
        )
    )
    components.append(
        Component(
            "service_fit",
            1.0 if data.services_deliverable else 0.0,
            WEIGHTS["service_fit"],
            "the identified work is deliverable"
            if data.services_deliverable
            else "no deliverable service matches this lead's needs",
        )
    )
    if not data.services_deliverable:
        reasons.append(
            "Service fit is zero: no offer in the playbook is deliverable for "
            "this lead's evidenced issues"
        )

    # ---- findings -----------------------------------------------------
    if pitchable:
        top_severity = max(SEVERITY_WEIGHT[f.severity] for f in pitchable)
        mean_confidence = sum(f.confidence for f in pitchable) / len(pitchable)
        # Breadth saturates at four distinct categories: a lead with problems
        # everywhere is not four times better than one with a clear headline issue.
        categories = {f.category for f in pitchable}
        breadth = min(len(categories), 4) / 4
    else:
        top_severity = mean_confidence = breadth = 0.0
        reasons.append("No evidence-backed findings; nothing specific to open with")

    components.append(
        Component(
            "finding_severity",
            top_severity,
            WEIGHTS["finding_severity"],
            f"most severe evidenced finding scores {top_severity:.2f}"
            if pitchable
            else "no evidenced findings",
            tuple(f.fingerprint for f in pitchable[:5]),
        )
    )
    components.append(
        Component(
            "finding_confidence",
            mean_confidence,
            WEIGHTS["finding_confidence"],
            f"mean confidence across {len(pitchable)} evidenced findings is {mean_confidence:.2f}"
            if pitchable
            else "no evidenced findings",
        )
    )
    components.append(
        Component(
            "opportunity_breadth",
            breadth,
            WEIGHTS["opportunity_breadth"],
            f"{len({f.category for f in pitchable})} distinct problem areas"
            if pitchable
            else "no distinct problem areas",
        )
    )

    # ---- commercial ----------------------------------------------------
    value = data.estimated_project_value_usd or 0.0
    # Linear to 10k, then flat: beyond that the constraint is delivery capacity,
    # not lead value.
    value_raw = min(value / 10_000.0, 1.0)
    components.append(
        Component(
            "commercial_impact",
            value_raw,
            WEIGHTS["commercial_impact"],
            f"estimated project value ${value:,.0f}",
        )
    )

    # ---- contact quality -----------------------------------------------
    contact_raw, contact_reason = _contact_quality(data)
    components.append(
        Component(
            "contact_quality", contact_raw, WEIGHTS["contact_quality"], contact_reason
        )
    )
    components.append(
        Component(
            "decision_maker",
            1.0
            if data.contact_is_decision_maker
            else (0.4 if not data.contact_is_generic_role else 0.2),
            WEIGHTS["decision_maker"],
            "named decision maker"
            if data.contact_is_decision_maker
            else (
                "named individual"
                if not data.contact_is_generic_role
                else "generic role address"
            ),
        )
    )

    # ---- business activity ---------------------------------------------
    activity_raw, activity_reason = _business_activity(data)
    components.append(
        Component(
            "business_activity",
            activity_raw,
            WEIGHTS["business_activity"],
            activity_reason,
        )
    )

    subtotal = sum(c.weighted for c in components)

    # ---- penalties -------------------------------------------------------
    penalty = 0.0
    if data.compliance_risk_flags:
        penalty += COMPLIANCE_RISK_PENALTY
        reasons.append("Compliance risk: " + ", ".join(data.compliance_risk_flags))
    if data.outreach_risk_flags:
        penalty += OUTREACH_RISK_PENALTY
        reasons.append("Outreach risk: " + ", ".join(data.outreach_risk_flags))

    total = int(round(max(0.0, min(100.0, subtotal - penalty))))

    # ---- hard gates -------------------------------------------------------
    # A lead with nothing evidenced cannot be "qualified" regardless of fit:
    # there would be nothing truthful to say in the first sentence.
    if not pitchable:
        total = min(total, 54)
        reasons.append("Capped below qualification: no evidence to open the message with")
    if data.business_status and data.business_status.upper() not in {
        "OPERATIONAL",
        "BUSINESS_STATUS_UNSPECIFIED",
        "",
    }:
        total = min(total, 20)
        reasons.append(f"Business status is {data.business_status}")

    band = _band(total)
    if not reasons:
        reasons.append(
            f"{len(pitchable)} evidenced finding(s); strongest severity "
            f"{max((f.severity.value for f in pitchable), default='none')}"
        )

    return ScoreResult(
        total=total,
        band=band,
        components=tuple(components),
        reasons=tuple(reasons),
        policy_version=SCORING_POLICY_VERSION,
        threshold_applied=threshold,
        passed_threshold=total >= threshold,
    )


def _contact_quality(data: ScoringInput) -> tuple[float, str]:
    if data.contact_source is None:
        return 0.0, "no contact channel discovered"
    if data.contact_source is ContactSource.PATTERN_GUESS:
        # Scored at zero rather than merely low: a guessed address is not a
        # weak contact, it is not a contact at all (invariant 6).
        return 0.0, "address was pattern-guessed and is not usable"
    by_status = {
        VerificationStatus.PROVIDER_VERIFIED: (1.0, "provider-verified address"),
        VerificationStatus.PUBLISHED_FIRST_PARTY: (
            0.9,
            "published on the company's own site",
        ),
        VerificationStatus.UNKNOWN: (0.4, "verification inconclusive"),
        VerificationStatus.UNVERIFIED: (0.3, "address not yet verified"),
        VerificationStatus.RISKY: (0.1, "verification flagged the address as risky"),
        VerificationStatus.INVALID: (0.0, "address is invalid"),
    }
    return by_status.get(
        data.contact_verification, (0.2, "unrecognised verification state")
    )


def _business_activity(data: ScoringInput) -> tuple[float, str]:
    if not data.has_website:
        return 0.1, "no website on record"
    if not data.website_reachable:
        return 0.2, "website did not respond"
    reviews = data.review_count or 0
    # 50 reviews is treated as a well-established local business.
    review_raw = min(reviews / 50.0, 1.0)
    rating_raw = ((data.rating or 0.0) / 5.0) if data.rating else 0.5
    raw = round(0.6 * review_raw + 0.4 * rating_raw, 3)
    return raw, f"{reviews} reviews, rating {data.rating if data.rating else 'unknown'}"


def _band(total: int) -> Band:
    if total >= 85:
        return Band.HIGH_PRIORITY
    if total >= 70:
        return Band.QUALIFIED
    if total >= 55:
        return Band.MANUAL_REVIEW
    return Band.REJECT


__all__ = [
    "SCORING_POLICY_VERSION",
    "WEIGHTS",
    "Band",
    "Component",
    "ScoreResult",
    "ScoringInput",
    "score_lead",
]
