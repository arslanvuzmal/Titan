"""The bounce reduction engine: what Titan knows about a recipient before it writes.

A hard bounce is not one wasted message. It is a receiver recording that this
sender does not know who it is mailing, and that record is what decides whether
the *next* hundred messages reach an inbox.
:mod:`titan.delivery.deliverability` already refuses to send once the bounce
rate climbs. This module exists so it does not have to: the cheapest bounce is
the one that was never queued.

**Layers, cheapest and most conclusive first.** Each catches something the
others cannot, and each is skipped when an earlier one has already settled the
question:

1. **Syntax** -- ``is_valid_email``. Free, local, conclusive.
2. **Disposable domain** -- free, local, conclusive, and invisible to DNS:
   throwaway providers run perfectly healthy mail servers.
3. **Lookalike domain** -- free, local, heuristic. The only layer that catches a
   *squatted* misspelling, which resolves, publishes MX and accepts the message.
4. **MX** -- one DNS lookup, conclusive in the negative direction only.
5. **Our own sending history** -- one indexed query, and the only evidence
   nobody can be wrong about: a bounce is the receiver saying so itself.
6. **Mailbox verification** -- a purchased round trip, the only layer that can
   say a specific mailbox exists, and the only one that can detect catch-all.

**Negative evidence outranks positive.** The layers are not weighted or scored.
A single conclusive refusal produces INVALID whatever else was found, because
the layers answer different questions and a positive answer to one is not a
rebuttal of a negative answer to another: a verification service confirming that
``info@gmial.com`` accepts mail does not make it less of a misspelling. Scoring
these against each other would let two weak positives outvote one conclusive
negative, which is exactly the arithmetic that puts a bounce in the queue.

**"Invalid" here means "must never be sent to", not "malformed".** A disposable
address is usually deliverable in the narrow sense; nobody reads it, it expires,
and it is far more often a defect in our own crawl than a real contact. The
engine reports what should happen, not what a mail server would do.

The engine is pure. Every input is gathered by the caller -- DNS in a thread,
verification over the network -- so this module does no I/O and the whole
decision table is testable without either.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from titan.db.enums import ContactSource, VerificationStatus, verification_permits_sending
from titan.intelligence.contacts import email_domain, is_valid_email, normalize_email
from titan.intelligence.domain_health import DomainHealth, DomainWindow, classify, explain
from titan.intelligence.mx import MxCheck
from titan.intelligence.recipient_domains import (
    is_disposable,
    is_free_mailbox_provider,
    typo_of,
)
from titan.intelligence.verifier import VerificationResult


class Verdict(StrEnum):
    """What one signal argues for."""

    #: Conclusive. This address must not be sent to, whatever else was found.
    REFUSE = "refuse"
    #: A real predictor of a bounce, short of conclusive.
    DOWNGRADE = "downgrade"
    #: The domain accepts every local part, so acceptance proves nothing.
    CATCH_ALL = "catch_all"
    #: Positive evidence that this specific mailbox exists.
    CONFIRM = "confirm"
    #: Context. Recorded because it changes what other signals mean; on its own
    #: it changes nothing.
    NOTE = "note"


@dataclass(frozen=True, slots=True)
class RiskSignal:
    code: str
    verdict: Verdict
    detail: str


@dataclass(frozen=True, slots=True)
class BounceRisk:
    """Everything the engine concluded, and why."""

    status: VerificationStatus
    source: ContactSource
    email: str
    signals: tuple[RiskSignal, ...] = ()

    @property
    def refusals(self) -> tuple[RiskSignal, ...]:
        return tuple(s for s in self.signals if s.verdict is Verdict.REFUSE)

    @property
    def permits_sending(self) -> bool:
        """Whether status and provenance together allow a send.

        Delegates rather than deciding, so this and the policy engine cannot
        drift apart: there is one rule, in ``titan.db.enums``, and both read it.
        """
        return verification_permits_sending(self.status, self.source)

    @property
    def reasons(self) -> tuple[str, ...]:
        """Human-readable refusal text, for the operator UI and lead rejection logs."""
        return tuple(
            s.detail
            for s in self.signals
            if s.verdict in (Verdict.REFUSE, Verdict.DOWNGRADE)
        )

    def as_verification_detail(self) -> dict[str, object]:
        """Shaped for ContactVerification.detail (append-only, diagnostic)."""
        return {
            "check": "bounce_risk",
            "status": self.status.value,
            "source": self.source.value,
            "permits_sending": self.permits_sending,
            "signals": [
                {"code": s.code, "verdict": s.verdict.value, "detail": s.detail}
                for s in self.signals
            ],
        }


def assess(
    *,
    email: str,
    source: ContactSource,
    mx: MxCheck | None = None,
    verification: VerificationResult | None = None,
    history: DomainWindow | None = None,
) -> BounceRisk:
    """Classify a recipient as deliverable, catch-all, risky, unknown or invalid.

    Every optional argument means "not checked" when absent, never "checked and
    found wanting". A deployment with no verification service still gets the
    local layers; one with a broken resolver still gets the rest; a domain
    nobody has written to yet still gets everything except history. Degrading to
    a weaker answer is correct here, because every status reachable without them
    is either non-sendable or sendable on provenance established elsewhere.
    """
    normalized = normalize_email(email)
    signals: list[RiskSignal] = []

    # ---- layer 1: syntax ------------------------------------------------
    if not is_valid_email(normalized):
        # Nothing below can mean anything about a string that is not an
        # address, and passing one to a resolver or a paid API is waste.
        return BounceRisk(
            status=VerificationStatus.INVALID,
            source=source,
            email=normalized,
            signals=(
                RiskSignal(
                    "malformed_address",
                    Verdict.REFUSE,
                    "not a syntactically valid email address",
                ),
            ),
        )

    domain = email_domain(normalized)

    # ---- layer 2: disposable --------------------------------------------
    if is_disposable(domain):
        signals.append(
            RiskSignal(
                "disposable_domain",
                Verdict.REFUSE,
                f"{domain} is a disposable mailbox provider; the address is read "
                "by nobody and is more likely a defect in our own crawl than a "
                "real contact",
            )
        )

    # ---- layer 3: lookalike ---------------------------------------------
    # DOWNGRADE rather than REFUSE: this is edit distance, not evidence. A real
    # domain can sit one character from a webmail provider, and the cost of
    # being wrong is a discarded lead, so the verdict leaves room for a
    # verification service to overrule it.
    impersonated = typo_of(domain)
    if impersonated is not None:
        signals.append(
            RiskSignal(
                "lookalike_domain",
                Verdict.DOWNGRADE,
                f"{domain} is one character from {impersonated} and is likely a "
                "misspelling; if the domain is registered, mail goes to whoever "
                "registered it",
            )
        )

    if is_free_mailbox_provider(domain):
        signals.append(
            RiskSignal(
                "free_mailbox_provider",
                Verdict.NOTE,
                f"{domain} is a consumer mailbox provider, so the address is "
                "personal rather than a company one",
            )
        )

    # ---- layer 4: MX -----------------------------------------------------
    # One direction only, matching mx.py: a conclusive negative disqualifies,
    # a positive proves the domain accepts mail and nothing about this mailbox,
    # and a failed lookup is our problem rather than evidence about the domain.
    if mx is not None and mx.is_conclusively_undeliverable:
        signals.append(
            RiskSignal(
                "domain_cannot_receive_mail",
                Verdict.REFUSE,
                f"{mx.domain} publishes no route for inbound mail "
                f"({mx.status.value}); every address at it hard-bounces",
            )
        )

    # ---- layer 5: our own sending history --------------------------------
    # The only layer that can be wrong about nothing: a bounce is the receiver
    # telling us in its own words that we were mistaken.
    if history is not None:
        health_signal = _history_signal(history)
        if health_signal is not None:
            signals.append(health_signal)

    # ---- layer 6: mailbox verification -----------------------------------
    if verification is not None and verification.is_conclusive:
        signals.append(_verification_signal(verification))

    return BounceRisk(
        status=_resolve(signals, source=source),
        source=source,
        email=normalized,
        signals=tuple(signals),
    )


def _history_signal(history: DomainWindow) -> RiskSignal | None:
    """What Titan's own delivery record at this domain argues for.

    BLOCKED refuses. Everything softer downgrades or merely notes, because a
    per-domain window at Titan's volumes holds two or three messages -- enough
    to be suspicious of, rarely enough to be certain about. The one exception is
    a complaint, which ``classify`` already promotes to BLOCKED on its own: a
    person marked us as spam, and no sample size makes that ambiguous.
    """
    health = classify(history)
    detail = explain(history, health)

    if health is DomainHealth.BLOCKED:
        return RiskSignal("recipient_domain_blocked", Verdict.REFUSE, detail)
    if health is DomainHealth.DEGRADED:
        return RiskSignal("recipient_domain_degraded", Verdict.DOWNGRADE, detail)
    if health is DomainHealth.WATCH:
        return RiskSignal("recipient_domain_watch", Verdict.NOTE, detail)
    if health is DomainHealth.HEALTHY:
        # Deliberately a NOTE, not a CONFIRM. Titan having delivered to this
        # domain before says the domain accepts mail; it says nothing about
        # whether *this* mailbox exists, which is the same trap MX presence
        # sets and the same answer.
        return RiskSignal("recipient_domain_healthy", Verdict.NOTE, detail)
    return None


def _verification_signal(result: VerificationResult) -> RiskSignal:
    provider = result.provider
    if result.status is VerificationStatus.INVALID:
        return RiskSignal(
            "mailbox_does_not_exist",
            Verdict.REFUSE,
            f"{provider} reports the mailbox does not exist"
            + (f": {result.detail}" if result.detail else ""),
        )
    if result.status is VerificationStatus.CATCH_ALL or result.is_catch_all:
        return RiskSignal(
            "catch_all_domain",
            Verdict.CATCH_ALL,
            f"{provider} reports the domain accepts every local part, so "
            "acceptance of this address proves nothing about it",
        )
    if result.status is VerificationStatus.RISKY:
        return RiskSignal(
            "mailbox_risky",
            Verdict.DOWNGRADE,
            f"{provider} reports the mailbox as risky"
            + (f": {result.detail}" if result.detail else ""),
        )
    return RiskSignal(
        "mailbox_confirmed",
        Verdict.CONFIRM,
        f"{provider} confirmed the mailbox accepts mail",
    )


def _resolve(signals: list[RiskSignal], *, source: ContactSource) -> VerificationStatus:
    """Reduce the signals to one status.

    Strictly ordered, not scored. Each branch is checked against everything
    found, so a conclusive refusal cannot be outvoted by any number of weaker
    positives -- see the module docstring on why weighting these would be wrong.

    Reads only the signals, never the raw inputs: a verification result that
    reached a conclusion has already become a signal by the time this runs, and
    consulting both would create two places where the same answer is
    interpreted.
    """
    verdicts = {s.verdict for s in signals}

    if Verdict.REFUSE in verdicts:
        return VerificationStatus.INVALID
    if Verdict.DOWNGRADE in verdicts:
        return VerificationStatus.RISKY
    if Verdict.CATCH_ALL in verdicts:
        return VerificationStatus.CATCH_ALL
    if Verdict.CONFIRM in verdicts:
        return VerificationStatus.PROVIDER_VERIFIED

    # Nothing conclusive either way. Publication on the business's own site is
    # then the strongest thing known about the address, and it is exactly what
    # the pipeline recorded unconditionally before this engine existed.
    #
    # Only that one source reaches a sendable status here, and the narrowness is
    # deliberate rather than an oversight about the others. The status is called
    # PUBLISHED_FIRST_PARTY because that is the claim it makes: somebody put this
    # address on their own contact page. A manually entered address is trusted
    # provenance -- ``_CATCH_ALL_TRUSTED_SOURCES`` accepts it -- but it was not
    # published, so it cannot carry this status, and with no verification behind
    # it there is nothing else for it to carry. It stays UNKNOWN and does not
    # send, which is what it did before this engine existed too: nothing wrote a
    # status for those rows and the column default is UNVERIFIED.
    #
    # The two rules sit at different evidence levels. This one asks what
    # provenance establishes *on its own*; the catch-all rule asks whose
    # assertion is good enough to stand behind a verifier's answer that the
    # domain accepts everything.
    if source is ContactSource.FIRST_PARTY_WEBSITE:
        return VerificationStatus.PUBLISHED_FIRST_PARTY
    return VerificationStatus.UNKNOWN


__all__ = [
    "BounceRisk",
    "RiskSignal",
    "Verdict",
    "assess",
]
