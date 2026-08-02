"""Redaction for logs, traces, audit entries, and API error bodies.

Ported and hardened from the pre-0.2 ``app.security.redaction`` (gap analysis
K-02), which had the right shape but three gaps that mattered:

* it was never wired into logging, so nothing was actually redacted (H-19);
* key-based redaction was absent -- a value only got masked if it appeared
  inside a ``key=value`` string, so ``{"api_key": "sk-live-..."}`` passed
  through untouched;
* the credit-card pattern ``(?:\\d[ -]*?){13,16}`` matched almost any long digit
  run, including UUID fragments and phone numbers.

Invariant 19 depends on this module: API keys must never reach logs or
responses.
"""

from __future__ import annotations

import re
from typing import Any

#: Keys whose *value* is always replaced wholesale, regardless of shape.
#: Matched case-insensitively against the key with separators normalized, so
#: "API-Key", "api_key", and "apiKey" all match.
SENSITIVE_KEY_PARTS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "apikey",
        "api_key",
        "accesskey",
        "authorization",
        "auth",
        "cookie",
        "setcookie",
        "credential",
        "privatekey",
        "clientsecret",
        "webhooksecret",
        "signingsecret",
        "sessionid",
        "refreshtoken",
        "encryptedcredential",
        "jwt",
        "bearer",
    }
)

REDACTED = "[REDACTED]"

_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "BEARER",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-]{8,}=*"),
        "Bearer [REDACTED]",
    ),
    # Provider key shapes, most-specific first.
    ("OPENAI", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "[REDACTED_KEY]"),
    ("RESEND", re.compile(r"\bre_[A-Za-z0-9_\-]{16,}"), "[REDACTED_KEY]"),
    (
        "SENDGRID",
        re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"),
        "[REDACTED_KEY]",
    ),
    ("GOOGLE", re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}"), "[REDACTED_KEY]"),
    ("SLACK", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{8,}"), "[REDACTED_KEY]"),
    ("NVIDIA", re.compile(r"\bnvapi-[A-Za-z0-9_\-]{16,}"), "[REDACTED_KEY]"),
    (
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (
        "JWT",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
        "[REDACTED_JWT]",
    ),
    # key=value / key: "value" forms not covered by key-based redaction because
    # they are embedded in free text (e.g. a stringified command line).
    (
        "INLINE_SECRET",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*[\"']?([^\s\"',;]{8,})"
        ),
        None,  # handled specially: only the value group is replaced
    ),
    (
        "SSN",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "[REDACTED_SSN]",
    ),
    # Anchored on standard card prefixes and length, with optional separators.
    # Far narrower than the previous "13-16 digits with anything between".
    (
        "CARD",
        re.compile(
            r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))"
            r"(?:[ -]?\d{4}){2,3}\b"
        ),
        "[REDACTED_CARD]",
    ),
)

_MAX_STRING = 4096


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def is_sensitive_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return any(part.replace("_", "") in normalized for part in SENSITIVE_KEY_PARTS)


def redact_string(value: str) -> str:
    """Mask secrets and PII inside a free-text string."""
    if not isinstance(value, str):
        return value
    out = value
    for name, pattern, replacement in _PATTERNS:
        if name == "INLINE_SECRET":
            out = pattern.sub(lambda m: m.group(0).replace(m.group(2), REDACTED), out)
        else:
            assert replacement is not None
            out = pattern.sub(replacement, out)
    if len(out) > _MAX_STRING:
        # Log injection defence: bound the length and strip control characters
        # so an attacker-supplied field cannot forge additional log lines.
        out = out[:_MAX_STRING] + "...[truncated]"
    return _strip_control(out)


def _strip_control(value: str) -> str:
    """Remove characters that could forge log records or terminal sequences."""
    return (
        re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact a JSON-like structure.

    Depth-limited so a hostile deeply-nested payload cannot exhaust the stack.
    """
    if _depth > 12:
        return "[REDACTED_DEPTH_LIMIT]"
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            out[key] = REDACTED if is_sensitive_key(key) else redact(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_string(str(value))


__all__ = [
    "REDACTED",
    "SENSITIVE_KEY_PARTS",
    "is_sensitive_key",
    "redact",
    "redact_string",
]
