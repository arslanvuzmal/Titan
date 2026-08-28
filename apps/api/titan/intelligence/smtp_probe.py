"""Mailbox verification Titan performs itself.

Until now the mailbox-level answer was bought. :mod:`titan.intelligence.mx` and
:mod:`titan.intelligence.verifier` both argued against building this, and the
argument was not wrong:

* most large providers accept every recipient at ``RCPT TO`` and bounce later,
  so the probe answers nothing; and
* a host that probes strangers' mail servers gets rate-limited, tarpitted or
  blocklisted, which damages the sending reputation the rest of this package
  exists to protect.

Both objections are about *where* you probe, not about probing. This module is
built around that distinction, so it is worth being explicit about how each one
is answered rather than leaving it to be discovered later.

**The first objection is answered by not asking.** A probe is only run against
domains where the answer means something. Google Workspace, Microsoft 365,
Yahoo, iCloud and the big filtering front-ends (Proofpoint, Mimecast,
Barracuda, MessageLabs) all either accept everything or refuse to talk to a
prober, and between them they front a large share of business mail. When the MX
records name one of those, this module returns ``UNKNOWN`` **without opening a
connection at all**. That is not a degraded answer; it is the honest one, and
it removes most of the traffic that would have caused the second problem.

**The second objection is answered by behaving like a mail server, once.**
Every probe: opens one connection, says who it is with a real hostname, gives a
real envelope sender at a mailbox that can receive complaints, asks about the
recipient, and then sends ``RSET`` and ``QUIT``. It never sends ``DATA``, so
nothing can ever be delivered by this path. Connections to one domain are
serialised and spaced, results are cached for the run, and a domain that
answers with a temporary failure is left alone rather than retried.

**Catch-all is detected before it can be mistaken for success.** A random local
part is asked about first. If the server accepts an address that cannot exist,
it accepts everything, and the real address is reported ``CATCH_ALL`` -- which
is not a sendable status on its own. Reporting such a domain as deliverable is
the specific failure that makes cheap verification services worse than useless.

**Everything uncertain is ``UNKNOWN``.** Greylisting, a timeout, a refused
connection, a 4xx of any kind, an unparseable reply: all ``UNKNOWN``. Only a
definite 5xx on a domain proven not to be catch-all yields ``INVALID``. The
asymmetry is deliberate and matches :mod:`titan.intelligence.mx`: a wrong
``INVALID`` silently discards a real customer, and nothing downstream would
ever show it happened.

What this still cannot do is tell you a Gmail address is real. Nothing can, for
any money. What it does is remove the addresses that hard-bounce -- dead
domains, retired mailboxes, typos in a scraped listing -- which is where the
reputation damage actually comes from.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import smtplib
import time
from dataclasses import dataclass, field

from titan.db.enums import VerificationStatus
from titan.intelligence.mx import (
    MxCheck,
    MxResolver,
    MxStatus,
    check_mx,
    system_mx_resolver,
)
from titan.intelligence.verifier import VerificationResult

logger = logging.getLogger(__name__)

#: MX hostnames whose operators either accept every recipient or treat a probe
#: as abuse. Matched as a domain suffix against each MX host.
#:
#: Being on this list is not a judgement about the *recipient*; it is a
#: statement that no useful answer is obtainable from that operator, so asking
#: costs something and buys nothing. The address falls through to the rest of
#: the eligibility engine -- provenance, syntax, disposable and lookalike
#: domains, MX presence -- exactly as it did before this module existed.
UNINFORMATIVE_MX_SUFFIXES: frozenset[str] = frozenset(
    {
        # Google: aspmx.l.google.com and friends. Accepts at RCPT, rejects on
        # delivery, and rate-limits probers aggressively.
        "google.com",
        "googlemail.com",
        # Microsoft 365: *.mail.protection.outlook.com. Accept-all by default
        # unless the tenant enables directory-based edge blocking.
        "outlook.com",
        "protection.outlook.com",
        "hotmail.com",
        # Yahoo / AOL: answers, but not truthfully, and tarpits repeat callers.
        "yahoodns.net",
        "yahoo.com",
        # Apple.
        "icloud.com",
        "apple.com",
        # Zoho: accept-all on most plans.
        "zoho.com",
        "zoho.eu",
        # Security front-ends. These sit in front of a mail system they will
        # not describe, and are the most likely of all to blocklist a prober.
        "pphosted.com",
        "ppe-hosted.com",
        "mimecast.com",
        "mimecast.co.za",
        "barracudanetworks.com",
        "messagelabs.com",
        "trendmicro.com",
        "sophos.com",
        "fortinet.net",
        # Large consumer providers outside the above.
        "qq.com",
        "163.com",
        "mail.ru",
        "yandex.net",
    }
)

#: Replies that mean the recipient is accepted.
ACCEPTED_CODES = frozenset({250, 251})

#: Replies that mean this mailbox does not exist and never will.
#:
#: 552 is deliberately absent: it is "mailbox full", which is a real mailbox
#: belonging to a real person having a bad week.
REJECTED_CODES = frozenset({550, 551, 553, 554})

#: Definite, but about capacity rather than existence.
FULL_CODES = frozenset({452, 552})

#: Fragments that mean the server refused *the caller*, not the recipient.
#:
#: This was found the hard way, on the first live run. Three addresses came
#: back INVALID and all three were real:
#:
#:     550 "JunkMail rejected - (arslanvuzmallone.com) [x.x.x.x] is in an
#:          RBL: Reverse DNS (PTR) missing - RFC1912 section 2.1"
#:     550 http://www.barracudanetworks.com/reputation/?pr=1&ip=x.x.x.x
#:
#: A server refusing the connection refuses every recipient on it -- the decoy
#: and the real address alike -- which is indistinguishable from a selective
#: server rejecting a mailbox that does not exist, unless the reply is read.
#: Three good addresses would have been suppressed for a property of *our* IP.
SENDER_REJECTION_MARKERS: tuple[str, ...] = (
    "rbl",
    "dnsbl",
    "blacklist",
    "blocklist",
    "black list",
    "block list",
    "blocked",
    "banned",
    "reputation",
    "spamhaus",
    "barracuda",
    "spamcop",
    "sorbs",
    "junkmail",
    "junk mail",
    "reverse dns",
    "rdns",
    "ptr",
    "client host",
    "helo",
    "ehlo",
    "not authorized",
    "not authorised",
    "access denied",
    "policy",
    "spf",
    "too many",
    "rate",
)

#: Fragments that mean the server refused *this recipient*, specifically.
#:
#: Required before a probe may say INVALID when both addresses were refused.
#: Without positive evidence that the refusal is about the mailbox, "both
#: refused" means only that the conversation produced nothing.
RECIPIENT_REJECTION_MARKERS: tuple[str, ...] = (
    "no such user",
    "no such recipient",
    "user unknown",
    "unknown user",
    "unknown recipient",
    "recipient unknown",
    "recipient not found",
    "recipient rejected",
    "does not exist",
    "doesn't exist",
    "no mailbox",
    "mailbox unavailable",
    "mailbox not found",
    "invalid recipient",
    "invalid mailbox",
    "unrouteable address",
    "unroutable address",
    "address rejected",
    "5.1.1",
    "5.1.10",
    "5.1.6",
)

#: How long a domain's catch-all verdict is reused within one process.
DOMAIN_CACHE_SECONDS = 3600.0

#: Minimum gap between two connections to the same domain. A verifier that
#: opens fifty connections to one small host in a second is indistinguishable
#: from an attack, whatever its intent.
MIN_DOMAIN_INTERVAL_SECONDS = 2.0

#: How many domains may be probed at once across the process.
DEFAULT_CONCURRENCY = 4

DEFAULT_TIMEOUT_SECONDS = 12.0


class ProbeConfigError(ValueError):
    """The probe is not configured to identify itself honestly."""


@dataclass(frozen=True, slots=True)
class ProbeReply:
    """One server's answer to one ``RCPT TO``."""

    code: int | None
    text: str = ""
    #: Set when the conversation failed before the server could answer.
    error: str | None = None

    @property
    def accepted(self) -> bool:
        return self.code in ACCEPTED_CODES

    @property
    def rejected(self) -> bool:
        return self.code in REJECTED_CODES


