"""The machine-learning registry: which models exist, what they said, and what was true.

Three tables, because a model is only worth promoting once three questions
have answers: which version is this (``ml_models``), what did it predict
(``ml_predictions``), and what turned out to be right (``ml_labels``).

**Every model starts in shadow.** It predicts, its predictions are kept, and
nothing reads them to act. A model becomes ``active`` only by a promotion that
records who promoted it and the held-out score that justified it -- and the
code refuses a promotion without both. That is the rule the operator set: a
new model replaces the old one when it is measurably better *and* a person
has said yes.

**Labels are kept, never overwritten.** A corrected label is a new row; the
latest one per subject is the truth, and the history shows the correction. A
label's ``source`` says where it came from -- the operator on the reply desk,
a call outcome -- because a model graded against its own guesses learns
nothing.

Predictions and labels are append-only by trigger, like ``events``. No
personal data is stored in either: a prediction is a class and a score, a
label is a class, and both point at their subject by id.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f6d0b4e28c31"
down_revision = "e5c9a3d17b20"
branch_labels = None
depends_on = None

_ISOLATION = (
    "current_setting('titan.workspace_id', true) IS NULL"
    " OR current_setting('titan.workspace_id', true) = ''"
    " OR workspace_id = current_setting('titan.workspace_id', true)::uuid"
)

_UUID = postgresql.UUID(as_uuid=True)


def _workspace() -> sa.Column:
    return sa.Column(
        "workspace_id",
        _UUID,
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )


def _now(name: str = "created_at") -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def upgrade() -> None:
    op.create_table(
        "ml_models",
        sa.Column(
            "id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        _workspace(),
        #: The job, shared by every version that does it: ``reply_reader``.
        sa.Column("name", sa.String(64), nullable=False),
        #: ``rules-v1``, ``llm-<route>-v1``, ``gbm-2026-10-20``.
        sa.Column("version", sa.String(120), nullable=False),
        #: rules | llm | gbm | dl
        sa.Column("kind", sa.String(16), nullable=False),
        #: shadow | active | retired
        sa.Column("status", sa.String(16), nullable=False, server_default="shadow"),
        #: How to run it: the model route, the prompt version, the artifact path.
        sa.Column(
            "config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        #: What it scored on held-out labels when it was last evaluated.
        sa.Column("holdout", postgresql.JSONB(), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_by", sa.String(200), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        _now(),
        sa.UniqueConstraint(
            "workspace_id", "name", "version", name="uq_ml_models_version"
        ),
    )
    # At most one active version of each job.
    op.create_index(
        "uq_ml_models_one_active",
        "ml_models",
        ["workspace_id", "name"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "ml_predictions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        _workspace(),
        sa.Column(
            "model_id",
            _UUID,
            sa.ForeignKey("ml_models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        #: inbound_message | lead | page
        sa.Column("subject_kind", sa.String(32), nullable=False),
        sa.Column("subject_id", _UUID, nullable=False),
        #: The predicted class, or the score's name for a regression.
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        _now(),
        sa.UniqueConstraint(
            "model_id", "subject_kind", "subject_id", name="uq_ml_predictions_subject"
        ),
    )
    op.create_index(
        "ix_ml_predictions_subject",
        "ml_predictions",
        ["workspace_id", "subject_kind", "subject_id"],
    )

    op.create_table(
        "ml_labels",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        _workspace(),
        #: The job the label grades: ``reply_reader``.
        sa.Column("task", sa.String(64), nullable=False),
        sa.Column("subject_kind", sa.String(32), nullable=False),
        sa.Column("subject_id", _UUID, nullable=False),
        sa.Column("label", sa.String(64), nullable=False),
        #: operator | call | system
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=True),
        _now(),
    )
    op.create_index(
        "ix_ml_labels_subject",
        "ml_labels",
        ["workspace_id", "task", "subject_kind", "subject_id", "created_at"],
    )

    for table in ("ml_models", "ml_predictions", "ml_labels"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY {table}_workspace_isolation ON "{table}" '
            f"USING ({_ISOLATION}) WITH CHECK ({_ISOLATION})"
        )
    for table in ("ml_predictions", "ml_labels"):
        op.execute(
            f"CREATE TRIGGER {table}_no_update "
            f'BEFORE UPDATE ON "{table}" FOR EACH ROW '
            "EXECUTE FUNCTION titan_forbid_mutation()"
        )


def downgrade() -> None:
    for table in ("ml_predictions", "ml_labels"):
        op.execute(f'DROP TRIGGER IF EXISTS {table}_no_update ON "{table}"')
    for table in ("ml_labels", "ml_predictions", "ml_models"):
        op.execute(f'DROP POLICY IF EXISTS {table}_workspace_isolation ON "{table}"')
    op.drop_index("ix_ml_labels_subject", table_name="ml_labels")
    op.drop_table("ml_labels")
    op.drop_index("ix_ml_predictions_subject", table_name="ml_predictions")
    op.drop_table("ml_predictions")
    op.drop_index("uq_ml_models_one_active", table_name="ml_models")
    op.drop_table("ml_models")
