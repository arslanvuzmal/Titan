"""Every path that creates a campaign must create its sequence.

D20. `ensure_sequence` existed, was correct, was idempotent, and was called
from exactly one of the three places a campaign can be born -- the API route.
The two that matter for an unattended system, autonomous market expansion and
market provisioning, both skipped it.

A campaign without a sequence can never follow up. `FollowUpScheduler` builds
its plan from `email_sequences`, finds nothing, and every lead in that campaign
is contacted exactly once. The absence is invisible from outside: a campaign
with no follow-ups due looks identical to one that is up to date.

By 17 September, 49 active campaigns had no sequence and 205 leads sat past
their `next_action_at` with nowhere to go -- and expansion was adding more
every time it opened a market, so the estate was manufacturing the defect
faster than anyone was noticing it.

The test below is deliberately written against *the property*, not against the
two functions that were wrong. A further creation path added next year is the
thing that would reintroduce this, and asserting "expansion calls
ensure_sequence" would not catch that.

It earned that immediately: written to check three paths, it failed on first
run and named a fourth -- ``seed.py`` -- which a grep for ``Campaign(`` had
missed because the output was truncated. The roster assertion is there so the
next one announces itself too.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

TITAN = pathlib.Path(__file__).resolve().parents[2] / "titan"


def _calls_in(tree: ast.AST) -> set[str]:
    """Every function name called anywhere in this module."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _constructs_campaign(tree: ast.AST) -> bool:
    return "Campaign" in {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _modules_that_create_campaigns() -> list[pathlib.Path]:
    found = []
    for path in TITAN.rglob("*.py"):
        if "migrations" in path.parts:
            # A migration creating a row is a data fix, not a creation path.
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - would fail elsewhere first
            continue
        if _constructs_campaign(tree):
            found.append(path)
    return found


def test_the_creation_paths_are_the_ones_we_think_they_are() -> None:
    """If this fails, a new way to create a campaign has appeared.

    Not a failure in itself -- but the next assertion only means something if
    this list is complete, so it is worth being told.
    """
    paths = {p.name for p in _modules_that_create_campaigns()}
    assert paths == {
        "routes.py",
        "expansion.py",
        "provision_markets.py",
        "seed.py",
    }, (
        "a new campaign creation path exists; check it calls ensure_sequence "
        f"and add it here. Found: {sorted(paths)}"
    )


@pytest.mark.parametrize(
    "module",
    ["routes.py", "expansion.py", "provision_markets.py", "seed.py"],
)
def test_every_campaign_creation_path_provisions_a_sequence(module: str) -> None:
    """The property, stated once, checked everywhere it applies."""
    path = next(p for p in _modules_that_create_campaigns() if p.name == module)
    tree = ast.parse(path.read_text(encoding="utf-8"))

    assert "ensure_sequence" in _calls_in(tree), (
        f"{module} creates a Campaign but never calls ensure_sequence. "
        "Its leads will be contacted once and never followed up, and nothing "
        "will report that -- a campaign with no follow-ups due is "
        "indistinguishable from one that is up to date."
    )
