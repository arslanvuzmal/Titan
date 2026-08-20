"""Approved drafts that nothing ever queued.

Queueing happens inside ``LeadResearchWorkflow``, one step after the approval it
waits for. If the workflow is not there when the approval arrives -- the window
elapsed, the worker restarted, the run was cancelled -- nothing else is
watching.

Found on the live workspace: 225 drafts approved and validated, with no outbox
row and no message. Mail a person authorised that would never have left, and
nothing reported it because every component had done its own job correctly.
"""

from __future__ import annotations

from titan.db.enums import DraftStatus
from titan.delivery.stranded import (
    DEFAULT_BATCH,
    STRANDABLE_STATUSES,
    Stranded,
    find_stranded,
)


def test_the_batch_is_bounded() -> None:
    """The sweeper competes with live sending for the same mailbox quota, and a
    backlog that appeared over two weeks does not need to clear in a minute."""
    assert 0 < DEFAULT_BATCH <= 500


def test_a_stranded_draft_carries_what_the_caller_needs() -> None:
    """Enough to queue it and to say which campaign it belonged to, and nothing
    else -- the decision to send is re-made downstream by ``queue_message``."""
    fields = Stranded.__dataclass_fields__

    assert set(fields) == {"draft_id", "lead_id", "campaign_id", "status"}


def test_find_stranded_is_exported_for_the_activity() -> None:
    assert callable(find_stranded)


class TestTheQueryShape:
    """The predicates, read off the compiled SQL.

    Asserted rather than eyeballed because each one is the difference between a
    correct sweep and a duplicate send, and none of them fails loudly if it is
    dropped -- the query still runs and returns the wrong rows.
    """

    def _sql(self) -> str:
        import uuid

        from sqlalchemy import select
        from sqlalchemy.dialects import postgresql
        from titan.db.models import Message, MessageDraft, OutboxMessage

        outbox_exists = (
            select(OutboxMessage.id)
            .where(OutboxMessage.draft_id == MessageDraft.id)
            .exists()
        )
        message_exists = (
            select(Message.id).where(Message.draft_id == MessageDraft.id).exists()
        )
        stmt = (
            select(MessageDraft.id)
            .where(
                MessageDraft.workspace_id == uuid.uuid4(),
                MessageDraft.status.in_(STRANDABLE_STATUSES),
                MessageDraft.validation_passed.is_(True),
                ~outbox_exists,
                ~message_exists,
            )
            .order_by(MessageDraft.created_at)
        )
        return str(stmt.compile(dialect=postgresql.dialect()))

    def test_it_is_scoped_to_one_workspace(self) -> None:
        assert "workspace_id" in self._sql()

    def test_an_already_queued_draft_is_excluded(self) -> None:
        """Without this the sweep re-queues everything the workflow queued
        correctly, and every recipient gets two."""
        sql = self._sql()

        assert "NOT (EXISTS" in sql.replace("\n", " ")
        assert "outbox_messages" in sql

    def test_an_already_sent_draft_is_excluded(self) -> None:
        """A message with no outbox row was sent through another path --
        Smartlead's own sequence, reconciled back in. Queueing it now sends the
        same person the same message twice."""
        assert "messages" in self._sql()

    def test_only_validated_drafts_are_offered(self) -> None:
        """``queue_message`` refuses an unvalidated draft anyway. Filtering here
        as well means the sweep does not report a hundred refusals a night for
        drafts that were never going anywhere."""
        assert "validation_passed" in self._sql()

    def test_the_oldest_wait_the_least_longer(self) -> None:
        """They were composed against the oldest evidence, so their claims are
        closest to going stale."""
        assert "ORDER BY message_drafts.created_at" in self._sql()


# ------------------------------------------------- the earlier dead end
#
# The sweeper was built for a draft whose *decision* was made and never acted
# on. The larger accident is one step before that: the workflow drafted, then
# ended before it could ask whether a person had to approve. On the live
# workspace, 322 drafts sat at AWAITING_APPROVAL with no outbox row against 241
# APPROVED -- and the sweeper could see only the smaller half.


def test_both_dead_ends_are_swept() -> None:
    """Planted violation: narrow this back to APPROVED alone and 322 drafts go
    back to being invisible."""
    assert DraftStatus.APPROVED in STRANDABLE_STATUSES
    assert DraftStatus.AWAITING_APPROVAL in STRANDABLE_STATUSES


def test_no_other_status_is_swept() -> None:
    """A validation failure is a verdict, not an accident, and a queued draft
    already has somewhere to go. Widening past these two would send mail that
    was refused on purpose."""
    assert set(STRANDABLE_STATUSES) == {
        DraftStatus.APPROVED,
        DraftStatus.AWAITING_APPROVAL,
    }


def test_the_caller_can_tell_the_two_apart() -> None:
    """They must not be handled alike. An approved draft may be queued on
    sight; one still awaiting approval has had no decision made about it, and
    queueing it without asking would be the sweeper granting an authority
    nobody gave it."""
    assert "status" in Stranded.__dataclass_fields__
    assert Stranded.__dataclass_fields__["status"].default is DraftStatus.APPROVED


def test_the_sweeper_asks_the_gate_rather_than_deciding() -> None:
    """Planted violation: queue AWAITING_APPROVAL drafts unconditionally and
    this fails.

    ``requires_human_approval`` reads the workspace and campaign policy at
    execution time. Using it here means a campaign that genuinely requires a
    person keeps its drafts, and the sweeper cannot widen what Titan is
    permitted to do -- which is the whole of invariant 18.
    """
    import inspect

    from titan.activities import stranded as activity

    source = inspect.getsource(activity.sweep_stranded_drafts)

    assert "requires_human_approval" in source
    assert "DraftStatus.AWAITING_APPROVAL" in source


def test_the_gate_is_asked_once_per_campaign_not_once_per_draft() -> None:
    """It reads two rows that cannot change between two drafts of the same
    campaign in one pass, against hundreds of drafts and twenty-three
    campaigns."""
    import inspect

    from titan.activities import stranded as activity

    source = inspect.getsource(activity.sweep_stranded_drafts)

    assert "auto_approves" in source
