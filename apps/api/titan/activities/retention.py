"""The retention pass, run unattended.

:mod:`titan.delivery.retention` holds the judgement and the SQL; this is the
activity that gives it a schedule, because a retention policy nobody runs is a
policy that does not exist. That is not a hypothetical: the module's own column
had been in the schema since the first migration and nothing had ever written
to it.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from temporalio import activity

from titan.db.session import workspace_unit_of_work
from titan.delivery import retention
from titan.workflows.types import EraseExpiredDataInput, EraseExpiredDataResult

logger = logging.getLogger(__name__)


@activity.defn(name="erase_expired_data")
async def erase_expired_data(
    request: EraseExpiredDataInput,
) -> EraseExpiredDataResult:
    """Erase the content held about businesses that never replied."""
    workspace_id = uuid.UUID(request.workspace_id)
    now = dt.datetime.now(dt.UTC)

    async with workspace_unit_of_work(workspace_id) as session:
        report = await retention.erase_expired(
            session,
            workspace_id=workspace_id,
            now=now,
            limit=request.batch or retention.DEFAULT_BATCH,
        )

    if report.erased_anything:
        logger.info(
            "erased expired content",
            extra={
                "leads": report.leads_examined,
                "drafts": report.drafts_erased,
                "pages": report.pages_erased,
                "kept_for_reply": report.kept_for_reply,
            },
        )
    return EraseExpiredDataResult(
        leads_examined=report.leads_examined,
        drafts_erased=report.drafts_erased,
        pages_erased=report.pages_erased,
        kept_for_reply=report.kept_for_reply,
    )


ALL_RETENTION_ACTIVITIES = [erase_expired_data]

__all__ = ["ALL_RETENTION_ACTIVITIES", "erase_expired_data"]
