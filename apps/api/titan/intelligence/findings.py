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
from urllib.parse import urljoin, urlsplit

from titan.contracts.evidence import CrawlResult, PageEvidence, finding_fingerprint
from titan.db.enums import FindingCategory, Severity, VerificationMethod
from titan.intelligence.attribution import (
    attributable_console_errors,
    attributable_requests,
)


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
    """Scripting errors in the prospect's own code.

    Filtered before counting. The browser prints CSP refusals, cookie
    deprecation notices and third-party widget failures as errors, and none of
    them is a defect the recipient wrote or can fix. Claiming otherwise in a
    cold email is checkably false to the first developer who looks.
    """
    errors = attributable_console_errors(
        list(page.console_errors), site_url=page.final_url
    )
    if not errors:
        return None
    sample = errors[0][:200]
    return _f(
        category=FindingCategory.TECHNICAL,
        issue_type="javascript_console_errors",
        title=f"{len(errors)} JavaScript error(s) on load",
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
        evidence=tuple((err[:300], page.final_url) for err in errors[:3]),
    )


def _failed_requests(page: PageEvidence) -> DetectedFinding | None:
    """Resources on the prospect's own site that did not load.

    Analytics beacons are excluded before the threshold is applied, not after.
    Our crawler blocks them and so does a large share of real visitors'
    ad-blockers, so a failure is expected rather than diagnostic -- and two
    blocked Google Analytics calls used to be enough to tell somebody their
    website was broken.
    """
    failures = attributable_requests(list(page.failed_requests), site_url=page.final_url)
    if len(failures) < 2:
        return None
    return _f(
        category=FindingCategory.TECHNICAL,
        issue_type="failed_network_requests",
        title=f"{len(failures)} resource(s) failed to load",
        severity=Severity.MEDIUM,
        confidence=0.95,
        verification_method=VerificationMethod.BROWSER_NAVIGATION,
        page_url=page.final_url,
        observed_value=failures[0][:200],
        expected_behavior="All referenced resources load successfully",
        business_impact="Missing images, styles or scripts degrade how the page looks and works",
        recommended_solution="Repair or remove the broken resource references",
        estimated_effort=SMALL,
        evidence=tuple((r[:300], page.final_url) for r in failures[:3]),
    )


def _broken_cta(pages: list[PageEvidence]) -> DetectedFinding | None:
    """A visible call to action whose target we fetched and found broken.

    The most valuable finding this module can produce -- "the Book Now button
    on your homepage leads to an error page" is a sentence a stranger acts on
    -- and until 10 September it had **never fired once**. 217,024 calls to
    action had been recorded across the estate and not one had a target status,
    because the browser worker writes ``target_status: null`` as a literal and
    nothing has ever filled it in. The rule required the field to have been
    measured, correctly, and so it could never be satisfied.

    The obvious repair -- read the status from the crawl, which visits contact
    and booking pages anyway -- was tried on 2026-09-10 and **rejected on the
    evidence**. Eight of the CTAs it would have condemned were fetched live:
    four answered 200. The cause is in the crawl history:

    ==========================================  ====  =========
    ``whitesmileancoats.com/contact``           200   14:57:18
    ``whitesmileancoats.com/contact``           404   14:57:08
    ==========================================  ====  =========

    Ten seconds apart, and that URL alternates between the two across dozens of
    fetches over three weeks. A single recorded 404 from an incidental page
    visit is not evidence a page is broken; it is one sample of something
    flaky, or of a bot defence. It is nowhere near enough to tell a stranger
    their Book Now button is dead.

    So the requirement stands as it was written, and the fix belongs upstream:
    the browser worker has to navigate to the target and record what it saw,
    ideally more than once. Until it does, this rule produces nothing -- which
    is the correct amount to produce from evidence this thin.
    """
    for page in pages:
        if (page.http_status or 200) >= 400:
            continue  # a CTA on an error page is not the story
        base = page.final_url or page.url
        try:
            here = _canonical(base)
        except ValueError:
            continue
        for cta in page.ctas:
            if not cta.is_visible:
                continue  # invisible to the reader is unverifiable by them
            href = (cta.href or "").strip()
            if not href or href.startswith("#"):
                continue
            if _is_placeholder(href) or _is_bare_address(href):
                continue
            try:
                absolute = urljoin(base, href)
                target = _canonical(absolute)
            except ValueError:
                continue
            if target == here:
                continue
            # Somebody else's site is not ours to report on.
            if target.split("/")[0] != here.split("/")[0]:
                continue

            status = cta.target_status
            broken_status = status is not None and status in BROKEN_LINK_STATUSES
            empty_target = cta.target_is_empty is True
            if not (broken_status or empty_target):
                continue

            observed = f"HTTP {status}" if broken_status else "renders an empty page"
            return _f(
                category=FindingCategory.CONVERSION,
                issue_type="broken_primary_cta",
                title=f"Call-to-action {cta.text or cta.href!r} leads nowhere",
                severity=Severity.CRITICAL,
                confidence=0.98,
                verification_method=VerificationMethod.BROWSER_NAVIGATION,
                page_url=page.final_url or page.url,
                selector=cta.selector,
                observed_value=observed,
                expected_behavior="Opens a working enquiry, booking or contact flow",
                business_impact=(
                    "Visitors who have already decided to act cannot complete "
                    "the next step, so the most valuable traffic is lost"
                ),
                recommended_solution=(
                    "Point the button at a tested enquiry or booking flow"
                ),
                estimated_effort=SMALL,
                evidence=(
                    (
                        f"{cta.selector} -> {cta.href} : {observed}",
                        page.final_url or page.url,
                    ),
                ),
            )
    return None


