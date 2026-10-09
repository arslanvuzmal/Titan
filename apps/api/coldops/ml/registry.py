"""Which model versions exist, what they predicted, what was true -- and the promotion rule.

**Promotion is the only way a model starts to matter**, and it is refused unless
all three hold:

1. enough labelled cases to judge it (``MIN_LABELS``);
2. it beats the version currently active *on the same cases* -- comparing two
   models on different subsets measures the subsets, not the models;
3. a named person approves it. The code will not promote on a score alone.

"Held out" means labels the model never saw while it was built. The LLM reply
reader is not trained on anything, so every label is held out for it; a
trained model is graded only on labels dated after its training snapshot
(``config.trained_until``), which stops it being marked on its own homework.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: Below this many labelled cases an accuracy figure is mostly noise.
MIN_LABELS = 30


class PromotionRefused(ValueError):
    """A promotion the rule does not allow, with the sentence saying why."""


@dataclasses.dataclass(frozen=True)
class ModelScore:
    model_id: uuid.UUID
    name: str
    version: str
    kind: str
    status: str
    labelled: int
    correct: int
    #: The subjects it was graded on, so two models compare on the same ones.
    subjects: frozenset[uuid.UUID] = frozenset()
    #: The subset it got right.
    right: frozenset[uuid.UUID] = frozenset()

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.labelled if self.labelled else None


async def ensure_model(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    name: str,
    version: str,
    kind: str,
    config: dict[str, Any] | None = None,
    status: str = "shadow",
) -> uuid.UUID:
    """The id of this version, registering it (in shadow) the first time it is seen."""
    return (
        await session.execute(
            text(
                """
                INSERT INTO ml_models (workspace_id, name, version, kind, status, config)
                VALUES (:ws, :name, :version, :kind, :status, CAST(:config AS jsonb))
                ON CONFLICT (workspace_id, name, version) DO UPDATE SET kind = EXCLUDED.kind
                RETURNING id
                """
            ),
            {
                "ws": workspace_id,
                "name": name,
                "version": version,
                "kind": kind,
                "status": status,
                "config": json.dumps(config or {}),
            },
        )
    ).scalar_one()


async def record_prediction(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    model_id: uuid.UUID,
    subject_kind: str,
    subject_id: uuid.UUID,
    label: str,
    score: float | None = None,
    detail: dict[str, Any] | None = None,
) -> bool:
    """Keep one prediction. False if this model already predicted this subject."""
    result = await session.execute(
        text(
            """
            INSERT INTO ml_predictions
                (workspace_id, model_id, subject_kind, subject_id, label, score, detail)
            VALUES (:ws, :model, :kind, :subject, :label, :score, CAST(:detail AS jsonb))
            ON CONFLICT (model_id, subject_kind, subject_id) DO NOTHING
            """
        ),
        {
            "ws": workspace_id,
            "model": model_id,
            "kind": subject_kind,
            "subject": subject_id,
            "label": label,
            "score": score,
            "detail": json.dumps(detail) if detail is not None else None,
        },
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def record_label(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    task: str,
    subject_kind: str,
    subject_id: uuid.UUID,
    label: str,
    source: str,
    created_by: str | None = None,
) -> None:
    """Add what was actually true. Never overwrites: the latest row per subject wins."""
    await session.execute(
        text(
            """
            INSERT INTO ml_labels
                (workspace_id, task, subject_kind, subject_id, label, source, created_by)
            VALUES (:ws, :task, :kind, :subject, :label, :source, :by)
            """
        ),
        {
            "ws": workspace_id,
            "task": task,
            "kind": subject_kind,
            "subject": subject_id,
            "label": label,
            "source": source,
            "by": created_by,
        },
    )


_GRADED = """
    WITH latest AS (
        SELECT DISTINCT ON (subject_kind, subject_id)
               subject_kind, subject_id, label, created_at
          FROM ml_labels
         WHERE workspace_id = :ws AND task = :name
         ORDER BY subject_kind, subject_id, created_at DESC, id DESC
    )
    SELECT m.id, m.name, m.version, m.kind, m.status, m.config,
           p.subject_id, (p.label = l.label) AS correct, l.created_at AS labelled_at
      FROM ml_models m
      LEFT JOIN ml_predictions p ON p.model_id = m.id AND p.workspace_id = :ws
      LEFT JOIN latest l
        ON l.subject_kind = p.subject_kind AND l.subject_id = p.subject_id
     WHERE m.workspace_id = :ws AND m.name = :name
