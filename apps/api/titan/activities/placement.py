"""The two halves of a placement round, as activities.

Two rather than one because the middle of a placement round is a wait. A filter
has not made up its mind the moment a message is accepted, so a round that sent
and checked in one breath would measure the race and report almost everything as
inbox -- which is where mail sits for a moment before it is moved.

The wait belongs to the workflow, which can sleep durably for an hour without
holding a worker. These are the parts either side of it.

**Neither activity raises on a misconfiguration.** No seeds and no mailboxes are
ordinary states for a deployment that has not set placement up yet, and a
retrying activity would turn that into an alert every hour forever. They return
``skipped`` instead, which the result carries and the report distinguishes from
a round that ran and found nothing.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections import Counter

from sqlalchemy import text
from temporalio import activity

from titan.config import get_settings
from titan.db.session import get_sessionmaker, workspace_unit_of_work
from titan.delivery.folder_search import find_probe
from titan.delivery.mailbox import ImapConfig
from titan.delivery.mailboxes import load_mailboxes
from titan.delivery.placement import record_result
from titan.delivery.placement_probe import plan_round, record_round, send_round
from titan.delivery.seeds import load_seeds
from titan.workflows.types import PlacementRoundInput, PlacementRoundResult

logger = logging.getLogger(__name__)

#: How far back the reader will look for probes nobody has read yet.
#:
#: Wider than one round on purpose. A worker that was down when the settle
#: timer fired leaves probes unchecked, and the next round should pick them up
#: rather than leave them null forever -- an unchecked probe reads as silence
#: in the report, which is the one thing this subsystem exists to stop.
UNCHECKED_WINDOW_DAYS = 3

UNCHECKED = text(
    """
    SELECT probe_token, seed_address, from_email
      FROM placement_checks
     WHERE workspace_id = :ws
       AND folder IS NULL
       AND sent_at > now() - make_interval(days => :days)
     ORDER BY sent_at
    """
)


@activity.defn(name="send_placement_probes")
async def send_placement_probes(request: PlacementRoundInput) -> PlacementRoundResult:
    """One probe from each sending mailbox to a seed we own."""
    settings = get_settings()
    seeds = load_seeds(settings.seed_file)
    if not len(seeds):
        return PlacementRoundResult(skipped="no seed mailboxes configured")

    registry = load_mailboxes(settings.mailbox_file)
    now = dt.datetime.now(dt.UTC)
    planned = plan_round(list(registry.addresses()), seeds, now=now)
    if not planned:
        return PlacementRoundResult(skipped="no enabled sending mailboxes")

    accepted = await send_round(
        registry,
        planned,
        timeout_seconds=float(settings.smtp_timeout_seconds),
        now=now,
    )

    workspace_id = uuid.UUID(request.workspace_id)
    async with workspace_unit_of_work(workspace_id) as session:
        recorded = await record_round(
            session,
            workspace_id=workspace_id,
            planned=planned,
            sent=accepted,
            now=now,
        )

    return PlacementRoundResult(sent=len(accepted), recorded=recorded)


@activity.defn(name="read_placement_probes")
async def read_placement_probes(request: PlacementRoundInput) -> PlacementRoundResult:
    """Look in each seed mailbox and record where its probe landed."""
    settings = get_settings()
    seeds = load_seeds(settings.seed_file)
    if not len(seeds):
        return PlacementRoundResult(skipped="no seed mailboxes configured")

    workspace_id = uuid.UUID(request.workspace_id)
    async with get_sessionmaker()() as session:
        pending = (
            (
                await session.execute(
                    UNCHECKED, {"ws": workspace_id, "days": UNCHECKED_WINDOW_DAYS}
                )
            )
            .mappings()
            .all()
        )
    if not pending:
        return PlacementRoundResult(skipped="no unchecked probes")

    by_address = {seed.address.lower(): seed for seed in seeds.all()}
    found: Counter[str] = Counter()
    for row in pending:
        seed = by_address.get(str(row["seed_address"]).lower())
        if seed is None:
            # Dropped from the seed file after the probe went out. Left
            # unchecked rather than guessed at: "we no longer hold the
            # credential" is a different fact from "it was not there", and
            # writing the second would put a fabricated reading into a series
            # whose entire value is being comparable week to week.
            continue

        verdict = await find_probe(
            ImapConfig(
                host=seed.imap.host,
                port=seed.imap.port,
                username=seed.imap.username,
                password=seed.imap.password,
                security=seed.imap.security,
            ),
            provider=seed.provider,
            probe_token=str(row["probe_token"]),
        )
        async with workspace_unit_of_work(workspace_id) as session:
            await record_result(
                session,
                workspace_id=workspace_id,
                probe_token=str(row["probe_token"]),
                seed_address=seed.address,
                folder=verdict.folder,
                checked_by="imap",
                note=(
                    f"found in {verdict.found_in}"
                    if verdict.found_in
                    else f"searched {', '.join(verdict.searched) or 'nothing'}"
                ),
            )
        found[verdict.folder] += 1

    return PlacementRoundResult(
        recorded=sum(found.values()),
        folders=tuple(sorted(found.items())),
    )


__all__ = [
    "UNCHECKED_WINDOW_DAYS",
    "read_placement_probes",
    "send_placement_probes",
]
