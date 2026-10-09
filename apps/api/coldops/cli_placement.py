"""``coldops placement`` -- send the probes, read the folders, print the answer.

Three subcommands rather than one, because the middle step is a wait. A filter
has not made up its mind the moment a message is accepted, so checking straight
after sending measures the race rather than the filter. An hour between ``send``
and ``check`` is the smallest honest gap.

Split out of ``cli.py`` rather than added to it: that file is past two and a
half thousand lines and every command in it has to be read to find any one of
them. The argument parser still lives there, because there is one parser.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import uuid

from sqlalchemy import select, text

from coldops.config import get_settings


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


async def _report(workspace_id: uuid.UUID, *, days: int) -> None:
    from coldops.db.session import get_sessionmaker
    from coldops.delivery.placement import by_mailbox
    from coldops.delivery.placement_report import placements, render

    async with get_sessionmaker()() as session:
        rows = await by_mailbox(session, workspace_id=workspace_id, days=days)
    print(render(placements(rows), days=days))


async def _send(workspace_id: uuid.UUID, seeds) -> int:
    from coldops.db.session import workspace_unit_of_work
    from coldops.delivery.mailboxes import load_mailboxes
    from coldops.delivery.placement_probe import plan_round, record_round, send_round

    settings = get_settings()
    registry = load_mailboxes(settings.mailbox_file)
    now = dt.datetime.now(dt.UTC)

    planned = plan_round(list(registry.addresses()), seeds, now=now)
    if not planned:
        print("Nothing to probe: no enabled mailboxes.")
        return 1

    accepted = await send_round(
        registry,
        planned,
        timeout_seconds=float(settings.smtp_timeout_seconds),
        now=now,
    )
    for probe in planned:
        mark = "sent" if probe.probe_token in accepted else "FAILED"
        print(f"  {probe.from_email:<34} -> {probe.seed.address:<28} {mark}")

    async with workspace_unit_of_work(workspace_id) as session:
        written = await record_round(
            session,
            workspace_id=workspace_id,
            planned=planned,
            sent=accepted,
            now=now,
        )

    print()
    print(f"{written} probe(s) recorded. Read them in about an hour:")
    print("  coldops placement check")
    return 0 if written == len(planned) else 1


#: How far back ``check`` will look for probes nobody has read yet.
#:
#: Three days rather than forever. A probe older than that has usually been
#: superseded by two more rounds to the same pairing, and re-reading it costs
#: an IMAP login to learn something the newer readings already say. Probes
#: past the window keep their null folder, which the report counts as
#: unchecked rather than quietly dropping.
UNCHECKED_WINDOW_DAYS = 3

PENDING = text(
    """
    SELECT probe_token, seed_address, provider, from_email
      FROM placement_checks
     WHERE workspace_id = :ws
       AND folder IS NULL
       AND sent_at > now() - make_interval(days => :days)
     ORDER BY sent_at
    """
)


async def _check(workspace_id: uuid.UUID, seeds) -> int:
    from coldops.db.session import get_sessionmaker, workspace_unit_of_work
    from coldops.delivery.folder_search import find_probe
    from coldops.delivery.mailbox import ImapConfig
    from coldops.delivery.placement import record_result

    async with get_sessionmaker()() as session:
        pending = (
            (
                await session.execute(
                    PENDING, {"ws": workspace_id, "days": UNCHECKED_WINDOW_DAYS}
                )
            )
            .mappings()
            .all()
        )

    if not pending:
        print(f"No unchecked probes in the last {UNCHECKED_WINDOW_DAYS} days.")
        return 0

    by_address = {seed.address.lower(): seed for seed in seeds.all()}
    checked = 0
    for row in pending:
        seed = by_address.get(str(row["seed_address"]).lower())
        if seed is None:
            # Removed from the seed file after the probe went out. Left
            # unchecked rather than guessed at: "we no longer hold the
            # credential" is a different fact from "it was not there", and
            # recording the second would put a fabricated reading in a series
            # whose whole value is being comparable over weeks.
            print(f"  {row['seed_address']}: no longer in the seed file, skipped")
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
        note = (
            f"found in {verdict.found_in}"
            if verdict.found_in
            else f"searched {', '.join(verdict.searched) or 'nothing'}"
        )
        async with workspace_unit_of_work(workspace_id) as session:
            await record_result(
                session,
                workspace_id=workspace_id,
                probe_token=str(row["probe_token"]),
                seed_address=seed.address,
                folder=verdict.folder,
                checked_by="imap",
                note=note,
            )
        print(f"  {row['from_email']:<34} -> {seed.address:<28} {verdict.folder}")
        checked += 1

    print()
    await _report(workspace_id, days=14)
    return 0 if checked else 1


def cmd_placement(args: argparse.Namespace) -> int:
    from coldops.db.session import dispose_engine
    from coldops.delivery.seeds import load_seeds
    from coldops.runtime import configure_event_loop

    async def run() -> int:
        workspace_id = await _workspace_id(args.workspace)
        try:
            if args.placement_command == "report":
                await _report(workspace_id, days=args.days)
                return 0

            seeds = load_seeds(get_settings().seed_file)
            if not len(seeds):
                # Refused rather than reported as a clean round of nothing. A
                # placement table that stops gaining rows looks exactly like
                # placement that has stopped being a problem, and that
                # confusion is what this whole subsystem exists to end.
                print("No seed mailboxes configured. Set COLDOPS_SEED_FILE.")
                print("Copy secrets/seeds.json.example and fill in two app passwords.")
                return 1

            if args.placement_command == "send":
                return await _send(workspace_id, seeds)
            return await _check(workspace_id, seeds)
        finally:
            await dispose_engine()

    configure_event_loop()
    return asyncio.run(run())


def add_placement_parser(sub) -> None:
    """Register ``coldops placement`` on the main parser."""
    parser = sub.add_parser(
        "placement",
        help="whether our mail is reaching inboxes, measured rather than assumed",
    )
    commands = parser.add_subparsers(dest="placement_command", required=True)

    send = commands.add_parser(
        "send", help="one probe from each mailbox to a seed we own"
    )
    check = commands.add_parser(
        "check", help="read the seeds and record where each probe landed"
    )
    report = commands.add_parser(
        "report", help="placement per mailbox and provider, worst first"
    )
    report.add_argument("--days", type=int, default=14)

    for one in (send, check, report):
        one.add_argument("--workspace", default="titan")

    parser.set_defaults(func=cmd_placement)


__all__ = ["UNCHECKED_WINDOW_DAYS", "add_placement_parser", "cmd_placement"]