"""


async def evaluate(
    session: AsyncSession, *, workspace_id: uuid.UUID, name: str
) -> list[ModelScore]:
    """Every version of one job, graded against the latest label for each subject."""
    rows = (
        await session.execute(text(_GRADED), {"ws": workspace_id, "name": name})
    ).all()
    by_model: dict[uuid.UUID, dict[str, Any]] = {}
    for r in rows:
        entry = by_model.setdefault(r.id, {"row": r, "right": {}})
        if r.subject_id is None or r.correct is None:
            continue
        trained_until = (r.config or {}).get("trained_until")
        if trained_until and r.labelled_at <= dt.datetime.fromisoformat(trained_until):
            continue  # seen in training: not held out
        entry["right"][r.subject_id] = bool(r.correct)
    return [
        ModelScore(
            model_id=model_id,
            name=v["row"].name,
            version=v["row"].version,
            kind=v["row"].kind,
            status=v["row"].status,
            labelled=len(v["right"]),
            correct=sum(v["right"].values()),
            subjects=frozenset(v["right"]),
            right=frozenset(k for k, ok in v["right"].items() if ok),
        )
        for model_id, v in by_model.items()
    ]


async def promote(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    model_id: uuid.UUID,
    approved_by: str,
    min_labels: int = MIN_LABELS,
) -> ModelScore:
    """Make this version the active one, if the rule allows. Raises ``PromotionRefused``."""
    if not (approved_by or "").strip():
        raise PromotionRefused("a promotion needs the name of the person approving it")
    name = (
        await session.execute(
            text("SELECT name FROM ml_models WHERE id = :id AND workspace_id = :ws"),
            {"id": model_id, "ws": workspace_id},
        )
    ).scalar_one_or_none()
    if name is None:
        raise PromotionRefused("no such model in this workspace")
    scores = {
        s.model_id: s
        for s in await evaluate(session, workspace_id=workspace_id, name=name)
    }
    candidate = scores[model_id]
    if candidate.status == "active":
        raise PromotionRefused(f"{candidate.version} is already active")
    if candidate.labelled < min_labels:
        raise PromotionRefused(
            f"{candidate.version} has {candidate.labelled} labelled cases; "
            f"at least {min_labels} are needed to judge it"
        )
    current = next((s for s in scores.values() if s.status == "active"), None)
    if current is not None:
        shared = candidate.subjects & current.subjects
        if len(shared) < min_labels:
            raise PromotionRefused(
                f"only {len(shared)} cases were judged by both {candidate.version} and "
                f"{current.version}; at least {min_labels} are needed to compare them"
            )
        new_right = len(candidate.right & shared)
        old_right = len(current.right & shared)
        if new_right <= old_right:
            raise PromotionRefused(
                f"{candidate.version} got {new_right} of {len(shared)} right; the active "
                f"{current.version} got {old_right}. It has to do better, not as well."
            )
        await session.execute(
            text(
                "UPDATE ml_models SET status = 'retired', retired_at = now() "
                "WHERE id = :id AND workspace_id = :ws"
            ),
            {"id": current.model_id, "ws": workspace_id},
        )
    await session.execute(
        text(
            """
            UPDATE ml_models
               SET status = 'active', promoted_at = now(), promoted_by = :by,
                   holdout = CAST(:holdout AS jsonb)
             WHERE id = :id AND workspace_id = :ws
            """
        ),
        {
            "id": model_id,
            "ws": workspace_id,
            "by": approved_by.strip(),
            "holdout": json.dumps(
                {
                    "labelled": candidate.labelled,
                    "correct": candidate.correct,
                    "accuracy": candidate.accuracy,
                }
            ),
        },
    )
    return candidate


__all__ = [
    "MIN_LABELS",
    "ModelScore",
    "PromotionRefused",
    "ensure_model",
    "evaluate",
    "promote",
    "record_label",
    "record_prediction",
]
