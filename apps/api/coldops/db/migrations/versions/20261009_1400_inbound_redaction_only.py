"""Let the retention job erase an inbound message's content -- and nothing else.

``inbound_messages`` was append-only by trigger, and the retention job erases
the body and raw payload of mail older than its window by UPDATE. The two have
been fighting since the job was scheduled: every run failed with
``relation inbound_messages is append-only``, so no inbound body has ever been
erased, which is the opposite of what the retention policy promises.

The same answer ``pages`` got (c4a7e1b93d26): the trigger now allows exactly one
change -- ``body_text`` set to NULL and ``raw_payload`` set to ``{}`` -- and
refuses any other. It checks by putting the old content back on a copy of the
new row and comparing whole records, so a column added later is protected
without anyone remembering to list it here.
"""

from __future__ import annotations

from alembic import op

revision = "a7e2c5f91d04"
down_revision = "f6d0b4e28c31"
branch_labels = None
depends_on = None

_REDACTION_ONLY = """
CREATE OR REPLACE FUNCTION titan_inbound_redaction_only() RETURNS trigger AS $$
DECLARE
    probe public.inbound_messages;
BEGIN
    IF NEW.body_text IS NULL AND NEW.raw_payload = '{}'::jsonb THEN
        probe := NEW;
        probe.body_text := OLD.body_text;
        probe.raw_payload := OLD.raw_payload;
        IF probe IS NOT DISTINCT FROM OLD THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION
        'relation % is append-only apart from erasing its content; % is not permitted',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(_REDACTION_ONLY)
    op.execute("DROP TRIGGER IF EXISTS inbound_messages_no_update ON inbound_messages")
    op.execute(
        """
        CREATE TRIGGER inbound_messages_redaction_only
            BEFORE UPDATE ON inbound_messages
            FOR EACH ROW EXECUTE FUNCTION titan_inbound_redaction_only()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS inbound_messages_redaction_only ON inbound_messages"
    )
    op.execute(
        """
        CREATE TRIGGER inbound_messages_no_update
            BEFORE UPDATE ON inbound_messages
            FOR EACH ROW EXECUTE FUNCTION titan_forbid_mutation()
        """
    )
    op.execute("DROP FUNCTION IF EXISTS titan_inbound_redaction_only()")
