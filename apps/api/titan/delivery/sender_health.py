"""Sender health, as a thing with a history rather than a thing recomputed.

Every input here already existed. :mod:`titan.intelligence.sender_auth` resolves
SPF, DKIM and DMARC and expires the claim after fourteen days;
:mod:`titan.delivery.deliverability` measures bounces and complaints over a
trailing window and refuses to send past a threshold. What neither did was
*remember*. Both ran inside the outbox worker, decided one message, and threw the
numbers away.

That is enough to stop a send and not enough to answer the question an operator
actually asks, which is never "is this mailbox healthy right now" but "is it
getting worse". A complaint rate of 0.04% is fine. A complaint rate of 0.04%
that was 0.01% last Tuesday is a mailbox about to be shut off, and the two are
indistinguishable from a single measurement.

**Thresholds are not redefined here.** ``classify`` calls
``deliverability.check_reputation`` and reads its signals. Restating "pause at
0.1%" in a second module is how two parts of one system come to disagree about
what a healthy sender is, and the one that matters is always the other one.

**Warm-up is a status, not a fault.** A mailbox in its first fortnight is
supposed to be sending very little. Reporting that as degraded would train
whoever reads these to ignore the word.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum

from titan.delivery import deliverability
from titan.delivery.deliverability import ReputationWindow, Severity

#: Share of attempts that had to be retried before the provider accepted them,
#: above which the sender is being pushed back on. Retries are ordinary in ones
#: and twos; a fifth of a day's mail needing a second attempt is a mail server
#: telling us to slow down.
RETRY_PRESSURE_RATIO = 0.2

#: Below this many attempts the ratio above is noise.
MIN_ATTEMPTS_FOR_PRESSURE = 10


class SenderHealth(StrEnum):
    #: Never sent. Not a verdict.
    UNKNOWN = "unknown"
    #: Inside the warm-up ramp and behaving. Low volume is correct here.
    WARMING = "warming"
    HEALTHY = "healthy"
    #: Being throttled, or something worth seeing. Sending continues.
    WATCH = "watch"
    #: A published threshold is close. Sending continues; a human should look.
    DEGRADED = "degraded"
    #: Authentication is broken or a pause threshold is crossed. Not sendable.
    BLOCKED = "blocked"


#: Worst to best. Comparing two snapshots is comparing these positions.
_SEVERITY_ORDER: tuple[SenderHealth, ...] = (
    SenderHealth.BLOCKED,
    SenderHealth.DEGRADED,
    SenderHealth.WATCH,
    SenderHealth.WARMING,
    SenderHealth.HEALTHY,
    SenderHealth.UNKNOWN,
)


def severity_rank(health: SenderHealth) -> int:
    """Lower is worse. UNKNOWN sits outside the scale and ranks best."""
    return _SEVERITY_ORDER.index(health)


class Trend(StrEnum):
    IMPROVING = "improving"
    STABLE = "stable"
    DEGRADING = "degrading"
    #: Fewer than two snapshots. A trend needs two points.
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True, slots=True)
class SenderSnapshot:
    """One day's measurement of one sending identity."""

    sender_identity_id: str
    sending_domain: str
    captured_on: dt.date

    # ---- authentication -------------------------------------------------
    domain_verified: bool = False
    spf_ok: bool = False
    dkim_ok: bool = False
    dmarc_ok: bool = False
    #: The four flags above are older than sender_auth.MAX_VERIFICATION_AGE.
    auth_stale: bool = False

    # ---- reputation, trailing window ------------------------------------
    window: ReputationWindow = field(default_factory=lambda: ReputationWindow(0, 0, 0, 0))

    # ---- throttling -----------------------------------------------------
    #: Delivery attempts in the window, including retries of the same message.
    attempts: int = 0
    #: Attempts that were not the first for their message.
    retries: int = 0
    #: Messages currently held back rather than refused.
    deferred: int = 0

    # ---- activity -------------------------------------------------------
    sent_today: int = 0
    #: Index into deliverability.WARMUP_SCHEDULE, or None once warm-up is done.
    warmup_day: int | None = None
    warmup_limit: int | None = None

    @property
    def authenticated(self) -> bool:
        """Whether the identity's authentication is both complete and current.

        Staleness is part of the test, not a footnote to it. A boolean set two
        months ago is an assertion; ``SenderIdentity.authorization_errors``
        already refuses to send on one, and this agrees with it.
        """
        return (
            self.domain_verified
            and self.spf_ok
            and self.dkim_ok
            and self.dmarc_ok
            and not self.auth_stale
        )

    @property
    def is_warming(self) -> bool:
        return self.warmup_day is not None

    @property
    def retry_ratio(self) -> float:
        return self.retries / self.attempts if self.attempts else 0.0

    @property
    def under_throttling_pressure(self) -> bool:
        return (
            self.attempts >= MIN_ATTEMPTS_FOR_PRESSURE
            and self.retry_ratio >= RETRY_PRESSURE_RATIO
        )


