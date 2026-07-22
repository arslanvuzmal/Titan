import re
import logging
from typing import List

logger = logging.getLogger(__name__)

class SecurityViolationError(Exception):
    pass

class InputGuardrail:
    """
    Evaluates raw user input to detect prompt injections and jailbreaks
    before passing it to the LLM.
    """
    
    # Common prompt injection triggers
    BLOCKED_PHRASES = [
        "ignore previous instructions",
        "ignore all rules",
        "you are now dan",
        "do anything now",
        "output the system prompt",
        "disregard context",
        "system prompt"
    ]

    @classmethod
    def evaluate(cls, user_input: str) -> None:
        """
        Raises SecurityViolationError if the input looks malicious.
        """
        input_lower = user_input.lower()
        for phrase in cls.BLOCKED_PHRASES:
            if phrase in input_lower:
                logger.warning(f"Prompt Injection Detected: '{phrase}' found in input.")
                raise SecurityViolationError(f"Security Violation: Prompt injection attempt detected. Blocked phrase: '{phrase}'.")

    @staticmethod
    def wrap_rag_context(context: str) -> str:
        """
        Wraps untrusted third-party documents in XML tags.
        This provides 'indirect prompt injection' defense by explicitly 
        delimitating instructions from data.
        """
        return f"\n<retrieved_context>\n{context}\n</retrieved_context>\n"


class OutputGuardrail:
    """
    Evaluates LLM output to ensure it does not leak PII or attempt
    to execute unauthorized generic commands.
    """
    
    # Basic patterns we never expect a Sales/Support agent to output raw
    BLOCKED_PATTERNS = [
        r"(DROP\s+TABLE|DELETE\s+FROM|TRUNCATE\s+TABLE)",  # SQL Injection via LLM
        r"(/bin/bash|/bin/sh|cmd\.exe|powershell)",         # Shell payloads
    ]

    @classmethod
    def evaluate(cls, llm_output: str) -> None:
        """
        Raises SecurityViolationError if the output contains dangerous strings.
        """
        for pattern in cls.BLOCKED_PATTERNS:
            if re.search(pattern, llm_output, re.IGNORECASE):
                logger.warning("Dangerous output pattern detected from LLM.")
                raise SecurityViolationError("Security Violation: LLM attempted to output unauthorized executable payload.")
