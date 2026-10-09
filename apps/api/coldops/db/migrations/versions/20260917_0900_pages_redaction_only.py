"""Let retention erase page text without letting anything else rewrite a page.

Two deliberate guarantees had been in direct conflict since the retention pass
was written, and the conflict was invisible because each looked correct alone.

`pages` is append-only, enforced by a BEFORE UPDATE trigger, so that the
evidence a message was built on cannot be altered after the fact. Retention has
to erase `text_excerpt` after thirty days, because it is scraped text about
real businesses that never replied. Every hourly pass therefore raised

    RestrictViolation: relation pages is append-only; UPDATE is not permitted

and the housekeeping workflow logged "retention pass skipped". Nothing had ever
been erased -- 0 of 807 messages carried `body_retained_until` -- while the
system reported itself healthy, because a skipped pass is not a failed one.

The fix is not to drop the guard. It is to say precisely which single mutation
is permitted: setting `text_excerpt` from a value to NULL, with every other
column identical. Anything else on `pages` still raises, including setting the
text to a *different* value, which is the shape a redaction could otherwise
have been used as cover for.

Revision ID: c4a7e1b93d26
Revises: e2f7a91c4b83
Create Date: 2026-09-17
"""

from __future__ import annotations

from alembic import op

revision = "c4a7e1b93d26"
down_revision = "e2f7a91c4b83"
branch_labels = None
depends_on = None


# `probe := NEW` copies the incoming row; putting the old text back and then
# comparing the whole record is how this asserts "nothing else changed" without
# naming all twenty columns -- a list that would silently stop being exhaustive
# the next time one is added.
_REDACTION_ONLY = """
CREATE OR REPLACE FUNCTION titan_pages_redaction_only() RETURNS trigger AS $$
DECLARE
    probe public.pages;
BEGIN
    IF OLD.text_excerpt IS NOT NULL AND NEW.text_excerpt IS NULL THEN
        probe := NEW;
        probe.text_excerpt := OLD.text_excerpt;
        IF probe IS NOT DISTINCT FROM OLD THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION
        'relation % is append-only apart from erasing text_excerpt; % is not permitted',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(_REDACTION_ONLY)
    op.execute("DROP TRIGGER IF EXISTS pages_no_update ON pages")
    op.execute(
        """
        CREATE TRIGGER pages_redaction_only
            BEFORE UPDATE ON pages
            FOR EACH ROW EXECUTE FUNCTION titan_pages_redaction_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS pages_redaction_only ON pages")
    op.execute(
        """
        CREATE TRIGGER pages_no_update
            BEFORE UPDATE ON pages
            FOR EACH ROW EXECUTE FUNCTION titan_forbid_mutation()
        """
    )
    op.execute("DROP FUNCTION IF EXISTS titan_pages_redaction_only()")
