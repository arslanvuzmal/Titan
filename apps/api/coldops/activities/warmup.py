"""Warm-up traffic, as a daily activity rather than a command somebody runs.

The module that sends and tends warm-up mail has existed since August and has
only ever been reachable from ``coldops warmup run --apply``. Nothing scheduled
it, so after the rented carrier's warm-up stopped on 24 August the domain had
none at all -- through the week it sent 492 cold messages and was filed as
spam by every probe that was read.

Returns ``skipped`` for every ordinary unconfigured state rather than raising,
for the same reason the placement activities do: a retrying activity would turn
"not set up yet" into an alert every day forever.
"""

from __future__ import annotations

import uuid

from temporalio import activity

from coldops.config import get_settings
from coldops.delivery.mailboxes import MailboxConfigError, load_mailboxes
from coldops.delivery.seeds import load_seeds
from coldops.delivery.warmup import (
    check_recipients_are_participants,
    involving_ours,
    participants_from,
    plan,
    round_pool,
    send_round,
    tend,
)
from coldops.workflows.types import WarmupRoundInput, WarmupRoundResult


@activity.defn(name="run_warmup_round")
async def run_warmup_round(request: WarmupRoundInput) -> WarmupRoundResult:
    """Send today's warm-up, then rescue, read and answer what arrived."""
    settings = get_settings()
    if not settings.warmup_enabled:
        return WarmupRoundResult(skipped="warm-up is switched off")
    if not settings.mailbox_file:
        return WarmupRoundResult(skipped="no mailbox file")
    if not settings.warmup_partner_file:
        # Not a softer "fewer signals" state: without a mailbox on another
        # provider every message stays on one server and teaches nobody
        # anything. Refusing says so; running would look like progress.
        return WarmupRoundResult(
            skipped="no warm-up partners; mail between our own mailboxes "
            "never reaches Gmail or Microsoft"
        )

    try:
        sending_registry = load_mailboxes(settings.mailbox_file)
        partner_registry = load_mailboxes(settings.warmup_partner_file)
    except MailboxConfigError as exc:
        return WarmupRoundResult(skipped=f"mailbox file unusable: {exc}")

    # The CLI's own helper, so the scheduled round and the manual one agree
    # about which day of its ramp each mailbox is on.
    from coldops.cli import _warmup_days

    days = await _warmup_days(uuid.UUID(request.workspace_id))
    sending = participants_from(sending_registry, days=days)
    partners = participants_from(partner_registry)
    seeds = {seed.address for seed in load_seeds(settings.seed_file).all()}
    pool = round_pool(sending, partners, seed_addresses=seeds)

    today = involving_ours(plan(pool), sending)
    check_recipients_are_participants(today, pool)
    timeout = float(settings.smtp_timeout_seconds)
    sent = await send_round(today, timeout_seconds=timeout)
    tended = await tend(pool, timeout_seconds=timeout)

    activity.logger.info(
        "warm-up round",
        extra={
            "sent": sent.sent,
            "failed": sent.failed,
            "rescued": tended.rescued_from_spam,
            "replied": tended.replied,
        },
    )
    return WarmupRoundResult(
        planned=len(today),
        sent=sent.sent,
        failed=sent.failed,
        rescued_from_spam=tended.rescued_from_spam,
        marked_read=tended.marked_read,
        replied=tended.replied,
        errors=tuple((sent.errors + tended.errors)[:10]),
    )


ALL_WARMUP_ACTIVITIES = [run_warmup_round]

__all__ = ["ALL_WARMUP_ACTIVITIES", "run_warmup_round"]
