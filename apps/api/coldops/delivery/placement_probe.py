"""Sending the probes, and deciding how few will do.

A probe is one message from one sending mailbox to one seed we own. Sent so
that an hour later something can look in that seed and record where the filter
put it. That is the only way to observe placement: no provider reports the
folder it filed a stranger's mail into, and "delivered" is equally true of junk.

**The rotation is the whole design decision here.** Every mailbox to every seed
is the obvious scheme and it is too expensive: five mailboxes and three seeds
is fifteen messages a round, against a Monday budget of sixty. A quarter of the
day's sending spent measuring the day's sending.

So each mailbox probes one provider per round, and which one advances daily.
Five messages a round, every mailbox-provider pair read every third day, and
the overhead falls from 25% to 8%. What is given up is same-day comparison
across providers from one mailbox -- and that was never the question. The
question is whether a mailbox's placement is moving over weeks, which a reading
every third day answers perfectly well.

**Probes are counted, not exempted.** They go out through the same pool as
everything else and they are real mail to a real Gmail account. Exempting them
from the daily cap would mean a mailbox at its limit quietly sending five more,
which is the sort of accounting that makes a warm-up ramp meaningless.

**This is the one module outside the outbox worker that may reach a provider,
and the exemption is kept as narrow as the daily report's.** ``send_round``
takes no recipient argument: every address it writes to comes from the seed
registry, which lives in its own file and refuses any entry carrying SMTP
credentials. There is no argument anybody could pass to point it at a prospect.

Probes deliberately do *not* go through the outbox. The outbox writes a
``messages`` row per send, and five probes a day in that table would be five
sends a day in every count the estate reports -- the daily report would say
sixty-five where sixty reached a prospect, and every deliverability ratio would
be computed over a denominator padded with our own mail to ourselves.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

from coldops.delivery.placement import ProbeRow, new_probe_token, record_sent
from coldops.delivery.providers.base import OutboundEmail
from coldops.delivery.providers.smtp_pool import SmtpPoolProvider
from coldops.delivery.seeds import Seed, SeedRegistry

logger = logging.getLogger(__name__)

#: How the probe announces itself in its own subject.
#:
#: The token has to be in the subject because that is what the checker can
#: search for over plain IMAP; ``HEADER X-ColdOps-Probe`` is supported unevenly
#: and silently returns nothing where it is not, which reads exactly like a
#: message that never arrived.
#:
#: The word "delivery check" is there for the human who finds one of these in
#: their own spam folder in six months and wonders what it is.
SUBJECT = "ColdOps delivery check {token}"

BODY = """This is an automated delivery check sent by ColdOps at {stamp}.

It went from {from_email} to this mailbox to find out which folder it
lands in. Nobody needs to do anything with it.

{token}
"""


@dataclass(frozen=True, slots=True)
class PlannedProbe:
    """One message to send, decided before anything is sent."""

    from_email: str
    seed: Seed
    probe_token: str

    @property
    def subject(self) -> str:
        return SUBJECT.format(token=self.probe_token)

    def body(self, *, now: dt.datetime) -> str:
        return BODY.format(
            stamp=now.strftime("%Y-%m-%d %H:%M:%SZ"),
            from_email=self.from_email,
            token=self.probe_token,
        )


def rotation_day(now: dt.datetime) -> int:
    """Which step of the rotation today is.

    Counted in whole days from the epoch rather than from a stored cursor. A
    cursor would need somewhere to live and a decision about what happens when
    a round is skipped; the date already increments reliably and a skipped day
    simply means that pairing waits for its next turn rather than the whole
    rotation stalling a day behind forever.
    """
    return now.date().toordinal()


def plan_round(
    mailboxes: list[str], seeds: SeedRegistry, *, now: dt.datetime
) -> list[PlannedProbe]:
    """One probe per sending mailbox, each to a different provider today.

    Returns empty when there is nothing to measure with, and the caller treats
    that as a reason to refuse the round rather than to report a clean one: a
    placement table that stops gaining rows looks identical to placement that
    has stopped being a problem.
    """
    providers = seeds.providers()
    if not providers or not mailboxes:
        return []

    day = rotation_day(now)
    planned: list[PlannedProbe] = []
    for index, from_email in enumerate(sorted(mailboxes)):
        # Offset by the mailbox's position as well as the day, so five
        # mailboxes on a three-provider rotation cover all three every round
        # instead of all pointing at the same one and rotating in lockstep.
        provider = providers[(index + day) % len(providers)]
        candidates = seeds.for_provider(provider)
        if not candidates:
            continue
        seed = candidates[day % len(candidates)]
        planned.append(
            PlannedProbe(from_email=from_email, seed=seed, probe_token=new_probe_token())
        )
    return planned


async def send_round(
    registry,
    planned: list[PlannedProbe],
    *,
    timeout_seconds: float,
    now: dt.datetime,
) -> set[str]:
    """Send each planned probe. Returns the tokens the provider accepted.

    No recipient parameter, by design -- see the module docstring. The only
    addresses reachable from here are the ones ``plan_round`` read out of the
    seed registry.

    A refusal is logged and the loop continues. One mailbox whose SMTP is
    broken is a fact about that mailbox and precisely what a placement round
    exists to surface; aborting would throw away the readings for the four
    that worked.
    """
    provider = SmtpPoolProvider(registry, timeout_seconds=timeout_seconds)
    accepted: set[str] = set()
    for probe in planned:
        result = await provider.send(
            OutboundEmail(
                to_email=probe.seed.address,
                from_email=probe.from_email,
                from_name="ColdOps",
                reply_to=probe.from_email,
                subject=probe.subject,
                text_body=probe.body(now=now),
                idempotency_key=f"placement:{probe.probe_token}",
            )
        )
        if result.accepted:
            accepted.add(probe.probe_token)
        else:
            logger.warning(
                "placement probe refused",
                extra={
                    "from_email": probe.from_email,
                    "seed": probe.seed.address,
                    "error": result.error_detail,
                },
            )
    return accepted


async def record_round(
    session,
    *,
    workspace_id: uuid.UUID,
    planned: list[PlannedProbe],
    sent: set[str],
    now: dt.datetime,
) -> int:
    """Write a row for each probe that the provider accepted.

    Only the accepted ones. A row for a probe that never left would be
    unfindable an hour later and would record itself as ``missing`` -- the
    same value a message the filter silently dropped produces, and the two
    must not be confusable.
    """
    written = 0
    for probe in planned:
        if probe.probe_token not in sent:
            continue
        await record_sent(
            session,
            workspace_id=workspace_id,
            probe=ProbeRow(
                probe_token=probe.probe_token,
                seed_address=probe.seed.address,
                provider=probe.seed.provider,
                from_email=probe.from_email,
                subject=probe.subject,
                had_attachment=False,
                sent_at=now,
            ),
        )
        written += 1
    return written


__all__ = [
    "BODY",
    "SUBJECT",
    "PlannedProbe",
    "plan_round",
    "record_round",
    "rotation_day",
]
