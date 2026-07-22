import re
from typing import Any, Dict


class PIIRedactor:
    """
    Automatically detects and masks Personally Identifiable Information (PII)
    and secrets before they are logged to the database or broadcasted.
    """

    # Simple regex patterns for demonstration.
    # In production, use Microsoft Presidio or specialized NER models.
    PATTERNS = {
        "SSN": r"\b\d{3}-\d{2}-\d{4}\b",
        "CREDIT_CARD": r"\b(?:\d[ -]*?){13,16}\b",
        "API_KEY": r"(?i)(?:api_key|token|secret)['\"]?\s*[:=]\s*['\"]?([a-zA-Z0-9_\-]{16,})['\"]?",
        "BEARER_TOKEN": r"(?i)Bearer\s+[a-zA-Z0-9_\-\.]+",
    }

    @classmethod
    def redact_string(cls, text: str) -> str:
        """Redacts PII from a single string."""
        if not isinstance(text, str):
            return text

        redacted = text
        for name, pattern in cls.PATTERNS.items():
            if name == "API_KEY":
                # We only want to redact the matching group (the actual key), not the whole string
                redacted = re.sub(
                    pattern,
                    lambda m: m.group(0).replace(m.group(1), "[REDACTED_SECRET]"),
                    redacted,
                )
            else:
                redacted = re.sub(pattern, f"[REDACTED_{name}]", redacted)
        return redacted

    @classmethod
    def redact_dict(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """Recursively redacts PII from a dictionary payload."""
        redacted_data = {}
        for k, v in data.items():
            if isinstance(v, str):
                redacted_data[k] = cls.redact_string(v)
            elif isinstance(v, dict):
                redacted_data[k] = cls.redact_dict(v)
            elif isinstance(v, list):
                redacted_data[k] = [
                    (
                        cls.redact_dict(i)
                        if isinstance(i, dict)
                        else (cls.redact_string(i) if isinstance(i, str) else i)
                    )
                    for i in v
                ]
            else:
                redacted_data[k] = v
        return redacted_data
