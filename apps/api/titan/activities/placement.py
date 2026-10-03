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
from titan.notify.operator import NotificationKind, record_notification
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
                auth=seed.imap.auth,
                client_id=seed.imap.client_id,
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

    if settings.placement_gate_enabled:
        await _alert_on_failing_mailboxes(workspace_id, list(registry_addresses()))

    return PlacementRoundResult(
        recorded=sum(found.values()),
        folders=tuple(sorted(found.items())),
    )


def registry_addresses() -> list[str]:
    """Every sending mailbox in the pool file, or none when it is unset."""
    settings = get_settings()
    if not settings.mailbox_file:
        return []
    return list(load_mailboxes(settings.mailbox_file).addresses())


async def _alert_on_failing_mailboxes(
    workspace_id: uuid.UUID, mailboxes: list[str]
) -> list[str]:
    """Tell the operator, once a day per mailbox, that one has stopped sending.

    Run straight after the readings are written, because that is the moment a
    mailbox's verdict changes. The gate itself needs nobody to act -- it stops
    the mailbox and lets it resume on its own -- but a mailbox going quiet is
    capacity disappearing, and the operator should hear it from the system
    rather than notice it in the send count three days later.

    Returns the addresses an email went out for. A mail failure is logged and
    swallowed: the readings are already committed and are the thing that
    matters; the alert is a courtesy on top of them.
    """
    from titan.delivery import placement_gate
    from titan.notify.operator_mail import mail_the_operator

    now = dt.datetime.now(dt.UTC)
    mailed: list[str] = []
    async with workspace_unit_of_work(workspace_id) as session:
        # Only mailboxes that are meant to carry outreach. A resting mailbox
        # is still probed every morning -- that is how it earns its way back --
        # but its failing a reading is expected, not news.
        active = {
            str(a).strip().lower()
            for a in (
                await session.execute(
                    text(
                        "SELECT from_email FROM sender_identities "
                        "WHERE workspace_id = :ws AND is_active"
                    ),
                    {"ws": workspace_id},
                )
            ).scalars()
        }
        watched = [m for m in mailboxes if m.strip().lower() in active]
        verdicts = await placement_gate.verdicts_for(
            session, workspace_id=workspace_id, mailboxes=watched, now=now
        )
        fresh = []
        for address, verdict in sorted(verdicts.items()):
            if verdict.may_send:
                continue
            note = await record_notification(
                session,
                workspace_id=workspace_id,
                kind=NotificationKind.DELIVERABILITY_ALERT,
                title=f"{address} paused: {verdict.code}",
                description=verdict.detail,
                # One per mailbox, per reason, per day. A mailbox that stays
                # under the floor for a week is one email a day, not one per
                # round, and a change of reason is news worth a second one.
                dedupe_key=f"placement:{address}:{verdict.code}:{now:%Y-%m-%d}",
                now=now,
            )
            if note is not None:
                fresh.append(verdict)

    for verdict in fresh:
        try:
            await mail_the_operator(
                subject=f"[Titan] {verdict.from_email} paused for placement",
                body=(
                    f"{verdict.detail}.\n\n"
                    "No cold mail leaves this mailbox until its own probes read "
                    "inbox again. Nothing needs doing to resume it: the morning "
                    "probe round keeps measuring it, and it starts sending on its "
                    "own once it is back at or above the floor.\n"
                ),
            )
            mailed.append(verdict.from_email)
        except Exception:
            logger.exception(
                "placement alert mail failed", extra={"mailbox": verdict.from_email}
            )
    return mailed


__all__ = [
    "UNCHECKED_WINDOW_DAYS",
    "read_placement_probes",
    "send_placement_probes",
]