def classify(snapshot: SenderSnapshot) -> SenderHealth:
    """Reduce a snapshot to one verdict, worst condition first."""
    if not snapshot.authenticated:
        return SenderHealth.BLOCKED

    signals = deliverability.check_reputation(snapshot.window)
    if any(s.severity is Severity.BLOCK for s in signals):
        return SenderHealth.BLOCKED
    if any(s.severity is Severity.WARN for s in signals):
        return SenderHealth.DEGRADED

    if snapshot.under_throttling_pressure:
        return SenderHealth.WATCH

    if snapshot.is_warming:
        return SenderHealth.WARMING

    if snapshot.window.sent == 0:
        return SenderHealth.UNKNOWN
    return SenderHealth.HEALTHY


def reasons(snapshot: SenderSnapshot) -> tuple[str, ...]:
    """Why the verdict is what it is, in an operator's words."""
    out: list[str] = []

    if not snapshot.domain_verified:
        out.append(f"{snapshot.sending_domain} is not verified")
    else:
        missing = [
            name
            for flag, name in (
                (snapshot.spf_ok, "SPF"),
                (snapshot.dkim_ok, "DKIM"),
                (snapshot.dmarc_ok, "DMARC"),
            )
            if not flag
        ]
        if missing:
            out.append(f"{', '.join(missing)} not passing on {snapshot.sending_domain}")
        elif snapshot.auth_stale:
            out.append(
                f"{snapshot.sending_domain} authentication has not been "
                "re-checked recently; the flags are an assertion, not evidence"
            )

    out.extend(s.detail for s in deliverability.check_reputation(snapshot.window))

    if snapshot.under_throttling_pressure:
        out.append(
            f"{snapshot.retry_ratio:.0%} of delivery attempts needed a retry "
            f"({snapshot.retries} of {snapshot.attempts}); the receiving side is "
            "pushing back"
        )
    if snapshot.deferred:
        out.append(f"{snapshot.deferred} messages are deferred")
    if snapshot.is_warming and snapshot.warmup_limit is not None:
        out.append(
            f"warm-up day {(snapshot.warmup_day or 0) + 1}: "
            f"{snapshot.sent_today} of {snapshot.warmup_limit} sent today"
        )
    return tuple(out)


def trend(recent: list[SenderHealth]) -> Trend:
    """Direction of travel, newest first.

    Deliberately compares only the newest verdict against the one before it.
    A longer regression would be more informative with fifty points and is
    misleading with three, which is what a mailbox that sends on weekdays
    actually has after a week.

    UNKNOWN on either side yields INSUFFICIENT rather than a direction. It ranks
    best on the severity scale because it is not a fault, but a sender that went
    quiet has not *improved* -- there is simply nothing to compare, and reporting
    a silent mailbox as recovering would be the most misleading answer available.
    """
    if len(recent) < 2:
        return Trend.INSUFFICIENT
    if SenderHealth.UNKNOWN in (recent[0], recent[1]):
        return Trend.INSUFFICIENT
    now, before = severity_rank(recent[0]), severity_rank(recent[1])
    if now < before:
        return Trend.DEGRADING
    if now > before:
        return Trend.IMPROVING
    return Trend.STABLE


#: Verdicts worth waking an operator for.
ALERTING_HEALTH = frozenset({SenderHealth.BLOCKED, SenderHealth.DEGRADED})


def should_alert(current: SenderHealth, previous: SenderHealth | None) -> bool:
    """Whether this transition deserves a notification.

    Alerts fire on the *edge*, not on the state. A mailbox that has been
    degraded for a fortnight is a known problem, and re-raising it every day
    trains the reader to skip the message that matters. ``previous`` being None
    -- the first snapshot ever -- still alerts, because arriving already
    degraded is news.
    """
    if current not in ALERTING_HEALTH:
        return False
    if previous is None:
        return True
    return severity_rank(current) < severity_rank(previous)


__all__ = [
    "ALERTING_HEALTH",
    "MIN_ATTEMPTS_FOR_PRESSURE",
    "RETRY_PRESSURE_RATIO",
    "SenderHealth",
    "SenderSnapshot",
    "Trend",
    "classify",
    "reasons",
    "severity_rank",
    "should_alert",
    "trend",
]
