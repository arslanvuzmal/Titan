"""An approval covers the words a person read, not the row's lock counter.

Found by the end-to-end test of 10 Oct 2026: approving a draft in the CRM sets
its status, which moves ``version``, so the approval -- pinned to the version it
was given -- was refused at send as stale. Every human approval, every time.
"""

from __future__ import annotations

from coldops.policy.approval_content import content_fingerprint
from coldops.policy.engine import DenyCode, evaluate_send

from tests.policy.test_send_authorization import sendable_context

APPROVED = content_fingerprint(
    subject="A note about your booking page",
    body_text="Hi there,\nYour booking button returns a 404.\n",
    body_html=None,
    claim_map=[{"claim": "broken CTA"}],
)


def codes(decision) -> set[DenyCode]:
    return {d.code for d in decision.denials}


def test_a_status_change_after_approval_is_not_an_edit() -> None:
    """The live failure: approved at v1, sent at v3, not one word changed."""
    decision = evaluate_send(
        sendable_context(
            approval_draft_version=1,
            draft_version=3,
            approval_content_sha256=APPROVED,
            draft_content_sha256=APPROVED,
        )
    )
    assert decision.allowed, decision.reason_text()


def test_an_edit_after_approval_still_needs_a_new_one() -> None:
    edited = content_fingerprint(
        subject="A note about your booking page",
        body_text="Hi there,\nYour booking button returns a 404. Call me!\n",
        body_html=None,
        claim_map=[{"claim": "broken CTA"}],
    )
    decision = evaluate_send(
        sendable_context(
            approval_draft_version=1,
            draft_version=1,
            approval_content_sha256=APPROVED,
            draft_content_sha256=edited,
        )
    )
    assert DenyCode.APPROVAL_STALE in codes(decision)


def test_an_approval_from_before_fingerprints_keeps_the_version_rule() -> None:
    decision = evaluate_send(
        sendable_context(
            approval_draft_version=1,
            draft_version=3,
            approval_content_sha256=None,
            draft_content_sha256=APPROVED,
        )
    )
    assert DenyCode.APPROVAL_STALE in codes(decision)


def test_every_part_of_the_message_is_in_the_fingerprint() -> None:
    base = {
        "subject": "s",
        "body_text": "t",
        "body_html": "<p>h</p>",
        "claim_map": [{"claim": "c"}],
    }
    original = content_fingerprint(**base)
    for field, changed in (
        ("subject", "s2"),
        ("body_text", "t2"),
        ("body_html", "<p>h2</p>"),
        ("claim_map", [{"claim": "c2"}]),
    ):
        assert content_fingerprint(**{**base, field: changed}) != original, field
