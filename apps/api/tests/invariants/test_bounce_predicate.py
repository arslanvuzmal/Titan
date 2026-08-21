"""Every reputation query must count the same bounces.

Five separate SQL literals fed a field named ``hard_bounced``, and every one of
them counted ``bounced_at IS NOT NULL`` -- any bounce at all. On the live
workspace that blocked ``outreach@`` at "5.3%" while it held **zero** hard
bounces and five the provider never explained. A mailbox that took three weeks
to warm was stopped for a month on the wrong evidence.

Fixing one copy and not the others is how the send gate and the health view came
to disagree about the same mailbox once already. This makes the next divergence
a failing test.
"""

from __future__ import annotations

import re

from titan.delivery.bounces import COUNTS_AGAINST_REPUTATION

from tests.invariants.test_repository_invariants import API, python_sources

#: What a query is asking, when it is not asking about our reputation.
#:
#: A query grouped or filtered by ``to_domain`` is measuring the *recipient*
#: domain -- "is this domain refusing our mail?" -- and feeds ``DomainWindow``,
#: whose bounce field is not named "hard" and whose thresholds are its own.
#:
#: Deliberately exempt rather than quietly fixed. A soft bounce there is
#: arguably also weak evidence, but nothing has established what the right
#: treatment is, and changing a live gate on an intuition is how the bug this
#: test exists for was introduced. Two known sites today: the outbox worker's
#: per-message check and the pipeline's bulk pre-fetch.
_DIFFERENT_QUESTION_MARKER = "to_domain"

#: Any FILTER clause counting bounces.
_BOUNCE_FILTER = re.compile(
    r"count\(\*\)\s*FILTER\s*\(\s*\n?\s*WHERE\s+bounced_at\s+IS\s+NOT\s+NULL"
    r"(?P<rest>[^)]*)\)",
    re.IGNORECASE,
)


def _reputation_queries() -> list[tuple[str, str]]:
    """Every bounce-counting FILTER, with the query around it."""
    found: list[tuple[str, str]] = []
    for path in python_sources():
        rel = path.relative_to(API).as_posix()
        text = path.read_text(encoding="utf-8")
        for match in _BOUNCE_FILTER.finditer(text):
            window = text[max(0, match.start() - 900) : match.end() + 900]
            found.append((rel, window))
    return found


def test_every_reputation_query_excludes_soft_bounces() -> None:
    """Planted violation: drop the predicate from any one of them and this
    fails, naming the file."""
    offenders: list[str] = []
    for rel, window in _reputation_queries():
        if _DIFFERENT_QUESTION_MARKER in window:
            continue
        if "bounce_kind IS DISTINCT FROM 'soft'" not in window:
            offenders.append(rel)

    assert not offenders, (
        "these queries count every bounce as one that damages reputation, "
        f"including soft ones: {sorted(set(offenders))}. Use the predicate in "
        "titan.delivery.bounces.COUNTS_AGAINST_REPUTATION."
    )


def test_the_constant_is_the_text_the_queries_actually_use() -> None:
    """The constant is documentation only if the queries repeat it verbatim.

    Planted violation: reword the constant without rewording the queries and
    this fails -- which is the point, because a docstring that contradicts the
    code is worse than none.
    """
    assert COUNTS_AGAINST_REPUTATION == (
        "bounced_at IS NOT NULL AND bounce_kind IS DISTINCT FROM 'soft'"
    )

    users = [
        rel
        for rel, window in _reputation_queries()
        if "bounce_kind IS DISTINCT FROM 'soft'" in window
    ]
    assert users, "no query uses the predicate; the constant is describing nothing"


def test_is_distinct_from_is_used_rather_than_a_plain_comparison() -> None:
    """``bounce_kind <> 'soft'`` is NULL for a NULL kind, and a NULL in a FILTER
    is not true -- so a bounce recorded before ``bounce_kind`` existed would
    stop counting entirely. ``IS DISTINCT FROM`` treats NULL as different, which
    is the conservative reading: an unlabelled bounce still counts."""
    assert "IS DISTINCT FROM" in COUNTS_AGAINST_REPUTATION
    assert "<>" not in COUNTS_AGAINST_REPUTATION
