"""Message policy validation.

The last thing standing between a generated draft and a real person's inbox.
Mission section 12.2 lists what must be rejected; this module implements it and
returns *every* violation so a reviewer sees the whole picture.

The central rule: every sentence that asserts a fact about the recipient's
business must map, through ``claim_map``, to a finding backed by evidence. A
sentence with no such mapping is treated as an unsupported claim and blocks the
draft -- not because the model is untrustworthy in general, but because a
confident sentence about a stranger's website is only worth sending if it is
demonstrably true.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

MAX_SUBJECT_CHARS = 120
MAX_BODY_CHARS = 2200
MAX_BODY_WORDS = 320
MIN_BODY_WORDS = 40


class ViolationCode(StrEnum):
    UNSUPPORTED_CLAIM = "unsupported_claim"
    MISSING_EVIDENCE = "missing_evidence"
    FABRICATED_METRIC = "fabricated_metric"
    INVENTED_NAME = "invented_name"
    FALSE_RELATIONSHIP = "implies_existing_relationship"
    WORK_NOT_PERFORMED = "implies_work_already_performed"
    FALSE_URGENCY = "false_urgency"
    FEAR_APPEAL = "fear_based_manipulation"
    EXCESSIVE_PRAISE = "exaggerated_praise"
    AI_SPAM_LANGUAGE = "ai_spam_language"
    MULTIPLE_OFFERS = "multiple_unrelated_offers"
    TOO_LONG = "message_too_long"
    TOO_SHORT = "message_too_short"
    SUBJECT_TOO_LONG = "subject_too_long"
    DECEPTIVE_SUBJECT = "deceptive_subject"
    MISSING_FOOTER = "missing_required_footer"
    MISSING_MAILING_ADDRESS = "missing_mailing_address"
    MISSING_UNSUBSCRIBE = "missing_unsubscribe"
    MISSING_SENDER_IDENTITY = "missing_sender_identity"
    WRONG_PORTFOLIO_URL = "wrong_or_missing_portfolio_url"
    ACRONYM_MANGLED = "acronym_mangled"
    PLACEHOLDER_LEFT = "unfilled_placeholder"
    DUPLICATE_RECENT = "duplicate_of_recent_message"
    FOLLOWUP_ADDS_NOTHING = "followup_adds_no_new_evidence"
    INJECTION_ECHO = "echoes_untrusted_page_instructions"


@dataclass(frozen=True, slots=True)
class Violation:
    code: ViolationCode
    detail: str
    excerpt: str | None = None

    def __str__(self) -> str:
        return f"{self.code.value}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ValidationReport:
    passed: bool
    violations: tuple[Violation, ...] = ()
    #: Sentences that made a factual claim and were successfully traced.
    supported_sentences: tuple[str, ...] = ()
    word_count: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "word_count": self.word_count,
            "violations": [
                {"code": v.code.value, "detail": v.detail, "excerpt": v.excerpt}
                for v in self.violations
            ],
            "supported_sentences": list(self.supported_sentences),
        }


@dataclass(slots=True)
class MessageContext:
    subject: str
    body: str
    #: [{sentence, claim, finding_id, evidence_ids: [...], source_url}]
    claim_map: list[dict[str, Any]]
    #: Finding IDs that actually exist and carry >=1 evidence row.
    evidenced_finding_ids: frozenset[str]
    sender_name: str
    portfolio_url: str
    mailing_address: str | None
    unsubscribe_present: bool
    #: Names Titan actually knows, so an invented one can be spotted.
    known_names: frozenset[str] = field(default_factory=frozenset)
    #: Findings cited by previous messages in this sequence.
    previously_cited_finding_ids: frozenset[str] = field(default_factory=frozenset)
    is_followup: bool = False
    requires_new_evidence: bool = False
    recent_body_hashes: frozenset[str] = field(default_factory=frozenset)
    #: Text harvested from the prospect's site. Used to detect a model echoing
    #: injected instructions back into the message.
    untrusted_page_text: str = ""


# --------------------------------------------------------------------------
# Detectors
# --------------------------------------------------------------------------

#: A sentence is treated as making a factual claim about the recipient when it
#: asserts something observable about their site or business.
_CLAIM_MARKERS = re.compile(
    r"\b(your|your's|yours|the)\b.{0,60}\b("
    r"site|website|homepage|page|form|button|link|booking|checkout|"
    r"navigation|menu|contact|listing|reviews?|load(?:s|ing)?|speed|"
    r"mobile|images?|cta"
    r")\b",
    re.IGNORECASE,
)

#: Numbers presented as measured outcomes. Titan measures page facts, not
#: business results, so a revenue/conversion figure is fabricated by definition.
_METRIC_PATTERNS = (
    re.compile(
        r"\b\d{1,3}%\s*(?:more|increase|uplift|boost|growth|conversion|revenue)", re.I
    ),
    re.compile(
        r"\b(?:increase|boost|grow|double|triple)\w*\s+(?:your\s+)?\w*\s*(?:revenue|sales|leads|bookings|conversions?)\s+by\s+\d",
        re.I,
    ),
    re.compile(r"\b(?:losing|lost|missing out on)\s+[$£€]\s?[\d,]+", re.I),
    re.compile(r"\b[$£€]\s?[\d,]{3,}\s*(?:per|/)\s*(?:month|year|week)", re.I),
    re.compile(r"\b\d+x\s+(?:more|your)\b", re.I),
)

_FALSE_RELATIONSHIP = (
    re.compile(
        r"\b(?:as (?:we|I) discussed|per our (?:call|conversation|meeting)|following up on our|great (?:speaking|talking) with you|as promised|circling back on our)\b",
        re.I,
    ),
    re.compile(r"\b(?:you (?:signed up|requested|asked for|downloaded))\b", re.I),
    re.compile(r"\b(?:thanks for (?:reaching out|your enquiry|your interest))\b", re.I),
)

_WORK_PERFORMED = (
    re.compile(
        r"\b(?:I|we)\s+(?:ran|completed|performed|conducted|did)\s+(?:a\s+)?(?:full\s+)?(?:audit|analysis|review|assessment)\s+of\s+your\b",
        re.I,
    ),
    re.compile(
        r"\b(?:I|we)\s+(?:already\s+)?(?:built|created|designed|fixed|rebuilt|redesigned)\s+(?:you|your)\b",
        re.I,
    ),
    re.compile(r"\battached is your (?:full |complete )?(?:report|audit)\b", re.I),
)

_FALSE_URGENCY = (
    re.compile(
        r"\b(?:act now|urgent|expires? (?:today|tomorrow|in \d)|last chance|final (?:notice|reminder|warning)|only \d+ (?:spots?|places?|slots?) (?:left|remaining)|limited time offer|24 hours only|don'?t miss out)\b",
        re.I,
    ),
    re.compile(r"\b(?:this (?:offer|price) (?:ends|expires))\b", re.I),
)

_FEAR_APPEAL = (
    re.compile(
        r"\b(?:you(?:'re| are) losing (?:customers|clients|money|business)|your competitors are (?:already |now )?(?:beating|ahead of|stealing)|before it'?s too late|costing you (?:thousands|customers|clients)|bleeding (?:money|revenue|customers))\b",
        re.I,
    ),
    re.compile(
        r"\b(?:google will (?:penalise|penalize|drop|derank)|you'?ll (?:be )?(?:left behind|fall behind))\b",
        re.I,
    ),
)

_EXCESSIVE_PRAISE = (
    re.compile(
        r"\b(?:absolutely (?:stunning|amazing|incredible)|blown away|(?:truly |simply )?(?:phenomenal|outstanding|world.?class|best.in.class)|I love what you'?(?:ve|re) (?:done|doing)|huge fan of)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:your (?:website|site|brand) is (?:beautiful|gorgeous|stunning|amazing|incredible))\b",
        re.I,
    ),
)

_AI_SPAM = (
    re.compile(
        r"\b(?:I hope this (?:email|message) finds you well|I trust this (?:email|message) finds you)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:in today'?s (?:fast.paced|digital|competitive) (?:world|landscape|market))\b",
        re.I,
    ),
    re.compile(
        r"\b(?:leverage (?:the power of|cutting.edge)|revolutionise your|revolutionize your|game.?changer|synerg(?:y|ies|istic)|unlock (?:the|your) (?:full )?potential)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:as an AI|I am an AI|this (?:email|message) was (?:generated|written) by AI)\b",
        re.I,
    ),
    re.compile(r"\b(?:your business could (?:really )?(?:use|benefit from) AI)\b", re.I),
)

_PLACEHOLDER = re.compile(
    r"(\{\{[^}]*\}\}|\[(?:FIRST_?NAME|COMPANY|BUSINESS|NAME|CITY|X|INSERT[^\]]*)\]|<<[^>]+>>|TODO:|FIXME:|\bLorem ipsum\b)",
    re.IGNORECASE,
)

_INJECTION_ECHO = (
    re.compile(r"\bignore (?:all )?previous instructions\b", re.I),
    re.compile(r"\b(?:system prompt|you are now an? (?:unrestricted|new))\b", re.I),
    re.compile(r"\bskip approval\b", re.I),
)

#: Acronyms whose casing must survive generation intact (mission 12.1).
#: Acronyms whose casing must survive generation intact. HTTPS/URL/LCP are
#: deliberately absent: they appear lowercase inside legitimate links and paths,
#: so checking them produces false positives on correct messages.
_ACRONYMS = ("SEO", "CTA", "CRM", "API", "HVAC", "GDPR", "SPF", "DKIM", "DMARC", "ROI")

#: URLs are removed before the acronym check for the same reason.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _sentences(text: str) -> list[str]:
    """Split into sentences, tolerating hard-wrapped lines.

    A naive split on ``\\n`` breaks a wrapped sentence into fragments, none of
    which then matches its claim_map entry -- which would reject every
    well-formed message. Paragraph breaks (blank lines) do end a sentence;
    single newlines inside a paragraph do not.
    """
    out: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        unwrapped = re.sub(r"[ \t]*\n[ \t]*", " ", paragraph).strip()
        if not unwrapped:
            continue
        out.extend(
            part.strip() for part in re.split(r"(?<=[.!?])\s+", unwrapped) if part.strip()
        )
    return out


def _scan(
    patterns: tuple[re.Pattern[str], ...],
    text: str,
    code: ViolationCode,
    detail: str,
) -> list[Violation]:
    out: list[Violation] = []
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            out.append(Violation(code, detail, match.group(0)[:160]))
            break  # one violation per category is enough to block
    return out


def validate_message(ctx: MessageContext) -> ValidationReport:
    """Check a draft against every message policy rule."""
    violations: list[Violation] = []
    body = ctx.body or ""
    subject = ctx.subject or ""
    words = len(body.split())

    # ---- length and shape ------------------------------------------------
    if len(subject) > MAX_SUBJECT_CHARS:
        violations.append(
            Violation(
                ViolationCode.SUBJECT_TOO_LONG,
                f"{len(subject)} chars > {MAX_SUBJECT_CHARS}",
            )
        )
    if not subject.strip():
        violations.append(Violation(ViolationCode.DECEPTIVE_SUBJECT, "subject is empty"))
    if re.match(r"^\s*(re|fwd?)\s*:", subject, re.I):
        violations.append(
            Violation(
                ViolationCode.DECEPTIVE_SUBJECT,
                "subject fakes a reply or forward to a conversation that never happened",
                subject[:80],
            )
        )
    if len(body) > MAX_BODY_CHARS or words > MAX_BODY_WORDS:
        violations.append(
            Violation(ViolationCode.TOO_LONG, f"{words} words / {len(body)} chars")
        )
    if words < MIN_BODY_WORDS:
        violations.append(Violation(ViolationCode.TOO_SHORT, f"only {words} words"))

    # ---- required footer elements ----------------------------------------
    if ctx.sender_name and ctx.sender_name.lower() not in body.lower():
        violations.append(
            Violation(
                ViolationCode.MISSING_SENDER_IDENTITY,
                f"'{ctx.sender_name}' does not appear",
            )
        )
    if ctx.portfolio_url not in body:
        violations.append(
            Violation(
                ViolationCode.WRONG_PORTFOLIO_URL,
                f"expected portfolio link {ctx.portfolio_url}",
            )
        )
    mailing_address = (ctx.mailing_address or "").strip()
    if not mailing_address:
        violations.append(
            Violation(
                ViolationCode.MISSING_MAILING_ADDRESS,
                "no physical mailing address configured; required in commercial email",
            )
        )
    elif mailing_address not in body:
        violations.append(
            Violation(
                ViolationCode.MISSING_FOOTER, "mailing address is not present in the body"
            )
        )
    if not ctx.unsubscribe_present:
        violations.append(
            Violation(ViolationCode.MISSING_UNSUBSCRIBE, "no unsubscribe mechanism")
        )

    # ---- prohibited rhetoric ---------------------------------------------
    violations += _scan(
        _METRIC_PATTERNS,
        body,
        ViolationCode.FABRICATED_METRIC,
        "quantified business outcome Titan cannot have measured",
    )
    violations += _scan(
        _FALSE_RELATIONSHIP,
        body,
        ViolationCode.FALSE_RELATIONSHIP,
        "implies a prior conversation that did not happen",
    )
    violations += _scan(
        _WORK_PERFORMED,
        body,
        ViolationCode.WORK_NOT_PERFORMED,
        "implies work was performed for the recipient",
    )
    violations += _scan(
        _FALSE_URGENCY, body, ViolationCode.FALSE_URGENCY, "manufactured deadline"
    )
    violations += _scan(
        _FEAR_APPEAL, body, ViolationCode.FEAR_APPEAL, "fear-based framing"
    )
    violations += _scan(
        _EXCESSIVE_PRAISE, body, ViolationCode.EXCESSIVE_PRAISE, "exaggerated flattery"
    )
    violations += _scan(
        _AI_SPAM, body, ViolationCode.AI_SPAM_LANGUAGE, "generic AI-outreach phrasing"
    )
    violations += _scan(
        _INJECTION_ECHO,
        body,
        ViolationCode.INJECTION_ECHO,
        "echoes instructions injected into a crawled page",
    )

    placeholder = _PLACEHOLDER.search(body) or _PLACEHOLDER.search(subject)
    if placeholder:
        violations.append(
            Violation(
                ViolationCode.PLACEHOLDER_LEFT,
                "template placeholder not filled",
                placeholder.group(0)[:80],
            )
        )

    # ---- acronym integrity -------------------------------------------------
    prose = _URL_RE.sub(" ", body)
    for acronym in _ACRONYMS:
        # Flag a lowercase rendering that is not part of a longer word.
        bad = re.search(rf"(?<![A-Za-z]){acronym.lower()}(?![A-Za-z])", prose)
        if bad and acronym not in prose:
            violations.append(
                Violation(
                    ViolationCode.ACRONYM_MANGLED,
                    f"{acronym} written as {bad.group(0)!r}",
                )
            )

    # ---- claim traceability ------------------------------------------------
    supported, claim_violations = _validate_claims(ctx)
    violations += claim_violations

    # ---- offer focus -------------------------------------------------------
    distinct_findings = {
        str(entry.get("finding_id")) for entry in ctx.claim_map if entry.get("finding_id")
    }
    if len(distinct_findings) > 3:
        violations.append(
            Violation(
                ViolationCode.MULTIPLE_OFFERS,
                f"{len(distinct_findings)} separate findings cited; a first message "
                "should lead with one observation",
            )
        )

    # ---- follow-up rules ----------------------------------------------------
    if ctx.is_followup and ctx.requires_new_evidence:
        new_findings = distinct_findings - ctx.previously_cited_finding_ids
        if not new_findings:
            violations.append(
                Violation(
                    ViolationCode.FOLLOWUP_ADDS_NOTHING,
                    "follow-up cites only findings already used in an earlier message",
                )
            )

    # ---- duplicate detection ------------------------------------------------
    import hashlib

    body_hash = hashlib.sha256(
        re.sub(r"\s+", " ", body.strip().lower()).encode("utf-8")
    ).hexdigest()
    if body_hash in ctx.recent_body_hashes:
        violations.append(
            Violation(ViolationCode.DUPLICATE_RECENT, "identical body sent recently")
        )

    return ValidationReport(
        passed=not violations,
        violations=tuple(violations),
        supported_sentences=tuple(supported),
        word_count=words,
    )


def _validate_claims(ctx: MessageContext) -> tuple[list[str], list[Violation]]:
    """Every factual sentence must trace to an evidenced finding."""
    violations: list[Violation] = []
    supported: list[str] = []

    mapped: dict[str, dict[str, Any]] = {}
    for entry in ctx.claim_map:
        sentence = str(entry.get("sentence", "")).strip()
        if sentence:
            mapped[_normalize(sentence)] = entry

    for sentence in _sentences(ctx.body):
        if not _CLAIM_MARKERS.search(sentence):
            continue  # not a factual claim about the recipient
        key = _normalize(sentence)
        claim = mapped.get(key)
        if claim is None:
            violations.append(
                Violation(
                    ViolationCode.UNSUPPORTED_CLAIM,
                    "sentence asserts something about the recipient's site but is "
                    "not present in claim_map",
                    sentence[:160],
                )
            )
            continue

        finding_id = str(claim.get("finding_id") or "")
        evidence_ids = [str(e) for e in (claim.get("evidence_ids") or [])]
        if not finding_id:
            violations.append(
                Violation(
                    ViolationCode.MISSING_EVIDENCE,
                    "claim has no finding_id",
                    sentence[:160],
                )
            )
        elif finding_id not in ctx.evidenced_finding_ids:
            violations.append(
                Violation(
                    ViolationCode.MISSING_EVIDENCE,
                    f"finding {finding_id} does not exist or has no evidence rows",
                    sentence[:160],
                )
            )
        elif not evidence_ids:
            violations.append(
                Violation(
                    ViolationCode.MISSING_EVIDENCE,
                    "claim cites no evidence ids",
                    sentence[:160],
                )
            )
        else:
            supported.append(sentence)

    # A name Titan does not know must not appear in a greeting.
    # Case-insensitive on the salutation itself: without re.IGNORECASE the
    # ordinary capitalised "Hi Jonathan," never matched, so an invented name
    # passed straight through.
    greeting = re.match(
        r"^\s*(?:hi|hello|hey|dear)\s+([A-Z][a-z]{1,20})\b", ctx.body, re.IGNORECASE
    )
    if greeting:
        name = greeting.group(1)
        if ctx.known_names and name not in ctx.known_names:
            violations.append(
                Violation(
                    ViolationCode.INVENTED_NAME,
                    f"greeting addresses {name!r}, which is not a name Titan recorded",
                    greeting.group(0),
                )
            )

    return supported, violations


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower()).rstrip(".!?")


__all__ = [
    "MAX_BODY_WORDS",
    "MessageContext",
    "ValidationReport",
    "Violation",
    "ViolationCode",
    "validate_message",
]
