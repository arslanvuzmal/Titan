"""The re-admission pass, run unattended.

:mod:`titan.intelligence.readmission` holds the rule; this gives it a schedule.
The bucket it drains only ever filled because nothing asked the question a
second time, so a version of this that a person has to remember to run would
refill it the moment they stopped.
"""

from __future__ import annotations

import logging
import uuid

from temporalio import activity

from titan.db.session import workspace_unit_of_work
from titan.intelligence import readmission
from titan.workflows.types import ReadmitLeadsInput, ReadmitLeadsResult

logger = logging.getLogger(__name__)


@activity.defn(name="readmit_leads")
async def readmit_leads(request: ReadmitLeadsInput) -> ReadmitLeadsResult:
    """Return leads that now clear their campaign's gate to the pipeline."""
    workspace_id = uuid.UUID(request.workspace_id)

    async with workspace_unit_of_work(workspace_id) as session:
        report = await readmission.readmit(
            session,
            workspace_id=workspace_id,
            limit=request.batch or readmission.DEFAULT_BATCH,
        )

    if report.promoted:
        logger.info(
            "re-admitted leads whose campaign gate has since moved",
            extra={"promoted": report.promoted, "remaining": report.remaining},
        )
    return ReadmitLeadsResult(promoted=report.promoted, remaining=report.remaining)


ALL_READMISSION_ACTIVITIES = [readmit_leads]

__all__ = ["ALL_READMISSION_ACTIVITIES", "readmit_leads"]
