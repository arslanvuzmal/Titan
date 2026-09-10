"""Attach historic drafts to the sequence step they actually were.

``sequence_step_id`` was never written. :func:`titan.activities.pipeline.generate_draft`
omitted the column entirely, so every one of the 6,064 drafts in the estate
carried ``NULL``. :meth:`titan.delivery.followup_scheduler.FollowUpScheduler._plan_for`
builds its ``completed`` set by reading that column, found nothing, and handed
:func:`titan.intelligence.sequencing.plan_followup` an empty set -- which makes
``remaining[0]`` step one, ``delay_days=0``, due immediately, forever. The
estate re-drafted openers until they superseded each other 5,000 times and
never once composed step two.

Fixing the writer fixes new drafts. It does not fix the 375 leads already
contacted: their history is still ``NULL``, so the scheduler still believes no
step has been sent and would open with the message those businesses already
received. This closes that gap.

**Position is taken from what was delivered, not from what was drafted.** A
lead has thousands of superseded drafts and a handful of sent messages, and
only the sent ones happened as far as the recipient is concerned. So the
ordering key is ``outbox_messages.sent_at`` over rows that reached ``sent``,
and a draft that never left the building stays ``NULL`` -- correctly, because
it never was a step.

Dry by default. :func:`survey` reports; :func:`apply` writes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import text

from titan.db.session import get_sessionmaker

#: Ranks each lead's *delivered* messages and lines them up against the active
#: sequence's steps. Scoped to one workspace explicitly: raw SQL carries none of
#: the ORM's workspace guard, so the predicate has to be written by hand.
_MATCHED = """
WITH delivered AS (
    SELECT o.draft_id,
           o.lead_id,
           row_number() OVER (
               PARTITION BY o.lead_id ORDER BY o.sent_at, o.id
           ) AS position
      FROM outbox_messages o
     WHERE o.workspace_id = :ws
       AND o.status = 'sent'
       AND o.draft_id IS NOT NULL
),
steps AS (
    SELECT s.id AS step_id, s.step_number, e.campaign_id
      FROM sequence_steps s
      JOIN email_sequences e ON e.id = s.sequence_id
     WHERE e.is_active
       AND e.workspace_id = :ws
)
SELECT d.id AS draft_id, st.step_id, st.step_number
  FROM message_drafts d
  JOIN delivered dl ON dl.draft_id = d.id
  JOIN steps st ON st.campaign_id = d.campaign_id
                AND st.step_number = dl.position
 WHERE d.workspace_id = :ws
   AND d.sequence_step_id IS NULL
"""

_APPLY = f"""
UPDATE message_drafts d
   SET sequence_step_id = m.step_id
  FROM ({_MATCHED}) AS m
 WHERE d.id = m.draft_id
"""

#: Leads that have been written to and whose drafts still carry no step. After
#: the update these are the ones whose next scan can finally advance.
_LEADS_AFFECTED = f"""
SELECT count(DISTINCT d.lead_id)
  FROM message_drafts d
  JOIN ({_MATCHED}) AS m ON m.draft_id = d.id
"""


@dataclass
class BackfillReport:
    drafts_matched: int = 0
    leads_affected: int = 0
    #: How many drafts land on each step, so a run that assigned every draft to
    #: step one -- which would mean the ranking did nothing -- is visible rather
    #: than merely plausible.
    by_step: dict[int, int] = field(default_factory=dict)
    applied: bool = False

    @property
    def is_noop(self) -> bool:
        return self.drafts_matched == 0


async def survey(workspace_id: uuid.UUID) -> BackfillReport:
    """What the backfill would attach. Writes nothing."""
    return await _run(workspace_id, apply_changes=False)


async def apply(workspace_id: uuid.UUID) -> BackfillReport:
    """Attach the steps. Idempotent: only ever fills a ``NULL``."""
    return await _run(workspace_id, apply_changes=True)


async def _run(workspace_id: uuid.UUID, *, apply_changes: bool) -> BackfillReport:
    report = BackfillReport(applied=apply_changes)
    params = {"ws": workspace_id}

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (await session.execute(text(_MATCHED), params)).all()
        report.drafts_matched = len(rows)
        for _draft_id, _step_id, step_number in rows:
            report.by_step[step_number] = report.by_step.get(step_number, 0) + 1

        report.leads_affected = (await session.scalar(text(_LEADS_AFFECTED), params)) or 0

        if apply_changes and rows:
            await session.execute(text(_APPLY), params)
            await session.commit()

    return report


__all__ = ["BackfillReport", "apply", "survey"]
