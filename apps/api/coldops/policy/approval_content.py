"""What a person approved: the words, not the row's version counter.

``message_drafts.version`` is SQLAlchemy's optimistic-lock counter. It moves on
*every* update -- the approval route setting ``status = approved`` moves it,
``queue_message`` setting ``status = queued`` moves it again -- so an approval
pinned to the version it was given was stale before it could ever be used, and
the send gate refused it: "approval covers draft version 1 but the draft is now
version 3". Every draft a person approved in the CRM was refused that way. The
end-to-end test of 10 Oct 2026 is what surfaced it; the comment in
``policy.engine._approval_denials`` shows the same symptom seen earlier and
fixed only for auto-approved campaigns.

The rule that check exists for is still right: approve, then edit, then send
must not bypass review. So an approval records a fingerprint of the content the
person saw, and the gate asks whether the content going out is that content.
A status change no longer looks like an edit; an edit still does.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Key in ``message_approvals.policy_snapshot``.
CONTENT_KEY = "content_sha256"


def content_fingerprint(
    *,
    subject: str | None,
    body_text: str | None,
    body_html: str | None,
    claim_map: Any,
) -> str:
    """A stable digest of everything the recipient would read or be told."""
    payload = json.dumps(
        {
            "subject": subject or "",
            "body_text": body_text or "",
            "body_html": body_html or "",
            "claim_map": claim_map or [],
        },
        sort_keys=True,
        default=str,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def of_draft(draft: Any) -> str:
    return content_fingerprint(
        subject=draft.subject,
        body_text=draft.body_text,
        body_html=draft.body_html,
        claim_map=draft.claim_map,
    )


__all__ = ["CONTENT_KEY", "content_fingerprint", "of_draft"]
