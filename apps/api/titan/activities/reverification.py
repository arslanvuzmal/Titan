"""Re-check stored addresses on a schedule, instead of when somebody remembers.

The re-check itself has existed since 27 August and has never once run
unattended. :mod:`titan.intelligence.reverification` is reachable only from
``titan verify-contacts`` -- a command a human has to type -- which is the same
shape as the two defects that cost this workspace 596 stranded drafts and a
frozen warm-up ramp. A guard that runs when somebody thinks to run it is a
diagnostic, not a guard.

**Why it matters more than the other sweeps.** Every other bounce protection
answers a question once, at discovery, and stores the answer. This is the only
one that keeps those answers true. An address checked in July is protected by a
July answer, and the thing being guarded against is precisely the sort of change
that happens later: ``katie@reading-smiles.co.uk`` was a real mailbox belonging
to a real person, and it bounced because she left.

**Small batches, because each one is a stranger's mail server.** Housekeeping
runs hourly, so a batch of 25 is up to 600 addresses a day against a list of
2,300 -- a full sweep about every four days, which is well inside the 30-day
window an answer stands for. Raising it does not buy a fresher list, it just
spends other people's connections faster.

**Failure is swallowed by the caller and reported here.** A verification outage
must not fail a housekeeping pass that repaired stranded drafts, and the pass
must not retry into a provider that is already unwell -- ``reverify`` leaves the
stored status alone on an outage, so the address simply comes back next hour.
"""

from __future__ import annotations

import logging
import uuid

from temporalio import activity

from titan.config import get_settings
from titan.db.session import workspace_unit_of_work
from titan.intelligence.reverification import reverify
from titan.intelligence.verifier import build_verifier
from titan.workflows.types import ReverifyContactsInput, ReverifyContactsResult

logger = logging.getLogger(__name__)

#: Addresses per hourly pass.
#:
#: Sized against the window rather than against capacity: at 25 an hour the
#: whole list is re-checked roughly every four days, and an answer stands for
#: thirty. The remaining headroom is deliberate -- the machine sleeps, so the
#: real rate is lower than the nominal one, and a batch sized for a host that
#: is always up would under-deliver on this one and over-probe on a VPS.
HOURLY_BATCH = 25


@activity.defn(name="reverify_contacts")
async def reverify_contacts(request: ReverifyContactsInput) -> ReverifyContactsResult:
    """Re-check one batch of stored addresses and write what changed."""
    workspace_id = uuid.UUID(request.workspace_id)
    batch = request.batch or HOURLY_BATCH
    settings = get_settings()

    verifier = build_verifier(settings.mailbox_verifier, settings)
    if verifier.name == "null":
        # Not an error. The null verifier answers UNKNOWN for everything, so a
        # pass would examine the batch, learn nothing, and record a row saying
        # it had asked -- which would then exclude those addresses from the
        # next thirty days of real checks. Doing nothing is strictly better.
        return ReverifyContactsResult(
            reason=(
                f"TITAN_MAILBOX_VERIFIER is {settings.mailbox_verifier!r}, which "
                f"resolves to the null verifier; nothing would be learned"
            )
        )

    ok, detail = await verifier.health_check()
    if not ok:
        return ReverifyContactsResult(reason=f"{verifier.name} unhealthy: {detail}")

    # One committed transaction for the batch. The pass is small enough that a
    # single unit of work holds no lock long enough to matter, and short enough
    # that a crash loses one hour of checks rather than a day of them.
    async with workspace_unit_of_work(workspace_id) as session:
        report = await reverify(
            session,
            workspace_id=workspace_id,
            verifier=verifier,
            limit=batch,
            apply=True,
        )

    if report.downgraded:
        # Worth a line of its own at INFO: each entry is an address that was
        # sendable an hour ago and is not now, which is the only output of this
        # activity that changes what gets mailed.
        logger.info(
            "re-verification withdrew %s addresses from sending: %s",
            len(report.downgraded),
            ", ".join(report.downgraded[:10]),
        )

    return ReverifyContactsResult(
        examined=report.examined,
        checked=report.checked,
        changed=report.changed,
        downgraded=len(report.downgraded),
        reason="" if report.examined else "nothing due for a re-check",
    )


ALL_REVERIFICATION_ACTIVITIES = [reverify_contacts]

__all__ = ["ALL_REVERIFICATION_ACTIVITIES", "HOURLY_BATCH", "reverify_contacts"]
