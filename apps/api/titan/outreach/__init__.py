"""Outreach copy: what each sequence step says, and the evidence behind it.

Two modules, split along the line that matters. :mod:`titan.outreach.variables`
turns a verified finding into the handful of phrases a template may use, and
yields nothing at all for a finding it does not recognise.
:mod:`titan.outreach.sequence` holds the wording and the cadence.

Neither module performs I/O, calls a model, or knows which provider will carry
the result. That is what lets every rule in them be exercised exhaustively
without a database, and what keeps the words a recipient reads decided in one
readable place rather than assembled across a pipeline.
"""

from titan.outreach.sequence import (
    MAX_WORDS,
    MIN_WORDS,
    STEP_DELAYS_IN_DAYS,
    TEMPLATE_KEYS,
    compliance_footer,
    compose_first_email,
    compose_follow_up_1,
    compose_follow_up_2,
    compose_follow_up_3,
    count_words,
    rotate_subject,
    salutation,
    with_footer,
)
from titan.outreach.variables import FindingVariables, derive_variables

__all__ = [
    "MAX_WORDS",
    "MIN_WORDS",
    "STEP_DELAYS_IN_DAYS",
    "TEMPLATE_KEYS",
    "FindingVariables",
    "compliance_footer",
    "compose_first_email",
    "compose_follow_up_1",
    "compose_follow_up_2",
    "compose_follow_up_3",
    "count_words",
    "derive_variables",
    "rotate_subject",
    "salutation",
    "with_footer",
]