#: Statuses that mean the page a visitor was sent to is not there.
#:
#: Deliberately narrow, because this finding becomes a sentence written to a
#: stranger about their own business, and it has to be true when they check.
#:
#: Everything else at 4xx and 5xx describes *our* access rather than their site.
#: 403 is a refusal -- bot protection, a datacenter IP, a geo block -- and the
#: page usually works perfectly for a person. 429 is our own crawl rate coming
#: back at us. 401 is a login, which is not a defect. A 5xx is a server having a
#: bad minute and is likely fixed before the email is read.
#:
#: Measured on the live workspace before this existed: of 1,896 broken-link
#: findings, **89 rested on a 403 and 48 on a 429** -- 137 messages that would
#: have told a business their site was broken on the evidence that it had
#: declined to talk to us.
BROKEN_LINK_STATUSES: frozenset[int] = frozenset({404, 410})


#: How many pages must have loaded before "anywhere on the site" means anything.
#:
#: Three: a homepage and two others. The composer turns these findings into
#: claims about the whole site -- *"there is no number anywhere a visitor can
#: see"* -- and one page cannot carry that sentence however cleanly it was
#: read. Below the floor the finding describes how much of the site we managed
#: to reach, not how the site is built, and it is asserted to the one person
#: able to disprove it in a single click.
#:
#: Measured on the live workspace before this existed: 177 of 751
#: no_booking_or_enquiry_path findings and 141 of 488 no_visible_phone_number
#: findings rested on fewer than three loaded pages -- 251 of them on a single
#: page.
#:
#: `detect_findings` has always passed site rules only the pages that loaded,
#: so nothing here needs to re-filter error pages; a guard that did was written
#: first and removed as unreachable. This floor is about how *many* pages were
#: read, which nothing was checking.
#:
#: Not applied to `_no_structured_data`: that one is scoped to "the page" in
#: the copy as well as in the detector, and a homepage that loaded supports it.
MIN_PAGES_FOR_ABSENCE = 3


def _canonical(url: str) -> str:
    """One spelling per address, so a link and a crawl of it compare equal.

    Scheme and ``www`` are dropped because a link written ``http://x.com/a``
    and a crawl of ``https://www.x.com/a`` are the same address to everybody
    except a string comparison. The query is kept: ``?page_id=431`` is a
    different page from ``?page_id=613``, and both appear in live evidence.
    """
    split = urlsplit(url)
    host = split.netloc.lower().removeprefix("www.")
    path = split.path.rstrip("/") or "/"
    return f"{host}{path}" + (f"?{split.query}" if split.query else "")


#: Markers of a template placeholder that the site never substituted.
#:
#: A live page carrying ``href="[#DSR_FORM_URL#]"`` is a real defect -- the CMS
#: shipped the token instead of the address -- but it is not the defect this
#: detector describes, and we cannot quote it back accurately. The fragment is
#: dropped in canonicalisation, so the address survives into the message as
#: ``https://www.vmh.co.uk/[``, and the sentence becomes "the link
#: https://www.vmh.co.uk/[ is broken" about something the reader cannot find on
#: their own page. That is the same failure as naming a probed URL: a claim the
#: recipient can check and find false.
#:
#: Found in the live data on 2026-09-10, on two domains, in the first six
#: broken-link findings the corrected detector produced.
_PLACEHOLDER_MARKERS = ("[#", "#]", "{{", "}}", "${", "<%", "%>", "%%", "[[", "]]")


