"""Opportunity derivation: what may be sold, what may only be recorded."""

from __future__ import annotations

import pytest
from titan.db.enums import FindingCategory, Industry, Severity, VerificationMethod
from titan.intelligence.findings import DetectedFinding
from titan.intelligence.opportunities import (
    DELIVERABLE_PRIORITY_FLOOR,
    UNSERVED_PREFIX,
    derive_opportunities,
    is_unserved,
)


def finding(
    issue_type: str = "no_booking_or_enquiry_path",
    *,
    severity: Severity = Severity.HIGH,
    category: FindingCategory = FindingCategory.BOOKING,
    confidence: float = 0.95,
    method: VerificationMethod = VerificationMethod.DOM_ASSERTION,
    evidence: tuple[tuple[str, str], ...] = (("observed", "https://x.test/"),),
    solution: str | None = "Add a short enquiry form",
    effort: str | None = "small",
    title: str | None = None,
) -> DetectedFinding:
    return DetectedFinding(
        category=category,
        issue_type=issue_type,
        title=title or f"{issue_type} on the homepage",
        severity=severity,
        confidence=confidence,
        verification_method=method,
        page_url="https://x.test/",
        recommended_solution=solution,
        estimated_effort=effort,
        evidence=evidence,
    )


# ==========================================================================
# The evidence gate
# ==========================================================================
def test_no_findings_produces_no_opportunities() -> None:
    assert derive_opportunities(Industry.GYM_FITNESS, []) == []


def test_a_model_inferred_finding_cannot_justify_an_offer() -> None:
    """Same gate the message validator applies, applied a stage earlier.

    If this ever relaxes, a hallucinated problem becomes a priced proposal.
    """
    inferred = finding(method=VerificationMethod.MODEL_INFERENCE)
    assert derive_opportunities(Industry.GYM_FITNESS, [inferred]) == []


def test_a_finding_with_no_evidence_excerpt_cannot_justify_an_offer() -> None:
    assert derive_opportunities(Industry.GYM_FITNESS, [finding(evidence=())]) == []


def test_a_low_confidence_finding_cannot_justify_an_offer() -> None:
    assert derive_opportunities(Industry.GYM_FITNESS, [finding(confidence=0.4)]) == []


# ==========================================================================
# Selection
# ==========================================================================
def test_an_evidenced_finding_selects_the_offers_that_require_it() -> None:
    derived = derive_opportunities(Industry.GYM_FITNESS, [finding()])

    keys = {o.offer_key for o in derived}
    # Every gym offer whose requires_finding_types contains this issue type.
    assert "trial_lead_automation" in keys
    assert "class_reminders" in keys
    assert all(o.deliverable for o in derived)


def test_an_offer_whose_requirements_were_not_evidenced_is_never_selected() -> None:
    """The whole point of the playbook gate: a good gym keeps its own pitch out."""
    derived = derive_opportunities(
        Industry.GYM_FITNESS,
        [finding(issue_type="no_structured_data", severity=Severity.LOW)],
    )
    keys = {o.offer_key for o in derived}

    assert "review_collection" in keys  # requires no_structured_data
    assert "trial_lead_automation" not in keys  # requires a booking failure


def test_supporting_findings_are_carried_by_fingerprint() -> None:
    booking = finding()
    derived = derive_opportunities(Industry.GYM_FITNESS, [booking])
    trial = next(o for o in derived if o.offer_key == "trial_lead_automation")

    assert trial.supporting_fingerprints == (booking.fingerprint,)


def test_the_offers_value_is_carried_onto_the_opportunity() -> None:
    derived = derive_opportunities(Industry.GYM_FITNESS, [finding()])
    trial = next(o for o in derived if o.offer_key == "trial_lead_automation")

    assert trial.estimated_value_usd == 2400


# ==========================================================================
# Gaps -- problems this owner does not sell a fix for
# ==========================================================================
def test_a_severe_finding_no_offer_covers_is_recorded_as_a_gap() -> None:
    derived = derive_opportunities(
        Industry.GYM_FITNESS,
        [
            finding(
                issue_type="serious_accessibility_violations",
                category=FindingCategory.ACCESSIBILITY,
                severity=Severity.MEDIUM,
                method=VerificationMethod.AXE_RULE,
            )
        ],
    )

    assert len(derived) == 1
    gap = derived[0]
    assert gap.is_unserved
    assert gap.offer_key == f"{UNSERVED_PREFIX}serious_accessibility_violations"
    assert gap.deliverable is False


def test_a_gap_is_never_priced() -> None:
    """Revenue nobody can deliver must not reach a forecast."""
    derived = derive_opportunities(
        Industry.GYM_FITNESS,
        [
            finding(
                issue_type="failed_network_requests",
                category=FindingCategory.TECHNICAL,
                severity=Severity.MEDIUM,
                method=VerificationMethod.BROWSER_NAVIGATION,
            )
        ],
    )

    assert derived[0].estimated_value_usd is None


