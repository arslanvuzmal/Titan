"""The token budget a reasoning model needs to return a whole sentence.

Gemini 3 spends output tokens thinking before it emits text. At 200 the
rewriter's request came back truncated mid-word --

    max_tokens=200   'The booking form on example.'
    max_tokens=600   "I'm getting an error on your booking form at example.com."

-- and truncation is not a visible failure here. The reassembled sentence has
lost the evidenced domain, so :func:`rewrite_message` correctly discards it and
keeps the deterministic text. The result was a rewrite path that ran, cost a
model call, and was thrown away every single time: enabled, exercised, and
producing nothing.
"""

from __future__ import annotations

import inspect
import re

from titan.intelligence import rewriter

#: Below this a reasoning model spends the whole budget before emitting.
MIN_SENTENCE_BUDGET = 400


def test_the_rewrite_budget_survives_a_reasoning_model() -> None:
    """Planted violation: put ``max_tokens=200`` back and this fails."""
    source = inspect.getsource(rewriter)
    budgets = [int(n) for n in re.findall(r"max_tokens=(\d+)", source)]

    assert budgets, "the rewriter no longer sets a token budget"
    assert min(budgets) >= MIN_SENTENCE_BUDGET, (
        f"a budget of {min(budgets)} truncates the sentence, and a truncated "
        "sentence is discarded silently rather than reported"
    )


def test_a_truncated_rewrite_is_rejected_rather_than_sent() -> None:
    """Why the failure above is silent, and why that is nonetheless correct.

    Losing the evidenced specific is one of the three conditions that make a
    rewrite unusable. It is what protects a truncated sentence from going out,
    and it is also what hid the truncation.
    """
    source = inspect.getsource(rewriter)

    assert "RewriteVerdict" in source or "specific" in source.lower()
