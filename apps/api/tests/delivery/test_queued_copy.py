"""The words that were gated must be the words that go out.

Every gate in the outbox worker reads the *draft*. The provider is handed
``outbox.payload``, a copy rendered when the row was queued. They agree until
something rewrites one of them, and then the system is checking one message and
sending another -- which is the only disagreement here that a stranger ever
sees.

Found on the live workspace: 44 deferred rows whose payload no longer matched
the draft they had been repointed at. Every gate passed; the text on the wire
would have been the copy nobody re-read.
"""

from __future__ import annotations

import uuid

from titan.delivery.outbox_worker import _payload_is_the_gated_draft


class Row:
    """Only what the check reads."""

    def __init__(self, payload: dict | None) -> None:
        self.id = uuid.uuid4()
        self.payload = payload


class Draft:
    def __init__(self, body_text: str | None) -> None:
        self.id = uuid.uuid4()
        self.body_text = body_text
        self.version = 1


def test_a_matching_copy_may_send() -> None:
    body = "Hi there,\n\nA message somebody approved."

    assert _payload_is_the_gated_draft(Row({"text_body": body}), Draft(body))


def test_a_rewritten_draft_with_a_stale_copy_may_not_send() -> None:
    """The exact shape of the live finding: repointed, not re-rendered."""
    row = Row({"text_body": "The words nobody approved twice."})

    assert not _payload_is_the_gated_draft(row, Draft("The rewritten body."))


def test_a_missing_payload_is_a_mismatch_not_a_pass() -> None:
    """Fails closed. A row with nothing rendered has nothing that was gated."""
    assert not _payload_is_the_gated_draft(Row(None), Draft("A body."))


def test_an_empty_draft_body_matches_an_empty_payload() -> None:
    """Neither can send -- the length band refuses both -- but this check is
    about agreement, and two empty bodies agree. Reporting a mismatch here
    would put the wrong reason on the refusal."""
    assert _payload_is_the_gated_draft(Row({"text_body": ""}), Draft(None))
