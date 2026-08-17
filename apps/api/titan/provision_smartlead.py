"""Create the per-market Smartlead campaigns, and verify what was written.

    python -m titan.provision_smartlead            # show the plan, change nothing
    python -m titan.provision_smartlead --apply

There was one campaign in the account and its clock was ``Europe/London`` for
every lead in every market. This gives each market its own.

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
from dataclasses import dataclass
from typing import Any

from titan.config import get_settings
from titan.outreach.smartlead_markets import (
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


async def provision(*, apply: bool, attach: bool) -> list[Result]:
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
            if mailbox_ids:
                await client.attach_email_accounts(campaign_id, mailbox_ids)

            fresh = await client.get_campaign(campaign_id)
            ok, detail = _read_back(fresh.raw, wanted)
            results.append(Result(name, campaign_id, created, ok, detail))
    finally:
        await client.aclose()
    return results


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write to Smartlead")
    parser.add_argument(
        "--no-attach",
        action="store_true",
        help="skip attaching mailboxes; create the campaign and its clock only",
    )
    args = parser.parse_args()

    results = await provision(apply=args.apply, attach=not args.no_attach)
    print("dry run -- nothing written" if not args.apply else "applied")
    for result in results:
        mark = "+" if result.created else "="
        status = "ok " if result.schedule_ok else "BAD"
        campaign = result.campaign_id if result.campaign_id is not None else "-"
        print(f"  {mark} {status} {result.market:<24} {campaign!s:<9} {result.detail}")
    print()
    print("Campaigns are created DRAFTED. Nothing here starts one or attaches a lead.")
    return 0 if all(r.schedule_ok for r in results) else 1


if __name__ == "__main__":
    configure_event_loop()
    raise SystemExit(asyncio.run(main()))