@dataclass
class _DomainVerdict:
    """What was learned about a domain, reused for its other addresses."""

    is_catch_all: bool
    at: float = field(default_factory=time.monotonic)
    detail: str = ""

    def is_fresh(self, *, ttl: float = DOMAIN_CACHE_SECONDS) -> bool:
        return (time.monotonic() - self.at) < ttl


def _mx_is_uninformative(check: MxCheck) -> str | None:
    """The operator's name if probing it answers nothing, else None."""
    for host in check.hosts:
        name = host.strip().lower().rstrip(".")
        for suffix in UNINFORMATIVE_MX_SUFFIXES:
            if name == suffix or name.endswith(f".{suffix}"):
                return suffix
    return None


def _mentions(text: str, markers: tuple[str, ...]) -> str | None:
    """The first marker present in a server's reply, or None."""
    lowered = (text or "").lower()
    for marker in markers:
        if marker in lowered:
            return marker
    return None


def is_sender_rejection(text: str) -> bool:
    """Whether a server's refusal is about *us* rather than the recipient.

    Public because delivery needs the same judgement and must not grow a second
    opinion about it. The probe learned this on its first live run -- three real
    addresses came back INVALID because our IP had no PTR record -- and the SMTP
    delivery adapter went on classifying every 5xx as a dead mailbox, so a
    ``554 ... too many messages from sender in last 60 minutes`` from Titan's
    own host suppressed the business it was aimed at.

    One list, one rule, both callers.
    """
    return _mentions(text, SENDER_REJECTION_MARKERS) is not None


