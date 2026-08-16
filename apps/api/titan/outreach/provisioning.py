"""Giving a campaign the sequence it is supposed to follow.

:mod:`titan.outreach.sequence` has held four steps, their wording and their
cadence since it was ported, and :mod:`titan.delivery.followup_scheduler` has
known how to walk them. Between the two there was nothing: no code path ever
created an ``email_sequences`` row, so the scheduler's lookup returned None for
every campaign, every lead was contacted once, and three quarters of the
outreach this system was built to send existed only as tested functions.

The steps are declared here rather than typed into a migration because they are
already declared in one place -- ``STEP_DELAYS_IN_DAYS`` and ``TEMPLATE_KEYS``
-- and a second copy in SQL would be free to drift from the wording it names.

**Idempotent.** Called at campaign creation and again by the backfill, and a
campaign that already has an active sequence keeps the one it has. Replacing it
would orphan the drafts that reference its steps.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.models import EmailSequence, SequenceStep
from titan.outreach.sequence import STEP_DELAYS_IN_DAYS, TEMPLATE_KEYS

#: The name every provisioned sequence carries. Unique per campaign, so this is
#: also what makes a second provisioning run a no-op rather than a duplicate.
DEFAULT_SEQUENCE_NAME = "outreach_v2"

#: Whether each step must cite a finding no earlier step cited.
#:
#: Derived from what each composer actually takes, not chosen: step 1 opens with
#: the evidence, ``compose_follow_up_1`` is a short nudge that takes no
#: variables at all and so cannot cite anything new, and follow-ups 2 and 3 both
#: take :class:`~titan.outreach.variables.FindingVariables` because carrying a
#: further observation is the reason they are sent. Marking the nudge as
#: requiring new evidence would make it unsendable; marking the other two as not
#: requiring it would permit the thing section 13 forbids -- the first message
#: again in different words.
REQUIRES_NEW_EVIDENCE: tuple[bool, ...] = (False, False, True, True)


async def ensure_sequence(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    campaign_id: uuid.UUID,
) -> EmailSequence | None:
    """Give this campaign the four-step sequence, unless it already has one.

    Returns the sequence it created, or None when one was already there.
    """
    existing = (
        await session.execute(
            select(EmailSequence).where(
                EmailSequence.campaign_id == campaign_id,
                EmailSequence.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None

    sequence = EmailSequence(
        workspace_id=workspace_id,
        campaign_id=campaign_id,
        name=DEFAULT_SEQUENCE_NAME,
        is_active=True,
    )
    session.add(sequence)
    await session.flush()

    for index, (delay, template_key) in enumerate(
        zip(STEP_DELAYS_IN_DAYS, TEMPLATE_KEYS, strict=True)
    ):
        session.add(
            SequenceStep(
                workspace_id=workspace_id,
                sequence_id=sequence.id,
                step_number=index + 1,
                delay_days=delay,
                template_key=template_key,
                requires_new_evidence=REQUIRES_NEW_EVIDENCE[index],
            )
        )
    await session.flush()
    return sequence


__all__ = ["DEFAULT_SEQUENCE_NAME", "REQUIRES_NEW_EVIDENCE", "ensure_sequence"]