def _is_placeholder(href: str) -> bool:
    """Whether an href is an unsubstituted template token rather than a link."""
    return any(marker in href for marker in _PLACEHOLDER_MARKERS)


def _is_bare_address(href: str) -> bool:
    """An email or phone written without its scheme, which resolves as a path.

    ``href="info@cristalclinic.ae"`` has no ``mailto:``, so ``urljoin`` reads it
    as a relative path and the crawler dutifully fetches
    ``https://crystalclinic.ae/info@cristalclinic.ae``, which 404s. Reporting
    that as a broken link tells the practice their own email address is a dead
    page. It is a real defect in their markup and an unrecognisable way to
    describe it.
    """
    first_segment = href.split("?", 1)[0].split("#", 1)[0].split("/", 1)[0]
    return "@" in first_segment


def _canonical_strict(url: str) -> str:
    """The same address, but a trailing slash is *not* the same address.

    :func:`_canonical` strips it, which is right for "is this the page I am
    standing on" and wrong for "did we fetch the thing they link to". Servers
    genuinely disagree about the two forms, and it is not rare:

    ================================================  =====
    ``https://dermalclinic.co.uk/contact``            404
    ``https://dermalclinic.co.uk/contact/``           200
    ``https://skinessence.com.au/book-a-treatment``   404
    ``https://skinessence.com.au/book-a-treatment/``  200
    ================================================  =====

    Both were checked live on 2026-09-10, after the crawler had recorded the
    slashless form as 404 and the loose comparison had matched it to a link
    written with the slash. The claim that would have gone out -- "the BOOK
    ONLINE button on your site leads to an error page" -- is false, and the
    recipient can see it is false in one click. That is the same failure as
    naming a probed URL, reached through the normalisation instead of through
    the crawl.

    Scheme and ``www`` are still folded: both were tested against these same
    hosts and neither changes the status.
    """
    split = urlsplit(url)
    host = split.netloc.lower().removeprefix("www.")
    return f"{host}{split.path}" + (f"?{split.query}" if split.query else "")


def _linked_urls(pages: list[PageEvidence]) -> set[str]:
    """Every internal address a *working* page links to, excluding self-links.

    Both exclusions were found by running this against real crawls rather than
    by reasoning about it, and without either one the rule passes everything it
    was written to stop.

    **Error pages are not sources.** Most 404s in the wild are soft: the server
    answers 404 and renders the full site template, navigation and all. Every
    broken page in the live data carries around 200 nav links for that reason.
    Treating an error page's markup as evidence of what the site links to means
    the pages we invented start vouching for each other.

    **A link to the page it sits on proves nothing.** Those same templates
    carry ``href="#"`` and bare anchors, which resolve against the current
    address -- so a probed URL that does not exist would mark *itself* as
    linked. Measured: this alone made 17,441 of 30,440 crawled 404s look
    genuine, including all six of the paths the crawler had guessed.

    **An unsubstituted template token is not an address.** ``[#DSR_FORM_URL#]``
    appears on every page of a site whose CMS failed to fill it in; it 404s
    honestly, and quoting it back is impossible because canonicalisation drops
    the fragment and leaves ``.../[``. See :data:`_PLACEHOLDER_MARKERS`.
    """
    linked: set[str] = set()
    for page in pages:
        if page.http_status is not None and page.http_status >= 400:
            continue
        base = page.final_url or page.url
        try:
            here = _canonical(base)
        except ValueError:
            continue
        for link in page.nav_links:
            if link.is_external:
                continue
            href = (link.href or "").strip()
            if not href or href.startswith("#"):
                continue
            if _is_placeholder(href) or _is_bare_address(href):
                # Real markup, real 404, and still not a claim we can make:
                # see _PLACEHOLDER_MARKERS and _is_bare_address.
                continue
            try:
                absolute = urljoin(base, href)
                target = _canonical(absolute)
            except ValueError:
                # A malformed href is not evidence of anything. Skipped rather
                # than raised: one unparseable link must not lose the page.
                continue
            if target == here:
                continue
            # Same host as the page carrying the link, checked here rather than
            # trusted from the worker's ``is_external`` flag.
            #
            # That flag missed ``accounts.shopify.com/login/external/...``,
            # which is Shopify's login page and answers 404 to anyone not
            # mid-flow. Reported as an internal link it becomes "a link on your
            # site is broken" about somebody else's infrastructure. The crawler
            # also drifts across domains legitimately -- a .co.uk redirecting
            # to its .com, an IDN to its punycode -- so the test is against the
            # page the link was written on, not against the seed.
            if target.split("/")[0] != here.split("/")[0]:
                continue
            # Recorded in the exact form it was written. Whether this address
            # is the page we are standing on is a loose question; whether we
            # fetched it is not -- see _canonical_strict.
            linked.add(_canonical_strict(absolute))
    return linked


