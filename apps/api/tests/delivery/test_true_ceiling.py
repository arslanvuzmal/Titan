"""The mailbox's configured ceiling, not today's ramped-down value.

`message_per_day` in the provider is what the ramp last *wrote*. Partway
through a warm-up that is a fraction of the real limit, so reading it back as
the ceiling is the one-way ratchet `observe_ceiling` exists to prevent --
reached by a different door.

Observed live: `sales@` was created with a ceiling of 18, because the ramp had
already written 18. It would have warmed toward a third of its actual limit and
stopped there, permanently.
"""

from __future__ import annotations

from titan.provision_senders import true_ceiling

ACCOUNT = {"id": 22147662, "from_email": "sales@x.com", "message_per_day": 18}


def test_the_recorded_ceiling_beats_the_ramped_value() -> None:
    """50 is what the operator set; 18 is what the ramp had reached."""
    assert true_ceiling(ACCOUNT, {"22147662": 50}) == 50


def test_a_mailbox_the_ramp_has_never_touched_uses_the_provider() -> None:
    """Nothing has written to it, so the live value is the operator's."""
    assert true_ceiling(ACCOUNT, {}) == 18


def test_an_account_reporting_nothing_gets_a_sane_default() -> None:
    """Zero would mean a mailbox that can never send, which is a harder failure
    than one that sends conservatively."""
    assert true_ceiling({"id": 1, "message_per_day": 0}, {}) == 50
    assert true_ceiling({"id": 1}, {}) == 50


def test_the_lookup_is_by_provider_id_not_address() -> None:
    """Addresses are reused across reconnections; the id is what the ramp keyed
    its own record on."""
    assert true_ceiling(ACCOUNT, {"99999": 50}) == 18