def _is_permanent(code: int | None) -> bool:
    """Whether a reply settles the question for good.

    5xx is a refusal the server stands behind. 4xx is "not now" and says
    nothing about whether the mailbox exists.
    """
    return code is not None and 500 <= code < 600


def _random_local_part() -> str:
    """A local part that cannot belong to anybody.

    Prefixed so an administrator reading their logs can see what it was and who
    was asking, rather than finding an unexplained probe for a random string.
    """
    return f"titan-verify-probe-{secrets.token_hex(8)}"


class SmtpProbeVerifier:
    """Asks the recipient's own mail server, where that is worth doing."""

    name = "smtp_probe"

    def __init__(
        self,
        *,
        helo_hostname: str,
        mail_from: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        concurrency: int = DEFAULT_CONCURRENCY,
        min_domain_interval: float = MIN_DOMAIN_INTERVAL_SECONDS,
        resolver: MxResolver = system_mx_resolver,
    ) -> None:
        if not (helo_hostname or "").strip():
            raise ProbeConfigError(
                "a probe needs a real hostname to introduce itself with; an "
                "invented HELO name is the first thing a receiving server "
                "checks and the first reason it blocks the caller"
            )
        if "@" not in (mail_from or ""):
            raise ProbeConfigError(
                "a probe needs a real envelope sender at a mailbox that can "
                "receive a complaint. Probing with a null or fictional sender "
                "is the behaviour that gets a host blocklisted."
            )
        self._helo = helo_hostname.strip()
        self._mail_from = mail_from.strip()
        self._timeout = timeout_seconds
        self._resolver = resolver
        self._gate = asyncio.Semaphore(max(1, concurrency))
        self._min_interval = min_domain_interval
        self._domains: dict[str, _DomainVerdict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_touch: dict[str, float] = {}
        #: How many servers refused the probe itself. Surfaced by
        #: health_check, because a rising count is one fact about this
        #: host's IP rather than many facts about other people's mailboxes.
        self._rejections = 0

    # ------------------------------------------------------------- plumbing
    def _lock_for(self, domain: str) -> asyncio.Lock:
        lock = self._locks.get(domain)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[domain] = lock
        return lock

    async def _space_out(self, domain: str) -> None:
        last = self._last_touch.get(domain)
        if last is not None:
            wait = self._min_interval - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_touch[domain] = time.monotonic()

    def _ask(self, host: str, recipients: list[str]) -> list[ProbeReply]:
        """One connection, one conversation, no message.

        Synchronous because :mod:`smtplib` is; the caller runs it off the event
        loop. Both recipients are asked on the same connection so a domain sees
        one caller rather than two.
        """
        replies: list[ProbeReply] = []
        client: smtplib.SMTP | None = None
        try:
            client = smtplib.SMTP(
                host, 25, timeout=self._timeout, local_hostname=self._helo
            )
            client.ehlo_or_helo_if_needed()
            code, text = client.mail(self._mail_from)
            if code >= 400:
                detail = (
                    f"MAIL FROM refused: {code} {text.decode(errors='replace')[:120]}"
                )
                return [ProbeReply(None, error=detail) for _ in recipients]
            for address in recipients:
                code, text = client.rcpt(address)
                replies.append(ProbeReply(int(code), text.decode(errors="replace")[:200]))
            # RSET before QUIT: the transaction is abandoned explicitly rather
            # than dropped, which is what a well-behaved client does and what
            # keeps this out of a server's "suspicious" bucket.
            client.rset()
        except (TimeoutError, smtplib.SMTPException, OSError) as exc:
            detail = f"{type(exc).__name__}: {exc}"
            while len(replies) < len(recipients):
                replies.append(ProbeReply(None, error=detail))
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception as exc:
                    # The conversation is over either way; a server that hangs
                    # up on QUIT has still told us what we asked. Logged at
                    # debug rather than swallowed, so a host that always does
                    # this is findable if it ever matters.
                    logger.debug("quit failed for %s: %s", host, exc)
        return replies

    # --------------------------------------------------------------- verify
    async def verify(self, email: str) -> VerificationResult:
        address = (email or "").strip().lower()
        local, _, domain = address.partition("@")
        if not local or not domain or "." not in domain:
            return VerificationResult(
                status=VerificationStatus.INVALID,
                provider=self.name,
                detail="not a syntactically valid address",
            )

        check = await asyncio.to_thread(check_mx, domain, resolver=self._resolver)
        if check.is_conclusively_undeliverable:
            return VerificationResult(
                status=VerificationStatus.INVALID,
                provider=self.name,
                detail=check.detail or check.status.value,
                raw={"mx": check.status.value},
            )
        if check.status is MxStatus.ERROR:
            return VerificationResult(
                status=VerificationStatus.UNKNOWN,
                provider=self.name,
                detail=f"mx lookup failed: {check.detail}",
                raw={"mx": check.status.value},
            )

        operator = _mx_is_uninformative(check)
        if operator is not None:
            # No connection is opened. See the module docstring: this is the
            # larger half of what keeps the probe defensible.
            return VerificationResult(
                status=VerificationStatus.UNKNOWN,
                provider=self.name,
                detail=(
                    f"{domain} is fronted by {operator}, which accepts every "
                    f"recipient at RCPT; probing it would answer nothing"
                ),
                raw={"mx": check.status.value, "operator": operator},
            )

        if not check.hosts:
            # IMPLICIT_A: RFC 5321 says the address record is the destination.
            hosts = [domain]
        else:
            hosts = list(check.hosts)

        async with self._gate, self._lock_for(domain):
            return await self._probe(address, domain=domain, hosts=hosts, mx=check)

    async def _probe(
        self, address: str, *, domain: str, hosts: list[str], mx: MxCheck
    ) -> VerificationResult:
        cached = self._domains.get(domain)
        if cached is not None and cached.is_fresh() and cached.is_catch_all:
            # Nothing more can be learned here at any price, so no connection.
            return VerificationResult(
                status=VerificationStatus.CATCH_ALL,
                provider=self.name,
                is_catch_all=True,
                detail=cached.detail or "domain accepts every local part",
                raw={"mx": mx.status.value, "cached": True},
            )

        await self._space_out(domain)
        decoy = f"{_random_local_part()}@{domain}"
        replies = await asyncio.to_thread(self._ask, hosts[0], [decoy, address])
        decoy_reply, real_reply = replies[0], replies[1]

        # Before anything else: did the server refuse *us*? A connection-level
        # or policy-level refusal applies to every recipient on it, so nothing
        # that follows is about the mailbox. Read from the decoy's reply because
        # the decoy is the address we know nothing can be true of.
        sender_marker = _mentions(
            f"{decoy_reply.text} {real_reply.text}", SENDER_REJECTION_MARKERS
        )
        if sender_marker is not None and not decoy_reply.accepted:
            self._rejections += 1
            logger.warning(
                "the receiving server refused this probe rather than the "
                "recipient; nothing was learned about the address",
                extra={
                    "domain": domain,
                    "marker": sender_marker,
                    "reply": decoy_reply.text[:200],
                },
            )
            return VerificationResult(
                status=VerificationStatus.UNKNOWN,
                provider=self.name,
                detail=(
                    f"the server refused the probe itself ({sender_marker}), "
                    f"which says nothing about this mailbox"
                ),
                raw={
                    "mx": mx.status.value,
                    "sender_rejected": sender_marker,
                    "reply": decoy_reply.text[:200],
                },
            )

        if decoy_reply.accepted:
            self._domains[domain] = _DomainVerdict(
                is_catch_all=True,
                detail=f"accepted {decoy}, which cannot exist",
            )
            return VerificationResult(
                status=VerificationStatus.CATCH_ALL,
                provider=self.name,
                is_catch_all=True,
                detail="domain accepts every local part, so acceptance proves nothing",
                raw={"mx": mx.status.value, "decoy_code": decoy_reply.code},
            )

        # Only a *permanent* rejection of the decoy proves the domain is
        # selective. A 4xx proves nothing -- greylisting and rate limiting both
        # look like this -- and neither does a dropped connection. Treating
        # either as "selective" would let the next reply be read as a verified
        # mailbox on the strength of an answer nobody gave, which is the exact
        # false positive this whole module is arranged to avoid.
        if not _is_permanent(decoy_reply.code):
            return VerificationResult(
                status=VerificationStatus.UNKNOWN,
                provider=self.name,
                detail=(
                    "could not establish whether the domain accepts every "
                    f"local part: {decoy_reply.code or decoy_reply.error}"
                ),
                raw={"mx": mx.status.value, "decoy_code": decoy_reply.code},
            )

        self._domains[domain] = _DomainVerdict(
            is_catch_all=False, detail=f"rejected {decoy} with {decoy_reply.code}"
        )

        if real_reply.accepted:
            return VerificationResult(
                status=VerificationStatus.PROVIDER_VERIFIED,
                provider=self.name,
                detail=(
                    f"the server accepted this recipient and rejected an "
                    f"address that cannot exist ({decoy_reply.code})"
                ),
                raw={
                    "mx": mx.status.value,
                    "code": real_reply.code,
                    "decoy_code": decoy_reply.code,
                },
            )
        if real_reply.code in FULL_CODES:
            return VerificationResult(
                status=VerificationStatus.RISKY,
                provider=self.name,
                detail=f"mailbox exists but is not accepting: {real_reply.code}",
                raw={"mx": mx.status.value, "code": real_reply.code},
            )
        if real_reply.rejected:
            # Both were refused, which is the shape of a selective server
            # rejecting a dead mailbox *and* the shape of a server refusing the
            # caller. One connection cannot tell them apart on the codes alone,
            # so INVALID needs positive evidence that the refusal is about the
            # recipient. Without it, UNKNOWN -- because the cost of being wrong
            # here is a real customer silently discarded.
            recipient_marker = _mentions(real_reply.text, RECIPIENT_REJECTION_MARKERS)
            if recipient_marker is None:
                return VerificationResult(
                    status=VerificationStatus.UNKNOWN,
                    provider=self.name,
                    detail=(
                        f"refused with {real_reply.code}, but the reply does not "
                        f"say the mailbox is the reason"
                    ),
                    raw={
                        "mx": mx.status.value,
                        "code": real_reply.code,
                        "reply": real_reply.text,
                    },
                )
            return VerificationResult(
                status=VerificationStatus.INVALID,
                provider=self.name,
                detail=(
                    f"the server refused this recipient: {real_reply.code} "
                    f"({recipient_marker})"
                ),
                raw={
                    "mx": mx.status.value,
                    "code": real_reply.code,
                    "reply": real_reply.text,
                    "recipient_marker": recipient_marker,
                },
            )
        # Greylisting, an unexpected code, a dropped connection. Nothing was
        # settled, and UNKNOWN is what "nothing was settled" is called.
        return VerificationResult(
            status=VerificationStatus.UNKNOWN,
            provider=self.name,
            detail=(f"no conclusive answer: {real_reply.code or real_reply.error}"),
            raw={"mx": mx.status.value, "code": real_reply.code},
        )

    # --------------------------------------------------------------- health
    async def health_check(self) -> tuple[bool, str]:
        """Whether the probe is configured to identify itself honestly.

        Not a network call. There is no server to check against -- every probe
        goes to a different one -- and a synthetic probe against somebody's
        real mail server, to prove Titan is healthy, is exactly the traffic
        this module is careful not to generate.
        """
        line = (
            f"smtp probe as {self._helo} from {self._mail_from}; "
            f"{len(UNINFORMATIVE_MX_SUFFIXES)} operators skipped without contact"
        )
        if self._rejections:
            # Not a failure of the probe -- it is doing the right thing -- but
            # the operator needs to know the answers are thinning out, and why.
            line += (
                f"; {self._rejections} server(s) refused the probe itself, which "
                f"usually means this host's IP has no reverse DNS or is listed"
            )
        return True, line


__all__ = [
    "ACCEPTED_CODES",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_TIMEOUT_SECONDS",
    "DOMAIN_CACHE_SECONDS",
    "FULL_CODES",
    "MIN_DOMAIN_INTERVAL_SECONDS",
    "RECIPIENT_REJECTION_MARKERS",
    "REJECTED_CODES",
    "SENDER_REJECTION_MARKERS",
    "UNINFORMATIVE_MX_SUFFIXES",
    "ProbeConfigError",
    "ProbeReply",
    "SmtpProbeVerifier",
    "is_sender_rejection",
]
