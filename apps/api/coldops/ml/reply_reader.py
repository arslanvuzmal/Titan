"""The reply reader (M2): rules today, a model in shadow beside them, graded by the operator.

Today's reader is rules -- ``classify_reply`` decides human or machine, and
``detect_intent`` decides what a human wants. They are registered as the active
version (``rules-v1``) and their past verdicts are copied into the shadow log
from ``reply_classifications``, so a challenger is compared with what actually
ran rather than with a rerun.

The challenger is an LLM reading the same reply. **It decides nothing**: its
verdict is written to ``ml_predictions`` and nowhere else. It becomes the
reader only through ``registry.promote`` -- more correct than the rules on the
same replies, judged against labels the operator gave on the reply desk, and
approved by name.

The reply travels as untrusted data inside a fenced block: it is written by a
stranger, and a stranger's email is the most obvious place to put "ignore your
instructions". The schema allows only the known classes, so the most a hostile
reply can do is be misread -- and a misread in shadow changes nothing.
"""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coldops.db.enums import HUMAN_REPLY_CLASSES, ModelTask, ReplyClass
from coldops.ml import registry
from coldops.models.channels import PromptBundle, UntrustedBlock

logger = logging.getLogger(__name__)

NAME = "reply_reader"
RULES_VERSION = "rules-v1"
#: Bumped whenever the prompt changes, so a new prompt is a new version with
#: its own record rather than a silent change to an old one.
PROMPT_VERSION = "p1"
SUBJECT = "inbound_message"

#: What a person can mean. Machine classes (bounce, auto-reply) are the rules'
#: job and stay there: headers decide those, not wording.
READER_CLASSES: tuple[str, ...] = (
    *sorted(c.value for c in HUMAN_REPLY_CLASSES),
    ReplyClass.UNKNOWN.value,
)

_SYSTEM = (
    "You read replies to a cold email sent to a small business and say what the "
    "person wants. Answer only from what they wrote."
)
_POLICY = (
    "Classes: interested (positive, wants to proceed or hear more), wants_more_info, "
    "wants_pricing, wants_call (asks for a call or meeting), referral (points to "
    "someone else who decides), not_now (maybe later), not_interested, objection "
    "(pushes back with a reason), wrong_person, unsubscribe (asks to be removed), "
    "complaint (calls it spam or threatens to report), unknown (cannot tell).\n"
    "If it says both 'not now' and 'call me in spring', choose not_now. If it asks "
    "to be removed in any words, choose unsubscribe."
)


class ReaderVerdict(BaseModel):
    reply_class: Literal[READER_CLASSES]  # type: ignore[valid-type]
    confidence: float = Field(ge=0.0, le=1.0)
    #: The person's own words that decided it, quoted.
    excerpt: str = Field(default="", max_length=300)


def llm_version(route: str) -> str:
    """The route and the prompt together are the model: change either, new version."""
    return f"llm-{route}-{PROMPT_VERSION}"[:120]


def bundle_for(subject: str | None, body: str) -> PromptBundle:
    return PromptBundle(
        system=_SYSTEM,
        policy=_POLICY,
        untrusted=[
            UntrustedBlock(label="reply", content=f"Subject: {subject or ''}\n\n{body}")
        ],
        task=(
            "Classify the reply in the untrusted block. Return JSON with keys "
            "'reply_class', 'confidence' (0 to 1) and 'excerpt' (their words, quoted)."
        ),
    )


async def record_rules_verdicts(session: AsyncSession, *, workspace_id: uuid.UUID) -> int:
    """Copy what the rules decided into the shadow log. Idempotent."""
    rules_id = await _rules_model(session, workspace_id)
    result = await session.execute(
        text(
            """
            INSERT INTO ml_predictions
                (workspace_id, model_id, subject_kind, subject_id, label, score)
            SELECT c.workspace_id, :model, :kind, c.inbound_message_id,
                   c.reply_class::text, c.confidence
              FROM reply_classifications c
             WHERE c.workspace_id = :ws
            ON CONFLICT (model_id, subject_kind, subject_id) DO NOTHING
            """
        ),
        {"ws": workspace_id, "model": rules_id, "kind": SUBJECT},
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def _rules_model(session: AsyncSession, workspace_id: uuid.UUID) -> uuid.UUID:
    """The rules version, made active the first time only if nothing else is."""
    has_active = (
        await session.execute(
            text(
                "SELECT 1 FROM ml_models WHERE workspace_id = :ws AND name = :n "
                "AND status = 'active'"
            ),
            {"ws": workspace_id, "n": NAME},
        )
    ).first()
    return await registry.ensure_model(
        session,
        workspace_id=workspace_id,
        name=NAME,
        version=RULES_VERSION,
        kind="rules",
        config={"source": "coldops.intelligence.replies + intent"},
        status="shadow" if has_active else "active",
    )


async def unread_replies(
    session: AsyncSession, *, workspace_id: uuid.UUID, model_id: uuid.UUID, limit: int
) -> list[tuple[uuid.UUID, str | None, str]]:
    """Human replies this model has not read yet, newest first."""
    rows = (
        await session.execute(
            text(
                """
                SELECT i.id, i.subject, i.body_text
                  FROM inbound_messages i
                  JOIN reply_classifications c
                    ON c.inbound_message_id = i.id AND c.workspace_id = :ws
                 WHERE i.workspace_id = :ws
                   AND i.body_text IS NOT NULL
                   AND c.reply_class::text = ANY(CAST(:classes AS text[]))
                   AND NOT EXISTS (
                       SELECT 1 FROM ml_predictions p
                        WHERE p.model_id = :model AND p.subject_kind = :kind
                          AND p.subject_id = i.id)
                 ORDER BY i.received_at DESC
                 LIMIT :limit
                """
            ),
            {
                "ws": workspace_id,
                "model": model_id,
                "kind": SUBJECT,
                "classes": list(READER_CLASSES),
                "limit": limit,
            },
        )
    ).all()
    return [(r.id, r.subject, r.body_text) for r in rows]


async def read_one(gateway, *, subject: str | None, body: str) -> ReaderVerdict:  # type: ignore[no-untyped-def]
    verdict, _ = await gateway.complete_typed(
        ModelTask.EXTRACTION,
        ReaderVerdict,
        bundle_for(subject, body),
        max_tokens=600,
        temperature=0.0,
    )
    return verdict


__all__ = [
    "NAME",
    "READER_CLASSES",
    "RULES_VERSION",
    "SUBJECT",
    "ReaderVerdict",
    "bundle_for",
    "llm_version",
    "read_one",
    "record_rules_verdicts",
    "unread_replies",
]
