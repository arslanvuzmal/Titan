"""The wire contract between the browser worker and the control plane.

The browser worker is a separate, credential-free service (mission section 4.3).
Everything it returns is **untrusted input**: it has just executed JavaScript
written by a stranger. So this contract is validated strictly on ingest, the
URLs are re-checked against the SSRF guard server-side, and no field is ever
interpolated into a model's instruction channel.

The TypeScript side of this contract lives in
``apps/browser-worker/src/contract.ts``. A drift test compares the two.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONTRACT_VERSION = "1.0.0"

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class StrictModel(BaseModel):
    # Reject unknown fields: a worker sending something we do not model is a
    # version mismatch or a compromise, and either way should fail loudly.
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResearchRequest(StrictModel):
    """A bounded unit of work. The worker may do nothing outside these limits."""

    request_id: str
    seed_url: str
    max_pages: int = Field(default=12, ge=1, le=200)
    max_depth: int = Field(default=2, ge=0, le=5)
    timeout_seconds: int = Field(default=120, ge=5, le=900)
    max_response_bytes: int = Field(default=5_000_000, ge=10_000)
    max_redirects: int = Field(default=5, ge=0, le=20)
    user_agent: str
    respect_robots: bool = True
    capture_screenshots: bool = True
    run_lighthouse: bool = True
    run_axe: bool = True
    #: Paths the playbook wants inspected, e.g. ["/contact", "/booking"].
    priority_paths: list[str] = Field(default_factory=list, max_length=40)
    #: Addresses the control plane already vetted. The worker pins to these
    #: instead of re-resolving, which is what closes the DNS-rebinding window.
    pinned_ips: list[str] = Field(default_factory=list, max_length=16)


class LinkObservation(StrictModel):
    text: str | None = None
    href: str
    is_external: bool = False
    #: Populated only for links the worker actually probed (HEAD/GET, bounded).
    status: int | None = None
    is_broken: bool | None = None


class FormObservation(StrictModel):
    selector: str
    action: str | None = None
    method: str | None = None
    field_count: int = 0
    #: Field *names* only. Values are never captured, and the worker never
    #: submits a form (mission section 7.3).
    field_names: list[str] = Field(default_factory=list, max_length=60)
    has_submit: bool = False


class CtaObservation(StrictModel):
    selector: str
    text: str | None = None
    href: str | None = None
    is_visible: bool = True
    #: Result of *navigating* to the CTA target in a throwaway context.
    target_status: int | None = None
    target_is_empty: bool | None = None


class AccessibilityViolation(StrictModel):
    rule_id: str
    impact: Literal["minor", "moderate", "serious", "critical"] | None = None
    description: str | None = None
    node_count: int = 0
    sample_selector: str | None = None


class PerformanceMetrics(StrictModel):
    performance_score: Confidence | None = None
    accessibility_score: Confidence | None = None
    seo_score: Confidence | None = None
    best_practices_score: Confidence | None = None
    largest_contentful_paint_ms: float | None = None
    total_blocking_time_ms: float | None = None
    cumulative_layout_shift: float | None = None
    speed_index_ms: float | None = None


class SecurityHeaders(StrictModel):
    strict_transport_security: str | None = None
    content_security_policy: str | None = None
    x_content_type_options: str | None = None
    x_frame_options: str | None = None
    referrer_policy: str | None = None
    has_mixed_content: bool = False


class PageEvidence(StrictModel):
    url: str
    final_url: str
    depth: int = 0
    http_status: int | None = None
    content_type: str | None = None
    title: str | None = None
    meta_description: str | None = None
    canonical_url: str | None = None
    robots_meta: str | None = None
    lang: str | None = None
    has_viewport_meta: bool = False

    headings: list[str] = Field(default_factory=list, max_length=80)
    nav_links: list[LinkObservation] = Field(default_factory=list, max_length=300)
    forms: list[FormObservation] = Field(default_factory=list, max_length=40)
    ctas: list[CtaObservation] = Field(default_factory=list, max_length=60)

    visible_emails: list[str] = Field(default_factory=list, max_length=30)
    visible_phones: list[str] = Field(default_factory=list, max_length=30)
    booking_links: list[str] = Field(default_factory=list, max_length=30)
    contact_links: list[str] = Field(default_factory=list, max_length=30)
    social_links: list[str] = Field(default_factory=list, max_length=40)
    review_links: list[str] = Field(default_factory=list, max_length=30)

    structured_data_types: list[str] = Field(default_factory=list, max_length=40)
    technologies: list[str] = Field(default_factory=list, max_length=60)
    console_errors: list[str] = Field(default_factory=list, max_length=60)
    failed_requests: list[str] = Field(default_factory=list, max_length=60)
    images_missing_alt: int = 0
    image_count: int = 0
    has_chat_widget: bool = False
    has_cookie_obstruction: bool = False
    security_headers: SecurityHeaders | None = None
    accessibility_violations: list[AccessibilityViolation] = Field(
        default_factory=list, max_length=80
    )
    performance: PerformanceMetrics | None = None

    #: Sanitised visible text, hard-truncated. ALWAYS untrusted.
    text_excerpt: str | None = Field(default=None, max_length=20_000)
    word_count: int = 0
    captured_at: dt.datetime

    @field_validator("text_excerpt")
    @classmethod
    def _strip_control_chars(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return "".join(ch for ch in v if ch == "\n" or ch == "\t" or ord(ch) >= 0x20)

    def content_fingerprint(self) -> str:
        """Stable hash of the *measured content*.

        Volatile fields -- capture time, worker id, session id, storage path --
        are excluded by construction, so a retried crawl of an unchanged page
        produces the same fingerprint and does not create duplicate evidence
        (mission section 7.4).
        """
        return fingerprint(
            self.model_dump(mode="json", exclude={"captured_at", "performance"})
        )


class ArtifactRef(StrictModel):
    kind: Literal[
        "screenshot_mobile",
        "screenshot_desktop",
        "lighthouse",
        "axe",
        "console",
        "network_failures",
        "headers",
    ]
    media_type: str
    #: Relative storage key. Validated against traversal on ingest.
    storage_key: str | None = None
    payload: dict[str, Any] | None = None
    byte_size: int | None = None
    content_fingerprint: str
    page_url: str | None = None


class CrawlResult(StrictModel):
    contract_version: str = CONTRACT_VERSION
    request_id: str
    status: Literal["completed", "blocked", "failed", "partial"]
    seed_url: str
    final_url: str | None = None
    redirect_chain: list[str] = Field(default_factory=list, max_length=25)
    blocked_reason: str | None = None
    failure_reason: str | None = None
    robots_allowed: bool | None = None
    pages: list[PageEvidence] = Field(default_factory=list, max_length=200)
    artifacts: list[ArtifactRef] = Field(default_factory=list, max_length=400)
    pages_fetched: int = 0
    bytes_fetched: int = 0
    duration_ms: int = 0
    worker_version: str


def fingerprint(value: Any) -> str:
    """Deterministic sha256 over a JSON-serialisable value.

    Keys are sorted so that dictionary ordering cannot change the hash.
    """
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def finding_fingerprint(
    issue_type: str, page_url: str | None, selector: str | None, observed: str | None
) -> str:
    """Identity of a finding, so a re-run recognises the same issue."""
    return fingerprint(
        {
            "issue_type": issue_type,
            "page_url": (page_url or "").rstrip("/").lower(),
            "selector": selector or "",
            "observed": observed or "",
        }
    )


__all__ = [
    "CONTRACT_VERSION",
    "AccessibilityViolation",
    "ArtifactRef",
    "CrawlResult",
    "CtaObservation",
    "FormObservation",
    "LinkObservation",
    "PageEvidence",
    "PerformanceMetrics",
    "ResearchRequest",
    "SecurityHeaders",
    "finding_fingerprint",
    "fingerprint",
]
