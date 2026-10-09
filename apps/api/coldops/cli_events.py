"""``coldops events`` -- fill the event stream and read one business's history.

    coldops events project --workspace titan            # last three days
    coldops events project --workspace titan --all      # the whole history
    coldops events lead <lead-id> --workspace titan     # one business, in order

The scheduled projection does the first every fifteen minutes; the command is
for the backfill after a deploy, and for seeing what the schedule would see.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from sqlalchemy import select

from coldops.intelligence import event_stream


async def _workspace_id(slug: str) -> uuid.UUID:
    from coldops.db.models import Workspace
    from coldops.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(Workspace).where(Workspace.slug == slug))
        ).scalar_one_or_none()
    if row is None:
        raise SystemExit(f"no workspace with slug {slug!r}")
    return row.id


async def _project(args: argparse.Namespace) -> int:
    from coldops.activities.events import project_events_now
    from coldops.workflows.types import ProjectEventsInput

    workspace_id = await _workspace_id(args.workspace)
    since = event_stream.EPOCH.isoformat() if args.all else None
    result = await project_events_now(
        ProjectEventsInput(workspace_id=str(workspace_id), since=since)
    )
    if result.unavailable:
        print(f"Projection failed: {result.unavailable}")
        return 1
    window = "the whole history" if args.all else "the last three days"
    print(f"Projected {window}: {result.inserted} new event(s).")
    for source, count in result.by_source:
        print(f"  {source:<24} {count:>7}")
    return 0


async def _lead(args: argparse.Namespace) -> int:
    from coldops.db.session import get_sessionmaker

    workspace_id = await _workspace_id(args.workspace)
    async with get_sessionmaker()() as session:
        rows = await event_stream.lead_history(
            session, workspace_id=workspace_id, lead_id=uuid.UUID(args.lead_id)
        )
    if not rows:
        print("No events for that lead. Run `coldops events project --all` first?")
        return 1
    for row in rows:
        detail = json.dumps(row["payload"], default=str, separators=(",", ":"))
        print(f"{row['occurred_at']:%Y-%m-%d %H:%M}  {row['kind']:<20} {detail}")
    return 0


def _run(coro_fn):
    def handler(args: argparse.Namespace) -> int:
        from coldops.runtime import configure_event_loop

        configure_event_loop()
        return asyncio.run(coro_fn(args))

    return handler


def add_events_parser(sub: argparse._SubParsersAction) -> None:
    events = sub.add_parser("events", help="fill and read the event stream")
    actions = events.add_subparsers(dest="events_action", required=True)

    project = actions.add_parser("project", help="bring the event stream up to date")
    project.add_argument("--workspace", default="titan")
    project.add_argument(
        "--all",
        action="store_true",
        help="project the whole history, not just the last three days (the backfill)",
    )
    project.set_defaults(func=_run(_project))

    lead = actions.add_parser("lead", help="print one business's history, oldest first")
    lead.add_argument("lead_id")
    lead.add_argument("--workspace", default="titan")
    lead.set_defaults(func=_run(_lead))


__all__ = ["add_events_parser"]