def test_a_gap_carries_no_implementation_outline() -> None:
    """There is no honest outline for work the owner does not do."""
    derived = derive_opportunities(
        Industry.GYM_FITNESS,
        [
            finding(
                issue_type="failed_network_requests",
                category=FindingCategory.TECHNICAL,
                severity=Severity.MEDIUM,
                method=VerificationMethod.BROWSER_NAVIGATION,
            )
        ],
    )

    assert derived[0].solution is None


def test_a_minor_unmatched_finding_is_not_reported_as_a_gap() -> None:
    """A missing meta description is not a hole in the catalogue."""
    derived = derive_opportunities(
        Industry.GYM_FITNESS,
        [
            finding(
                issue_type="missing_meta_description",
                category=FindingCategory.CONTENT,
                severity=Severity.LOW,
            )
        ],
    )

    assert derived == []


def test_every_deliverable_outranks_every_gap() -> None:
    derived = derive_opportunities(
        Industry.GYM_FITNESS,
        [
            # A gap at the highest severity there is.
            finding(
                issue_type="serious_accessibility_violations",
                category=FindingCategory.ACCESSIBILITY,
                severity=Severity.CRITICAL,
                method=VerificationMethod.AXE_RULE,
            ),
            # Sellable, and merely low.
            finding(
                issue_type="no_structured_data",
                category=FindingCategory.CONTENT,
                severity=Severity.LOW,
            ),
        ],
    )

    assert derived[0].deliverable is True
    assert derived[-1].is_unserved
    assert derived[0].priority >= DELIVERABLE_PRIORITY_FLOOR
    assert derived[-1].priority < DELIVERABLE_PRIORITY_FLOOR


def test_is_unserved_reads_the_key_not_the_flag() -> None:
    """The key survives a projection that drops the boolean; the test must too."""
    assert is_unserved(f"{UNSERVED_PREFIX}anything")
    assert not is_unserved("trial_lead_automation")


# ==========================================================================
# The solution outline
# ==========================================================================
def test_every_outline_step_comes_from_a_findings_recommended_solution() -> None:
    booking = finding(solution="Add a short enquiry form to the main pages")
    derived = derive_opportunities(Industry.GYM_FITNESS, [booking])
    outline = next(o for o in derived if o.offer_key == "trial_lead_automation").solution
    assert outline is not None

    assert any(
        "Add a short enquiry form to the main pages" in s
        for s in outline.implementation_outline
    )


def test_an_outline_ends_by_re_running_the_audit() -> None:
    """Otherwise the next crawl re-detects the work and re-proposes the offer."""
    derived = derive_opportunities(Industry.GYM_FITNESS, [finding()])
    outline = next(o for o in derived if o.deliverable).solution
    assert outline is not None

    assert "Re-run the audit" in outline.implementation_outline[-1]


def test_effort_is_the_largest_not_the_sum() -> None:
    """Four small fixes to one booking flow is a small job, not four."""
    derived = derive_opportunities(
        Industry.GYM_FITNESS,
        [
            finding(effort="small"),
            finding(
                issue_type="broken_primary_cta",
                category=FindingCategory.CONVERSION,
                severity=Severity.CRITICAL,
                method=VerificationMethod.BROWSER_NAVIGATION,
                effort="medium",
            ),
        ],
    )
    outline = next(o for o in derived if o.offer_key == "trial_lead_automation").solution
    assert outline is not None

    assert outline.estimated_effort == "medium"


def test_prerequisites_are_deduplicated_and_ordered_by_severity() -> None:
    derived = derive_opportunities(
        Industry.GYM_FITNESS,
        [
            finding(),  # BOOKING, high
            finding(
                issue_type="broken_primary_cta",
                category=FindingCategory.CONVERSION,
                severity=Severity.CRITICAL,
                method=VerificationMethod.BROWSER_NAVIGATION,
            ),
        ],
    )
    outline = next(o for o in derived if o.offer_key == "trial_lead_automation").solution
    assert outline is not None

    assert len(set(outline.prerequisites)) == len(outline.prerequisites)
    # The critical conversion finding sorts first, so its prerequisite leads.
    assert "enquiries" in outline.prerequisites[0]


def test_a_finding_with_no_recommended_solution_contributes_no_step() -> None:
    derived = derive_opportunities(
        Industry.GYM_FITNESS, [finding(solution=None, effort=None)]
    )
    outline = next(o for o in derived if o.deliverable).solution
    assert outline is not None

    assert outline.implementation_outline == ()
    assert outline.estimated_effort is None


# ==========================================================================
# Determinism -- the same evidence must produce the same proposal
# ==========================================================================
@pytest.mark.parametrize(
    "industry",
    [Industry.GYM_FITNESS, Industry.LAW_FIRM, Industry.RESTAURANT, Industry.GENERAL],
)
def test_derivation_is_stable_across_runs(industry: Industry) -> None:
    findings = [
        finding(),
        finding(
            issue_type="broken_primary_cta",
            category=FindingCategory.CONVERSION,
            severity=Severity.CRITICAL,
            method=VerificationMethod.BROWSER_NAVIGATION,
        ),
    ]

    first = derive_opportunities(industry, findings)
    second = derive_opportunities(industry, list(reversed(findings)))

    assert [o.offer_key for o in first] == [o.offer_key for o in second]
    assert [o.priority for o in first] == [o.priority for o in second]
