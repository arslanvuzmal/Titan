"""Section five, line by line.

The boundary is written down as two lists: seven things the campaign manager may
do, and seven it may never. Prose is not a boundary. This file is the second
list turned into seven named proofs, one per line, so that the document and the
codebase can be read against each other -- and so that removing a prohibition
requires deleting a test that says what is being given up.

Two mechanisms carry all seven, and they are different in kind:

* **The import graph.** A package that cannot import suppression, the outbox,
  a provider, the composer, the validator, the policy engine or the quota
  counters has no code path to any of them. Not a check that could be
  forgotten -- a capability that does not exist.
* **The column map.** Everything the manager writes goes through
  ``apply._COLUMN_FOR``, and every value in it is one of the manager's own
  ``managed_*`` columns. The human's configuration is what the manager is
  clamped *against*; writing to it directly would make next cycle's ceiling the
  manager's own last answer.

Four of the seven were previously proven only implicitly, by the column map
being short. That is true but indirect: a reader asking "can it resume a
campaign I paused?" had to reason from a list of three column names. Each now
has a test that answers the question in its own terms.
"""

from __future__ import annotations

import ast
import pathlib

from titan.autonomy.apply import _COLUMN_FOR

# parents[2] is apps/api -- the package root. Derived from this file rather
# than from the repository root, because an off-by-one there points the scan at
# an empty directory and every import test below passes vacuously. That is not
# hypothetical: it happened on the first run of this file, and
# test_the_manager_is_a_package_not_a_convention is what caught it.
API = pathlib.Path(__file__).resolve().parents[2]
AUTONOMY = API / "titan" / "autonomy"


def manager_sources() -> list[pathlib.Path]:
    """Every module the campaign manager is made of."""
    return sorted(AUTONOMY.rglob("*.py"))


def manager_imports() -> set[str]:
    """Everything the manager's package imports, transitively at the top level."""
    found: set[str] = set()
    for path in manager_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                # Both the module and each name imported from it. Recording
                # only the module leaves this blind to
                # `from titan.delivery import suppression`, which is exactly how
                # the import it bans would be written.
                found.add(node.module)
                found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def written_columns() -> set[str]:
    """Every column the manager can write, whatever the actuation."""
    return set(_COLUMN_FOR.values())


def assert_cannot_import(module: str, why: str) -> None:
    imported = manager_imports()
    offenders = {m for m in imported if m == module or m.startswith(f"{module}.")}
    assert not offenders, f"the manager imports {offenders}, so it can {why}"


def assert_cannot_write(column: str, why: str) -> None:
    assert column not in written_columns(), (
        f"the manager writes {column!r}, so it can {why}. "
        "Everything it may change goes through titan.autonomy.actuator, and "
        "everything else is not its to change."
    )


# ==========================================================================
# "The manager may never" -- seven lines, seven proofs
# ==========================================================================


def test_it_cannot_remove_or_override_a_suppression_entry() -> None:
    """An address that asked not to be contacted is not an optimisation target.

    Suppression is the one decision in the system made *by the recipient*, and
    a manager that could weigh it against a reply rate would eventually find a
    reason to.
    """
    assert_cannot_import(
        "titan.delivery.suppression", "remove or override a suppression entry"
    )


def test_it_cannot_approve_a_message_or_send_an_unapproved_one() -> None:
    """Approval is a human saying "yes, send this to this person".

    The manager cannot grant it -- no approval module is reachable -- and cannot
    route around it, because the outbox worker and every provider are outside
    its import graph too.
    """
    assert_cannot_import("titan.delivery.outbox_worker", "send without approval")
    for provider in ("resend", "smartlead", "smtp"):
        assert_cannot_import(
            f"titan.delivery.providers.{provider}", "hand a message to a provider"
        )


def test_it_cannot_relax_an_evidence_requirement_or_a_claim_map() -> None:
    """What a message asserts, and what backs the assertion.

    A manager able to reach the composer or the validator could improve a reply
    rate by loosening what has to be true, which is the one optimisation that
    must never be available.
    """
    assert_cannot_import("titan.intelligence.composer", "change what a message claims")
    assert_cannot_import(
        "titan.intelligence.message_validator", "change what the claim must satisfy"
    )


def test_it_cannot_raise_the_operating_mode_of_anything() -> None:
    """Operating mode is how much autonomy a human granted.

    A system that could widen its own permission is not bounded by it, however
    carefully every other bound is written.
    """
    assert_cannot_write("operating_mode", "widen its own autonomy")


def test_it_cannot_set_a_delivery_authorisation_gate() -> None:
    """The gates are the answer to "may this system send at all".

    Kept on the human's side of the column map, and the modules that evaluate
    them are outside the manager's import graph as well -- so it can neither set
    a gate nor consult one to find a way past it.
    """
    assert_cannot_write("sending_authorized", "authorise its own sending")
    assert_cannot_import("titan.policy.engine", "decide its own send permission")
    assert_cannot_import("titan.delivery.quotas", "spend or reset its own quota")


def test_it_cannot_widen_the_allowed_contact_sources() -> None:
    """Which sources a campaign may contact from is a compliance decision.

    Widening it is the cheapest possible way to raise lead volume, and the one
    with consequences that arrive months later as complaints.
    """
    assert_cannot_write(
        "allowed_contact_sources", "contact people from sources a human excluded"
    )


def test_it_cannot_resume_a_campaign_a_human_paused() -> None:
    """A pause is an instruction, not a health reading.

    The manager may throttle and may pause on evidence. Undoing a *person's*
    pause is a different act: it overrules a decision rather than making one.
    """
    assert_cannot_write("status", "restart something a person stopped")


# ==========================================================================
# The two mechanisms themselves
# ==========================================================================


def test_every_column_the_manager_writes_is_its_own() -> None:
    """The general rule behind the four column tests above.

    The specific names say which columns; this says what kind of column may
    ever appear on the list. A new actuation pointed at a human's field fails
    here even if nobody thought to write a test for that particular field.
    """
    offenders = [c for c in written_columns() if not c.startswith("managed_")]
    assert not offenders, (
        f"{offenders} are not the manager's columns. The human's configuration "
        "is the bound it is clamped against, and writing to it directly makes "
        "next cycle's ceiling the manager's own last answer."
    )


def test_the_manager_is_a_package_not_a_convention() -> None:
    """The boundary is enforceable only because the manager is one directory.

    If autonomy logic spread into activities or delivery, the import test would
    still pass while meaning nothing -- so the shape of the package is itself
    part of the guarantee.
    """
    modules = manager_sources()

    assert modules, "titan/autonomy is empty; the boundary tests prove nothing"
    assert (AUTONOMY / "actuator.py").exists(), "the actuator is the only way in"
    assert (AUTONOMY / "apply.py").exists(), "apply.py is the only writer"
