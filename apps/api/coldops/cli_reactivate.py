"""``coldops reactivate`` -- may cold mail be switched back on, and what would go out?

    coldops reactivate --workspace titan

Read-only: it reports and changes nothing. Switching mailboxes back on, and
withdrawing a stale backlog, stay deliberate acts by the operator.

Cold mail stops in two places: every mailbox switched off (``is_active``), and
the placement gate resting the domain after spam readings. Switching the
mailboxes back on is the one act that restarts it, and on 10 Oct 2026 the
end-to-end test showed what that act would have released: ten auto-approved
pitches queued a week or two earlier, and a ramp that read five mailboxes new
to Google Workspace as warm since August. This is the checklist in front of it.

The bar is the operator's (3 Oct): **at least 80% inbox on the placement
probes, every measured day for 14 days**, the domain not resting, and today's
gate verdict clear -- per mailbox.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from collections import defaultdict

from sqlalchemy import select, text

from coldops.config import get_settings

#: The operator's reactivation bar.
STREAK_DAYS = 14
DAILY_FLOOR = 0.80
#: A probe round can be missed; this many measured days in the window is the
#: least that still counts as two weeks of evidence.
MIN_MEASURED_DAYS = 10
#: Queued or approved longer ago than this should not go out on reactivation:
#: the findings behind it are that old, and so is the decision to send it.
STALE_DAYS = 7


async def _run(args: argparse.Namespace) -> int:
    from coldops.db.models import SenderIdentity, Workspace
    from coldops.db.models.identity import SENDER_INACTIVE
    from coldops.db.session import get_sessionmaker
    from coldops.delivery import deliverability, placement_gate
    from coldops.delivery.outbox_worker import _ramp_start

    settings = get_settings()
    now = dt.datetime.now(dt.UTC)
    cutoff = now - dt.timedelta(days=STALE_DAYS)

    async with get_sessionmaker()() as session:
        ws = (
            await session.execute(
                select(Workspace).where(Workspace.slug == args.workspace)
            )
        ).scalar_one_or_none()
        if ws is None:
            print(f"no workspace with slug {args.workspace!r}")
            return 1
        senders = list(
            (
                await session.execute(
                    select(SenderIdentity)
                    .where(SenderIdentity.workspace_id == ws.id)
                    .order_by(SenderIdentity.from_email)
                )
            )
            .scalars()
            .all()
        )
        readings = await placement_gate.load_readings(
            session,
            workspace_id=ws.id,
            domains={placement_gate.domain_of(s.from_email) for s in senders},
            now=now,
        )
        first_sends = {
            r.sender_identity_id: r.first
            for r in (
                await session.execute(
                    text(
                        "SELECT sender_identity_id, min(sent_at) AS first FROM messages "
                        "WHERE workspace_id = :ws AND sent_at IS NOT NULL "
                        "GROUP BY sender_identity_id"
                    ),
                    {"ws": ws.id},
                )
            ).all()
        }
        queue = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FILTER (WHERE created_at < :cutoff) AS stale,
                           count(*) AS total
                      FROM outbox_messages
                     WHERE workspace_id = :ws
                       AND status IN ('pending', 'deferred', 'leased')
                    """
                ),
                {"ws": ws.id, "cutoff": cutoff},
            )
        ).one()
        approved = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FILTER (WHERE d.created_at < :cutoff) AS stale,
                           count(*) AS total
                      FROM message_drafts d
                     WHERE d.workspace_id = :ws
                       AND d.status = 'approved'
                       AND NOT EXISTS (SELECT 1 FROM outbox_messages o
                                        WHERE o.draft_id = d.id)
                    """
                ),
                {"ws": ws.id, "cutoff": cutoff},
            )
        ).one()

    since = (now - dt.timedelta(days=STREAK_DAYS)).date()
    daily: dict[str, dict[dt.date, list[int]]] = defaultdict(
        lambda: defaultdict(lambda: [0, 0])
    )
    for r in readings:
        day = r.sent_at.astimezone(dt.UTC).date()
        if day >= since:
            cell = daily[r.from_email.lower()][day]
            cell[0] += int(r.reached)
            cell[1] += 1

    print(f"Reactivation check for {args.workspace}, {now:%Y-%m-%d %H:%M} UTC")
    print(
        f"Bar: every measured day >= {DAILY_FLOOR:.0%} inbox over {STREAK_DAYS} days, "
        f"at least {MIN_MEASURED_DAYS} days measured, domain not resting, gate clear."
    )
    print()

    ready = 0
    for s in senders:
        address = s.from_email.lower()
        days = daily.get(address, {})
        bad = sorted(d for d, (hit, n) in days.items() if hit / n < DAILY_FLOOR)
        hits = sum(h for h, _ in days.values())
        probes = sum(n for _, n in days.values())
        verdict = placement_gate.assess(address, readings, now=now)
        start = _ramp_start(first_sends.get(s.id), s.warmup_started_at)
        limit = deliverability.warmup_limit(
            first_send_at=start, now=now, target=s.daily_send_limit
        )

        problems: list[str] = []
        if len(days) < MIN_MEASURED_DAYS:
            problems.append(f"{len(days)} of {MIN_MEASURED_DAYS} days measured so far")
        if bad:
            problems.append(
                "under the bar on " + ", ".join(f"{d:%d %b}" for d in bad[-3:])
            )
        if not verdict.may_send:
            problems.append(verdict.detail)
        problems.extend(e for e in s.authorization_errors() if e != SENDER_INACTIVE)

        share = f"{hits}/{probes} inbox" if probes else "no readings"
        print(
            f"  {address:<36} {'on ' if s.is_active else 'off'}  {share:<14} "
            f"ramp day {deliverability.warmup_day(start, now)}, cold limit today "
            f"{limit if limit is not None else s.daily_send_limit} of {s.daily_send_limit}"
        )
        for p in problems:
            print(f"      not yet: {p}")
        if not problems:
            print("      READY")
            ready += 1

    print()
    print(
        f"Would go out the moment mail is switched on (stale = over {STALE_DAYS} days old):"
    )
    print(f"  queued emails       {queue.total:>4}   stale {queue.stale}")
    print(f"  approved, unqueued  {approved.total:>4}   stale {approved.stale}")
    print()
    if not settings.production_sending_enabled:
        print("  not yet: COLDOPS_PRODUCTION_SENDING_ENABLED is off")
    if not ws.sending_authorized:
        print("  not yet: the workspace is not authorized to send")
    if not settings.placement_gate_enabled:
        print("  not yet: the placement gate is off; it must stay on for cold mail")
    if settings.test_recipients:
        print(
            "  note: COLDOPS_TEST_RECIPIENTS is still set "
            f"({', '.join(settings.test_recipients)}); remove it when testing is done"
        )
    print()
    print(f"{ready} of {len(senders)} mailboxes meet the bar. Nothing was changed.")
    return 0


def add_reactivate_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = sub.add_parser(
        "reactivate",
        help="read-only checklist: may cold mail be switched back on, what would go out",
    )
    parser.add_argument("--workspace", default="titan")
    parser.set_defaults(func=cmd_reactivate)


def cmd_reactivate(args: argparse.Namespace) -> int:
    from coldops.db.session import dispose_engine
    from coldops.runtime import configure_event_loop

    async def run() -> int:
        try:
            return await _run(args)
        finally:
            await dispose_engine()

    configure_event_loop()
    return asyncio.run(run())


__all__ = ["add_reactivate_parser", "cmd_reactivate"]
