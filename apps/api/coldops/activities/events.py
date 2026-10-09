"""Keeping ``events`` up to date: the one activity behind the scheduled projection.

See ``coldops.intelligence.event_stream`` for what is projected and why. This
module only owns the transaction boundary and the failure report.

**Failures are reported, not raised.** A projector that fails leaves the stream
stale, never wrong -- the next run re-reads the same window and catches up --
so the result carries ``unavailable`` for the schedule's history and the
operator's view, and Temporal does not burn retries on a database that is down.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from temporalio import activity

from coldops.db.session import workspace_unit_of_work
from coldops.intelligence import event_stream
from coldops.workflows.types import ProjectEventsInput, ProjectEventsResult

logger = logging.getLogger(__name__)


def _since(request: ProjectEventsInput) -> dt.datetime:
    if request.since:
        parsed = dt.datetime.fromisoformat(request.since)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return dt.datetime.now(dt.UTC) - event_stream.DEFAULT_LOOKBACK


async def project_events_now(request: ProjectEventsInput) -> ProjectEventsResult:
    """The body of the activity, callable directly from the CLI."""
    workspace_id = uuid.UUID(request.workspace_id)
    try:
        async with workspace_unit_of_work(workspace_id) as session:
            report = await event_stream.project(
                session, workspace_id=workspace_id, since=_since(request)
            )
    except Exception as exc:  # stale, never wrong: report and let the next run catch up
        logger.exception("event projection failed")
        return ProjectEventsResult(unavailable=f"{type(exc).__name__}: {exc}"[:500])
    return ProjectEventsResult(
        inserted=report.total,
        by_source=tuple(sorted(report.inserted.items())),
    )


@activity.defn(name="project_events")
async def project_events(request: ProjectEventsInput) -> ProjectEventsResult:
    return await project_events_now(request)


ALL_EVENT_ACTIVITIES = [project_events]

__all__ = ["ALL_EVENT_ACTIVITIES", "project_events", "project_events_now"]
