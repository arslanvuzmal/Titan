"""Deterministic finding detection from browser evidence.

Every rule here answers a question that was *measured*, not inferred. A rule
may only fire when it can name the page, the selector or metric it looked at,
and the value it observed -- which is what makes the resulting claim defensible
in an email (mission section 7.5, invariant 7).

Model inference is deliberately absent from this module. Models add narrative
and prioritisation later; they never create a finding, because a hallucinated
finding is a false statement about a real business.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from titan.contracts.evidence import CrawlResult, PageEvidence, finding_fingerprint
from titan.db.enums import FindingCategory, Severity, VerificationMethod


@dataclass(frozen=True, slots=True)
class DetectedFinding:
    """A finding before persistence. Mirrors the AuditFinding columns."""

    category: FindingCategory
    issue_type: str
    title: str
    severity: Severity
    confidence: float
    verification_method: VerificationMethod
    page_url: str | None = None
    selector: str | None = None
    observed_value: str | None = None
    expected_behavior: str | None = None
    business_impact: str | None = None
    recommended_solution: str | None = None
    estimated_effort: str | None = None
    #: Evidence excerpts supporting the finding, each with a source URL.
    evidence: tuple[tuple[str, str], ...] = field(default=())

    def is_pitchable(self, min_confidence: float = 0.7) -> bool:
        """Whether this finding may justify a claim in a recipient-facing message.

        Same rule as AuditFinding.is_pitchable, kept on the dataclass too so the
        detection stage can count pitchable findings before anything is
        persisted: measured (not model-inferred), confident enough, and backed
        by at least one evidence excerpt.
        """
        from titan.db.enums import PITCHABLE_METHODS

        return (
            self.verification_method in PITCHABLE_METHODS
            and self.confidence >= min_confidence
            and bool(self.evidence)
        )

    @property
    def fingerprint(self) -> str:
        return finding_fingerprint(
            self.issue_type, self.page_url, self.selector, self.observed_value
        )


# Effort estimates are the owner's own delivery estimates, not guesses about
# the prospect's engineering capacity.
SMALL, MEDIUM, LARGE = "small", "medium", "large"


def _f(**kwargs: Any) -> DetectedFinding:
    return DetectedFinding(**kwargs)


# --------------------------------------------------------------------------
# Individual rules. Each returns zero or one finding for a page.
# --------------------------------------------------------------------------
def _missing_viewport(page: PageEvidence) -> DetectedFinding | None:
    if page.has_viewport_meta or page.depth != 0:
        return None
    return _f(
        category=FindingCategory.TECHNICAL,
        issue_type="missing_mobile_viewport",
        title="Homepage has no mobile viewport tag",
        severity=Severity.HIGH,
        confidence=1.0,
        verification_method=VerificationMethod.DOM_ASSERTION,
        page_url=page.final_url,
        selector='meta[name="viewport"]',
        observed_value="absent",
        expected_behavior="A viewport meta tag so the layout adapts to phones",
        business_impact=(
            "On a phone the page renders at desktop width, so visitors must pinch "
            "and zoom to read it or tap a button"
        ),
        recommended_solution="Add a responsive viewport tag and verify key pages on mobile",
        estimated_effort=SMALL,
        evidence=(("meta[name=viewport] not present in <head>", page.final_url),),
    )


def _missing_meta_description(page: PageEvidence) -> DetectedFinding | None:
    if page.depth != 0 or (page.meta_description or "").strip():
        return None
    return _f(
        category=FindingCategory.CONTENT,
        issue_type="missing_meta_description",
        title="Homepage has no meta description",
        severity=Severity.LOW,
        confidence=1.0,
        verification_method=VerificationMethod.DOM_ASSERTION,
        page_url=page.final_url,
        selector='meta[name="description"]',
        observed_value="absent",
        expected_behavior="A description search engines can show under the listing",
        business_impact=(
            "Search results show an arbitrary snippet instead of a chosen summary"
        ),
        recommended_solution="Write a concise description for the main pages",
        estimated_effort=SMALL,
        evidence=(("meta[name=description] not present in <head>", page.final_url),),
    )


def _images_missing_alt(page: PageEvidence) -> DetectedFinding | None:
    # Require a meaningful proportion, not a single decorative image, so this
    # does not fire on well-built sites.
    if page.image_count < 3 or page.images_missing_alt == 0:
        return None
    ratio = page.images_missing_alt / page.image_count
    if ratio < 0.34:
        return None
    return _f(
        category=FindingCategory.ACCESSIBILITY,
        issue_type="images_missing_alt_text",
        title=f"{page.images_missing_alt} of {page.image_count} images lack alt text",
        severity=Severity.MEDIUM if ratio > 0.6 else Severity.LOW,
        confidence=1.0,
        verification_method=VerificationMethod.DOM_ASSERTION,
        page_url=page.final_url,
        selector="img:not([alt])",
        observed_value=f"{page.images_missing_alt}/{page.image_count}",
        expected_behavior="Descriptive alt text on images that convey information",
        business_impact=(
            "Screen-reader users cannot tell what the images show, and search "
            "engines lose the context"
        ),
        recommended_solution="Add alt text to informative images; mark decorative ones empty",
        estimated_effort=SMALL,
        evidence=(
            (
                f"{page.images_missing_alt} img elements without an alt attribute",
                page.final_url,
            ),
        ),
    )


def _console_errors(page: PageEvidence) -> DetectedFinding | None:
    if not page.console_errors:
        return None
    sample = page.console_errors[0][:200]
    return _f(
        category=FindingCategory.TECHNICAL,
        issue_type="javascript_console_errors",
        title=f"{len(page.console_errors)} JavaScript error(s) on load",
        severity=Severity.MEDIUM,
        confidence=0.95,
        verification_method=VerificationMethod.BROWSER_NAVIGATION,
        page_url=page.final_url,
        selector=None,
        observed_value=sample,
        expected_behavior="The page loads without scripting errors",
        business_impact=(
            "Script errors can stop buttons, forms, or tracking from working, "
            "usually without any visible warning"
        ),
        recommended_solution="Fix the failing scripts and add error monitoring",
        estimated_effort=MEDIUM,
        evidence=tuple((err[:300], page.final_url) for err in page.console_errors[:3]),
    )


def _failed_requests(page: PageEvidence) -> DetectedFinding | None:
    if len(page.failed_requests) < 2:
        return None
    return _f(
        category=FindingCategory.TECHNICAL,
        issue_type="failed_network_requests",
        title=f"{len(page.failed_requests)} resource(s) failed to load",
        severity=Severity.MEDIUM,
        confidence=0.95,
        verification_method=VerificationMethod.BROWSER_NAVIGATION,
        page_url=page.final_url,
        observed_value=page.failed_requests[0][:200],
        expected_behavior="All referenced resources load successfully",
        business_impact="Missing images, styles or scripts degrade how the page looks and works",
        recommended_solution="Repair or remove the broken resource references",
        estimated_effort=SMALL,
        evidence=tuple((r[:300], page.final_url) for r in page.failed_requests[:3]),
    )


def _broken_cta(page: PageEvidence) -> DetectedFinding | None:
    """A call to action whose target 404s or renders an empty page.

    Deliberately requires target_status/target_is_empty to have been *measured*.
    If the crawler did not probe the target, no finding is produced -- an
    unverified CTA is not a claim.
    """
    for cta in page.ctas:
        broken_status = cta.target_status is not None and cta.target_status >= 400
        empty_target = cta.target_is_empty is True
        if not (broken_status or empty_target):
            continue
        observed = (
            f"HTTP {cta.target_status}" if broken_status else "renders an empty page"
        )
        return _f(
            category=FindingCategory.CONVERSION,
            issue_type="broken_primary_cta",
            title=f"Call-to-action {cta.text or cta.href!r} leads nowhere",
            severity=Severity.CRITICAL,
            confidence=0.98,
            verification_method=VerificationMethod.BROWSER_NAVIGATION,
            page_url=page.final_url,
            selector=cta.selector,
            observed_value=observed,
            expected_behavior="Opens a working enquiry, booking or contact flow",
            business_impact=(
                "Visitors who have already decided to act cannot complete the "
                "next step, so the most valuable traffic is lost"
            ),
            recommended_solution="Point the button at a tested enquiry or booking flow",
            estimated_effort=SMALL,
            evidence=((f"{cta.selector} -> {cta.href} : {observed}", page.final_url),),
        )
    return None


def _broken_internal_links(pages: list[PageEvidence]) -> DetectedFinding | None:
    broken = [
        (p.url, p.http_status)
        for p in pages
        if p.http_status is not None and p.http_status >= 400
    ]
    if not broken:
        return None
    url, status = broken[0]
    return _f(
        category=FindingCategory.TECHNICAL,
        issue_type="broken_internal_link",
        title=f"{len(broken)} internal page(s) return an error",
        severity=Severity.HIGH,
        confidence=1.0,
        verification_method=VerificationMethod.HTTP_RESPONSE,
        page_url=url,
        observed_value=f"HTTP {status}",
        expected_behavior="Linked pages return HTTP 200",
        business_impact="Visitors following a navigation link reach an error page",
        recommended_solution="Fix or remove the broken links",
        estimated_effort=SMALL,
        evidence=tuple((f"{u} returned HTTP {s}", u) for u, s in broken[:3]),
    )


def _no_booking_path(pages: list[PageEvidence]) -> DetectedFinding | None:
    """No way to book or schedule anywhere on the crawled site."""
    if any(p.booking_links for p in pages):
        return None
    # A contact form is an acceptable substitute; only fire when there is
    # neither a booking link nor a form anywhere.
    if any(p.forms for p in pages):
        return None
    home = pages[0]
    return _f(
        category=FindingCategory.BOOKING,
        issue_type="no_booking_or_enquiry_path",
        title="No booking link or enquiry form found on the site",
        severity=Severity.HIGH,
        confidence=0.9,
        verification_method=VerificationMethod.DOM_ASSERTION,
        page_url=home.final_url,
        observed_value=f"0 booking links and 0 forms across {len(pages)} pages",
        expected_behavior="A visible way to book, enquire, or request a callback",
        business_impact=(
            "Interested visitors have to find a phone number and call during "
            "office hours, so out-of-hours interest is lost entirely"
        ),
        recommended_solution="Add a short enquiry form or a booking link to the main pages",
        estimated_effort=MEDIUM,
        evidence=tuple(
            (f"{p.final_url}: no booking link, no form", p.final_url) for p in pages[:3]
        ),
    )


def _high_friction_form(page: PageEvidence) -> DetectedFinding | None:
    for form in page.forms:
        if form.field_count < 8:
            continue
        return _f(
            category=FindingCategory.CONVERSION,
            issue_type="high_friction_contact_form",
            title=f"Enquiry form asks for {form.field_count} fields",
            severity=Severity.MEDIUM,
            confidence=0.9,
            verification_method=VerificationMethod.DOM_ASSERTION,
            page_url=page.final_url,
            selector=form.selector,
            observed_value=f"{form.field_count} visible fields",
            expected_behavior="A short first-contact form; details gathered later",
            business_impact=(
                "Long forms are abandoned part-way, so enquiries that were "
                "already started never arrive"
            ),
            recommended_solution="Reduce the first step to name, contact, and message",
            estimated_effort=SMALL,
            evidence=(
                (
                    f"{form.selector} fields: {', '.join(form.field_names[:12])}",
                    page.final_url,
                ),
            ),
        )
    return None


def _no_visible_phone(pages: list[PageEvidence]) -> DetectedFinding | None:
    if any(p.visible_phones for p in pages):
        return None
    home = pages[0]
    return _f(
        category=FindingCategory.CONVERSION,
        issue_type="no_visible_phone_number",
        title="No phone number found on the crawled pages",
        severity=Severity.MEDIUM,
        confidence=0.85,
        verification_method=VerificationMethod.DOM_ASSERTION,
        page_url=home.final_url,
        observed_value=f"no tel: link or phone-shaped text across {len(pages)} pages",
        expected_behavior="A tappable phone number on mobile",
        business_impact="Visitors who prefer to call cannot find how",
        recommended_solution="Add a tel: link in the header and footer",
        estimated_effort=SMALL,
        evidence=tuple(
            (f"{p.final_url}: no phone found", p.final_url) for p in pages[:3]
        ),
    )


def _missing_security_headers(page: PageEvidence) -> DetectedFinding | None:
    headers = page.security_headers
    if headers is None or page.depth != 0:
        return None
    missing = [
        name
        for name, value in (
            ("Strict-Transport-Security", headers.strict_transport_security),
            ("X-Content-Type-Options", headers.x_content_type_options),
            ("Content-Security-Policy", headers.content_security_policy),
        )
        if not value
    ]
    if len(missing) < 2:
        return None
    return _f(
        category=FindingCategory.SECURITY,
        issue_type="missing_security_headers",
        title=f"{len(missing)} standard security headers are absent",
        severity=Severity.LOW,
        confidence=1.0,
        verification_method=VerificationMethod.HEADER_INSPECTION,
        page_url=page.final_url,
        observed_value=", ".join(missing),
        expected_behavior="Standard hardening headers on HTML responses",
        business_impact="Reduced protection against common browser-side attacks",
        recommended_solution="Add the headers at the web server or CDN",
        estimated_effort=SMALL,
        evidence=((f"absent: {', '.join(missing)}", page.final_url),),
    )


def _no_structured_data(pages: list[PageEvidence]) -> DetectedFinding | None:
    if any(p.structured_data_types for p in pages):
        return None
    home = pages[0]
    return _f(
        category=FindingCategory.CONTENT,
        issue_type="no_structured_data",
        title="No structured data (schema.org) found",
        severity=Severity.LOW,
        confidence=1.0,
        verification_method=VerificationMethod.STRUCTURED_DATA,
        page_url=home.final_url,
        observed_value="0 application/ld+json blocks",
        expected_behavior="LocalBusiness markup so search engines show hours, rating, location",
        business_impact="Local search results show less information than competitors'",
        recommended_solution="Add LocalBusiness structured data to the homepage",
        estimated_effort=SMALL,
        evidence=tuple((f"{p.final_url}: no ld+json", p.final_url) for p in pages[:3]),
    )


def _accessibility_violations(page: PageEvidence) -> DetectedFinding | None:
    serious = [
        v for v in page.accessibility_violations if v.impact in {"serious", "critical"}
    ]
    if not serious:
        return None
    top = serious[0]
    return _f(
        category=FindingCategory.ACCESSIBILITY,
        issue_type="serious_accessibility_violations",
        title=f"{len(serious)} serious accessibility issue(s) detected",
        severity=Severity.MEDIUM,
        confidence=0.95,
        verification_method=VerificationMethod.AXE_RULE,
        page_url=page.final_url,
        selector=top.sample_selector,
        observed_value=f"{top.rule_id} ({top.node_count} nodes)",
        expected_behavior="No serious or critical axe-core violations",
        business_impact=(
            "Some visitors cannot use the site, and in several jurisdictions "
            "this carries legal exposure"
        ),
        recommended_solution="Fix the flagged rules, starting with the highest impact",
        estimated_effort=MEDIUM,
        evidence=tuple(
            (f"{v.rule_id}: {v.description or ''} ({v.node_count} nodes)", page.final_url)
            for v in serious[:3]
        ),
    )


def _slow_page(page: PageEvidence) -> DetectedFinding | None:
    perf = page.performance
    if perf is None or perf.largest_contentful_paint_ms is None:
        return None
    lcp = perf.largest_contentful_paint_ms
    if lcp < 4000:
        return None
    return _f(
        category=FindingCategory.PERFORMANCE,
        issue_type="slow_largest_contentful_paint",
        title=f"Main content takes {lcp / 1000:.1f}s to appear",
        severity=Severity.HIGH if lcp >= 6000 else Severity.MEDIUM,
        confidence=0.9,
        verification_method=VerificationMethod.LIGHTHOUSE_METRIC,
        page_url=page.final_url,
        observed_value=f"LCP {lcp:.0f}ms",
        expected_behavior="Largest contentful paint under 2.5s",
        business_impact="Visitors leave before the page finishes loading",
        recommended_solution="Compress images, defer non-critical scripts, enable caching",
        estimated_effort=MEDIUM,
        evidence=((f"Lighthouse LCP = {lcp:.0f}ms", page.final_url),),
    )


PAGE_RULES = (
    _missing_viewport,
    _missing_meta_description,
    _images_missing_alt,
    _console_errors,
    _failed_requests,
    _broken_cta,
    _high_friction_form,
    _missing_security_headers,
    _accessibility_violations,
    _slow_page,
)

SITE_RULES = (
    _broken_internal_links,
    _no_booking_path,
    _no_visible_phone,
    _no_structured_data,
)


def detect_findings(result: CrawlResult) -> list[DetectedFinding]:
    """Run every rule over a crawl result.

    Deduplicated by fingerprint, so a rule that fires on several pages for the
    same underlying issue produces one finding, and a re-crawl of an unchanged
    site produces the same set.
    """
    if not result.pages:
        return []

    findings: dict[str, DetectedFinding] = {}

    # Site-wide rules see only successfully-loaded pages, so a single 404 does
    # not make the whole site look like it has no phone number.
    ok_pages = [p for p in result.pages if (p.http_status or 200) < 400]
    if ok_pages:
        for site_rule in SITE_RULES:
            found = site_rule(ok_pages)
            if found is not None:
                findings.setdefault(found.fingerprint, found)

    for page in result.pages:
        if (page.http_status or 200) >= 400:
            continue
        for rule in PAGE_RULES:
            found = rule(page)
            if found is not None:
                findings.setdefault(found.fingerprint, found)

    # Broken-link detection needs the error pages, so it runs over everything.
    broken = _broken_internal_links(result.pages)
    if broken is not None:
        findings.setdefault(broken.fingerprint, broken)

    return sorted(
        findings.values(),
        key=lambda f: (-_severity_rank(f.severity), f.issue_type),
    )


def _severity_rank(severity: Severity) -> int:
    return {
        Severity.INFO: 0,
        Severity.LOW: 1,
        Severity.MEDIUM: 2,
        Severity.HIGH: 3,
        Severity.CRITICAL: 4,
    }[severity]


__all__ = ["PAGE_RULES", "SITE_RULES", "DetectedFinding", "detect_findings"]