def _broken_internal_links(pages: list[PageEvidence]) -> DetectedFinding | None:
    """Pages that are gone *and* that something on the site still points at.

    The second half is the whole rule. This detector used to report any crawled
    page that answered 404, which is not the same question -- because the
    crawler reaches most of its URLs by guessing them. It tries ``/book``,
    ``/booking``, ``/appointments``, ``/fees`` and friends against every domain,
    and on a site that never had those pages all four come back 404.

    The finding that produced said, in the recipient's own inbox: *"The link is
    still on the page but the address behind it no longer resolves."* There was
    no link. We had invented the address, failed to find it, and reported the
    absence as the business's defect.

    Measured on the live workspace before this existed: of 31,399 crawled pages
    answering 404 or 410, **31,357 sat at depth 1 -- the probe list -- and 37
    had been reached by following a real link**. 277 messages had gone out
    carrying the sentence above; 156 of them named a booking path nothing on
    the site linked to. One went to a practice whose booking worked perfectly.

    So the claim is now verified the way it is worded: some page we read has to
    carry a link to the address before we will say the link is broken.
    """
    linked = _linked_urls(pages)

    # An address we also saw working is not an address we may call broken.
    #
    # ``whitesmileancoats.com/contact`` answered 404 at 14:57:08 and 200 at
    # 14:57:18 on the same day, and alternated between the two across dozens of
    # fetches over three weeks. Sites do this: rate limiting, a bot defence, a
    # flaky origin. One recorded 404 is one sample, and where the crawl holds a
    # success for the same address the honest reading is that the page is
    # there.
    seen_working = {
        _canonical_strict(u)
        for p in pages
        if p.http_status is not None and p.http_status < 400
        for u in (p.url, p.final_url)
        if u
    }

    broken = [
        (p.url, p.http_status)
        for p in pages
        if p.http_status is not None
        and p.http_status in BROKEN_LINK_STATUSES
        and (
            _canonical_strict(p.url) in linked
            or (p.final_url and _canonical_strict(p.final_url) in linked)
        )
        and _canonical_strict(p.url) not in seen_working
        and not (p.final_url and _canonical_strict(p.final_url) in seen_working)
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
    """No way to book or schedule anywhere on the site we could read.

    `pages` is already only the pages that loaded -- `detect_findings` filters
    site rules that way. What it does not check is how many, and "no booking
    form anywhere on your site" read off one page is a claim about our crawl
    wearing the clothes of a claim about their business.
    """
    if len(pages) < MIN_PAGES_FOR_ABSENCE:
        return None
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
    """No phone number on any page we could read.

    The strongest over-claim of the three: the copy says "there is no number
    anywhere a visitor can see", which is about the site, not the sample. 114
    of these were computed from a single page.
    """
    if len(pages) < MIN_PAGES_FOR_ABSENCE:
        return None
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
        # Measured by the page's own PerformanceObserver during the crawl, not
        # by Lighthouse -- Titan does not run it, and naming a tool that never
        # ran would put a false provenance behind a number shown to a stranger.
        verification_method=VerificationMethod.BROWSER_NAVIGATION,
        page_url=page.final_url,
        observed_value=f"LCP {lcp:.0f}ms",
        expected_behavior="Largest contentful paint under 2.5s",
        business_impact="The main content takes several seconds to appear",
        recommended_solution="Compress images, defer non-critical scripts, enable caching",
        estimated_effort=MEDIUM,
        evidence=((f"Largest contentful paint = {lcp:.0f}ms", page.final_url),),
    )


PAGE_RULES = (
    _missing_viewport,
    _missing_meta_description,
    _images_missing_alt,
    _console_errors,
    _failed_requests,
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

    # Both of these need the error pages to be visible, so they run over
    # everything rather than over ok_pages: the defect they describe is a
    # working page pointing at a broken one.
    for whole_site_rule in (_broken_internal_links, _broken_cta):
        found = whole_site_rule(result.pages)
        if found is not None:
            findings.setdefault(found.fingerprint, found)

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
