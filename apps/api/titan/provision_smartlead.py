"""Create the per-market Smartlead campaigns, and verify what was written.

    python -m titan.provision_smartlead            # show the plan, change nothing
    python -m titan.provision_smartlead --apply
    python -m titan.provision_smartlead --apply --record <workspace-id>

There was one campaign in the account and its clock was ``Europe/London`` for
every lead in every market. This gives each market its own.

**Creating them is half the job.** ``--record`` writes each carrier's id onto
the Titan campaigns for that market, which is what the delivery path reads.
Without it the ids exist only in Smartlead, ``campaigns.smartlead_campaign_id``
stays null everywhere, and every message keeps leaving through the single
carrier in ``TITAN_SMARTLEAD_CAMPAIGN_ID`` -- so the markets would be provisioned
and unused, which looks like success and changes nothing.

**Every write is read back.** ``POST /campaigns/{id}/schedule`` returns a
success shape whether or not it understood the body, and the account then holds
a campaign that looks configured and sends on the wrong days. So each schedule
is re-fetched from ``GET /campaigns/{id}`` and compared against what was
intended, and a mismatch is reported as a failure rather than a note.

**Created, never started.** Smartlead creates a campaign DRAFTED and this
leaves it there. Attaching leads stays where it was: behind verification,
approval, suppression and compliance, through the existing import path.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select

from titan.config import get_settings
from titan.db.enums import Region
from titan.db.models import Campaign
from titan.db.session import workspace_unit_of_work
from titan.outreach.smartlead_markets import (
    CARRIER_SETTINGS,
    SEQUENCE_STEPS,
    MarketSchedule,
    all_schedules,
    campaign_name,
    daily_capacity,
    excluded_mailboxes,
)
from titan.providers.smartlead import SmartleadClient
from titan.runtime import configure_event_loop

#: Held out of every outreach campaign under the operator's standing rule. A
#: real working mailbox whose reputation was never meant to carry cold mail.
FORBIDDEN_MAILBOXES: frozenset[str] = frozenset({"projects@arslanvuzmallone.com"})


@dataclass(frozen=True, slots=True)
class Result:
    market: str
    campaign_id: int | None
    created: bool
    schedule_ok: bool
    detail: str
    #: Titan campaigns pointed at this carrier by ``--record``. None means the
    #: writeback was not asked for, which reads differently from zero matched.
    recorded: int | None = None


async def record_carriers(
    workspace_id: uuid.UUID, carriers: dict[Region, int]
) -> dict[Region, int]:
    """Point each Titan campaign at the carrier for its own market.

    Without this the ``smartlead_campaign_id`` column stays null on every row
    and delivery falls back to ``TITAN_SMARTLEAD_CAMPAIGN_ID`` -- which is the
    single-carrier behaviour these per-market campaigns exist to replace. So
    creating them in Smartlead is only half the job; this is the half that makes
    a Dubai lead leave through the Dubai campaign.

    Scoped to one workspace and written through the ORM, so the workspace guard
    applies. Returns the number of campaigns updated per market.
    """
    updated: dict[Region, int] = {}
    async with workspace_unit_of_work(workspace_id) as session:
        for region, carrier_id in carriers.items():
            campaigns = (
                (await session.execute(select(Campaign).where(Campaign.region == region)))
                .scalars()
                .all()
            )
            for campaign in campaigns:
                campaign.smartlead_campaign_id = carrier_id
            updated[region] = len(campaigns)
    return updated


def _read_back(campaign: dict[str, Any], wanted: MarketSchedule) -> tuple[bool, str]:
    """Whether the account now holds the schedule that was sent.

    Smartlead reports the schedule under ``scheduler_cron_value`` with different
    key names than the write accepts -- ``tz``, ``startHour``, ``endHour`` --
    which is exactly why this compares values rather than trusting the response
    to the write.
    """
    cron = campaign.get("scheduler_cron_value") or {}
    got_tz = str(cron.get("tz") or "")
    got_days = tuple(int(day) for day in (cron.get("days") or ()))
    got_start = str(cron.get("startHour") or "")
    got_end = str(cron.get("endHour") or "")

    problems = []
    if got_tz != wanted.timezone:
        problems.append(f"tz {got_tz!r} != {wanted.timezone!r}")
    if got_days != wanted.days:
        problems.append(f"days {list(got_days)} != {list(wanted.days)}")
    if got_start != wanted.start_hour:
        problems.append(f"start {got_start!r} != {wanted.start_hour!r}")
    if got_end != wanted.end_hour:
        problems.append(f"end {got_end!r} != {wanted.end_hour!r}")
    if problems:
        return False, "; ".join(problems)
    return True, wanted.describe()


async def _existing(client: SmartleadClient) -> dict[str, int]:
    return {c.name.strip(): c.id for c in await client.list_campaigns()}


async def provision(
    *, apply: bool, attach: bool, workspace_id: uuid.UUID | None = None
) -> list[Result]:
    settings = get_settings()
    if settings.smartlead_api_key is None:
        raise SystemExit("TITAN_SMARTLEAD_API_KEY is not configured")

    client = SmartleadClient.from_settings(settings)
    results: list[Result] = []
    try:
        by_name = await _existing(client)
        accounts = await client.list_email_accounts()
        forbidden = set(FORBIDDEN_MAILBOXES)
        # What the platform itself permits, read from the account rather than
        # chosen here. Smartlead still enforces each mailbox's own limit, so
        # this cannot be used to push any single mailbox past its setting.
        capacity = daily_capacity(accounts, forbidden=forbidden)
        mailbox_ids = excluded_mailboxes(accounts, forbidden=forbidden) if attach else []
        carriers: dict[Region, int] = {}

        for wanted in all_schedules():
            name = campaign_name(wanted.region)
            campaign_id = by_name.get(name)
            created = False

            if not apply:
                results.append(
                    Result(
                        name,
                        campaign_id,
                        created=campaign_id is None,
                        schedule_ok=True,
                        detail=f"{wanted.describe()} max_new_leads/day={capacity}",
                    )
                )
                continue

            if campaign_id is None:
                campaign_id = (await client.create_campaign(name)).id
                created = True

            await client.set_schedule(
                campaign_id, wanted.body(max_new_leads_per_day=capacity)
            )
            await client.set_sequences(campaign_id, list(SEQUENCE_STEPS))
            # Smartlead's defaults are not the operator's. A campaign created
            # here otherwise ships with link and open tracking on, an HTML
            # body, and no unsubscribe text -- three differences from the
            # campaign that has actually been sending, all of which cost
            # deliverability.
            await client.update_settings(campaign_id, dict(CARRIER_SETTINGS))
            if mailbox_ids:
                await client.attach_email_accounts(campaign_id, mailbox_ids)

            fresh = await client.get_campaign(campaign_id)
            ok, detail = _read_back(fresh.raw, wanted)
            results.append(Result(name, campaign_id, created, ok, detail))
            if ok:
                # Only a carrier whose clock read back correctly. Routing leads
                # into one that failed verification would schedule them against
                # a schedule nobody has confirmed, which is the exact failure
                # the read-back exists to catch.
                carriers[wanted.region] = campaign_id
    finally:
        await client.aclose()

    if workspace_id is not None and carriers:
        updated = await record_carriers(workspace_id, carriers)
        by_region = {campaign_name(region): count for region, count in updated.items()}
        results = [
            replace(result, recorded=by_region.get(result.market))
            if result.market in by_region
            else result
            for result in results
        ]
    return results


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write to Smartlead")
    parser.add_argument(
        "--no-attach",
        action="store_true",
        help="skip attaching mailboxes; create the campaign and its clock only",
    )
    parser.add_argument(
        "--record",
        metavar="WORKSPACE_ID",
        help=(
            "point this workspace's campaigns at the carrier for their market. "
            "Without it the ids are created in Smartlead and nothing in Titan "
            "knows about them, so delivery keeps using the single configured "
            "carrier."
        ),
    )
    args = parser.parse_args()

    workspace_id = uuid.UUID(args.record) if args.record else None
    if workspace_id is not None and not args.apply:
        raise SystemExit("--record writes to the database; it requires --apply")

    results = await provision(
        apply=args.apply, attach=not args.no_attach, workspace_id=workspace_id
    )
    print("dry run -- nothing written" if not args.apply else "applied")
    for result in results:
        mark = "+" if result.created else "="
        status = "ok " if result.schedule_ok else "BAD"
        campaign = result.campaign_id if result.campaign_id is not None else "-"
        recorded = "" if result.recorded is None else f" [{result.recorded} campaigns]"
        print(
            f"  {mark} {status} {result.market:<24} {campaign!s:<9} "
            f"{result.detail}{recorded}"
        )
    print()
    print("Campaigns are created DRAFTED. Nothing here starts one or attaches a lead.")
    return 0 if all(r.schedule_ok for r in results) else 1


if __name__ == "__main__":
    configure_event_loop()
    raise SystemExit(asyncio.run(main()))
