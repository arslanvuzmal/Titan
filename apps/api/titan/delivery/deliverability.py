"""Inbox-placement controls.

A caveat worth stating plainly, because the opposite is widely implied: **no
configuration guarantees the inbox.** Placement is a receiver's judgement about
your domain's reputation, and reputation is earned by behaviour over time. What
software can do is remove every known *reason* to be filtered and refuse to send
when the signals that predict filtering go bad.

This module covers the four things that actually decide it:

1. **Headers receivers now require.** Gmail and Yahoo's bulk-sender rules make
   RFC 8058 one-click unsubscribe mandatory. Missing it is not a soft signal;
   it is a documented rejection reason.
2. **Message construction.** Image-only mail, no plain-text alternative, heavy
   link density, shouting subject lines -- each is an independent filter signal.
3. **Reputation thresholds.** Gmail publishes a 0.3% complaint ceiling. Titan
   stops well before that, because by the time you reach it you are already
   being filtered.
4. **Warm-up.** A brand-new mailbox sending at full volume looks exactly like
   a compromised one. Volume ramps towards the mailbox's own configured limit.

Everything here is enforced in the outbox worker, not offered as advice.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# --------------------------------------------------------------------------
# Reputation thresholds
# --------------------------------------------------------------------------
#: Gmail's published limit is 0.3%. Titan pauses at a third of that: a rate you
#: can measure is already a rate that is hurting you, and recovery from a
#: reputation hit takes weeks.
COMPLAINT_RATE_PAUSE = 0.001  # 0.1%
COMPLAINT_RATE_WARN = 0.0005  # 0.05%

#: Hard bounces above this indicate a list-quality problem. Receivers treat a
#: high bounce rate as evidence the sender does not know who they are mailing.
BOUNCE_RATE_PAUSE = 0.02  # 2%
BOUNCE_RATE_WARN = 0.01  # 1%

#: Below this, rates are noise. Two complaints out of ten sends is 20% but
#: means nothing; pausing on it would make the system unusable.
MIN_SAMPLE_FOR_RATES = 50

#: How long without a hard bounce before a bad bounce *rate* is treated as a
#: fact about the past rather than a reason to keep refusing.
#:
#: The rate is bounces over sends, so the only way down is more clean sends --
#: and a blocked mailbox sends nothing. ``outreach@`` took five hard bounces
#: over 94 sends (5.32%, against a 2% ceiling), every one of them before address
#: verification existed. It could not earn its way out; it could only wait for
#: the window to roll past the bad days, and meanwhile 50 messages sat queued
#: behind a number describing a fortnight ago.
#:
#: This deliberately matches ``adaptive_limits.PROBATION_QUIET_DAYS``. Those two
#: gates sit in series on the same send: the limit granting five a day is worth
#: nothing while this one still returns BLOCK, which is exactly what happened --
#: the probation allowance was live and the queue still did not move.
BOUNCE_QUIET_DAYS = 7

#: Mailbox warm-up. Day index -> the *fraction* of that mailbox's configured
#: daily limit it may send that day.
#:
#: Fractions rather than absolute counts, and the distinction is not cosmetic.
#: The schedule this replaced ran 20, 30, 40, 60, 80 ... up to 1000, which was
#: written for a sending domain expected to reach four figures a day. Applied to
#: a mailbox configured for 50 -- which is what a real cold-outreach mailbox is
#: configured for -- every rung above 50 was clamped away by the configured
#: limit, so the mailbox reached full volume on day four and warm-up stopped
#: constraining anything. A ramp that finishes before it has ramped is worse
#: than none, because it looks like protection.
#:
#: Geometric, not linear: deliverability reputation responds to *relative*
#: growth, so a mailbox climbing 13% a day looks the same to a receiver whether
#: it is heading for 50 a day or 500. Twenty days from a tenth of target to
#: target is the conservative end of common practice, and the cost of being slow
#: here is a delay, while the cost of being fast is the domain.
WARMUP_RAMP: tuple[float, ...] = (
    0.100,
    0.113,
    0.127,
    0.144,
    0.162,
    0.183,
    0.207,  # week 1
    0.234,
    0.264,
    0.298,
    0.336,
    0.379,
    0.428,
    0.483,  # week 2
    0.546,
    0.616,
    0.695,
    0.785,
    0.886,
    1.000,  # week 3
)
WARMUP_DAYS = len(WARMUP_RAMP)

#: A warming mailbox sends at least one message a day or it never establishes
#: any history at all. Only relevant for small targets: a tenth of 50 is five,
#: but a tenth of 6 rounds to nothing.
MIN_WARMUP_VOLUME = 1

#: The most a mailbox's daily allowance may exceed the largest volume it has
#: actually sent on a recent day.
#:
#: The ramp above positions a mailbox by *age*; this bounds it by *evidence*.
#: The two come apart whenever a mailbox's age is credited from somewhere other
#: than its own send history -- a provider's warm-up start date, an import, a
#: mailbox that sat idle mid-ramp -- and the result is a day-13 allowance handed
#: to a mailbox that has never sent more than six. Arriving at a volume is safe;
#: jumping to it is one of the patterns receivers watch for, which is the same
#: reason ``adaptive_limits`` recovers over three days rather than at once.
#:
#: Doubling reaches any ramp position within a few days, so this costs days and
#: never the destination.
MAX_DAILY_STEP_UP = 2.0


class Severity(StrEnum):
    BLOCK = "block"
    WARN = "warn"


@dataclass(frozen=True, slots=True)
class Signal:
    code: str
    severity: Severity
    detail: str
    remedy: str | None = None


@dataclass(frozen=True, slots=True)
class DeliverabilityReport:
    signals: tuple[Signal, ...] = ()

    @property
    def blocking(self) -> tuple[Signal, ...]:
        return tuple(s for s in self.signals if s.severity is Severity.BLOCK)

    @property
    def warnings(self) -> tuple[Signal, ...]:
        return tuple(s for s in self.signals if s.severity is Severity.WARN)

    @property
    def ok(self) -> bool:
        return not self.blocking

    def to_json(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "signals": [
                {
                    "code": s.code,
                    "severity": s.severity.value,
                    "detail": s.detail,
                    "remedy": s.remedy,
                }
                for s in self.signals
            ],
        }


# ==========================================================================
# Required headers
# ==========================================================================
def build_headers(
    *,
    message_id_domain: str,
    unsubscribe_url: str | None,
    unsubscribe_mailto: str | None,
    campaign_id: str,
    now: dt.datetime | None = None,
) -> dict[str, str]:
    """Headers every Titan message carries.

    ``List-Unsubscribe-Post`` is the one that matters most and is most often
    omitted: without it, Gmail shows no one-click unsubscribe button, and a
    recipient who wants out clicks "report spam" instead. That single
    substitution is the fastest way to destroy a sending domain.
    """
    moment = now or dt.datetime.now(dt.UTC)
    headers: dict[str, str] = {
        # A stable, domain-scoped Message-ID. Providers generate one if absent,
        # but a consistent domain helps threading and reputation attribution.
        "Message-ID": f"<{uuid.uuid4().hex}@{message_id_domain}>",
        "Date": moment.strftime("%a, %d %b %Y %H:%M:%S +0000"),
        # Marks this as solicited bulk mail rather than a personal message.
        # Honest classification; receivers detect the difference anyway.
        "Precedence": "bulk",
        "Auto-Submitted": "auto-generated",
        "X-Titan-Campaign": campaign_id,
    }

    targets = [t for t in (unsubscribe_url, unsubscribe_mailto) if t]
    if targets:
        headers["List-Unsubscribe"] = ", ".join(f"<{t}>" for t in targets)
    if unsubscribe_url:
        # RFC 8058. Only valid alongside an https List-Unsubscribe target.
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    return headers


#: What may be attached to a cold approach.
#:
#: An unsolicited attachment from an unknown sender is one of the strongest
#: spam signals there is, and corporate gateways quarantine or strip several
#: document types by policy. That is an argument for attaching carefully, not
#: for pretending the feature does not exist -- so these are the bounds.
MAX_ATTACHMENTS = 1
MAX_ATTACHMENT_BYTES = 400 * 1024
ALLOWED_ATTACHMENT_TYPES: frozenset[tuple[str, str]] = frozenset(
    {("application", "pdf")}
)

#: File signatures that are executable or archive content whatever the name
#: says. Checked on the bytes because the filename is the one part of an
#: attachment an attacker controls for free.
_EXECUTABLE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "a Windows executable"),
    (b"\x7fELF", "a Linux executable"),
    (b"PK\x03\x04", "a zip archive"),
    (b"\xca\xfe\xba\xbe", "a Mach-O or Java class file"),
    (b"#!", "a script"),
)

#: What a PDF actually starts with. A file claiming to be one and not starting
#: with this is mislabelled, and mailing it would be mailing something nobody
#: has identified.
_PDF_MAGIC = b"%PDF-"


def check_attachments(attachments: Sequence[Any]) -> list[Signal]:
    """Bound what may leave with a cold message.

    Every signal here is BLOCK. An attachment that trips one of these does not
    make the message slightly worse -- it makes it the kind of message that
    gets a sending domain filtered, or in the executable case, the kind that
    should never have been assembled at all.
    """
    signals: list[Signal] = []
    if not attachments:
        return signals

    if len(attachments) > MAX_ATTACHMENTS:
        signals.append(
            Signal(
                "too_many_attachments",
                Severity.BLOCK,
                f"{len(attachments)} attachments; at most {MAX_ATTACHMENTS}",
                "A cold approach carrying several documents reads as a mailing "
                "rather than a message, and is filtered like one.",
            )
        )

    for attachment in attachments:
        name = getattr(attachment, "filename", "") or "(unnamed)"
        content = getattr(attachment, "content", b"") or b""
        kind = (
            getattr(attachment, "maintype", ""),
            getattr(attachment, "subtype", ""),
        )

        if kind not in ALLOWED_ATTACHMENT_TYPES:
            signals.append(
                Signal(
                    "attachment_type_not_allowed",
                    Severity.BLOCK,
                    f"{name} is {kind[0]}/{kind[1]}",
                    "Only PDF is delivered intact by corporate mail gateways; "
                    "office formats and archives are quarantined by policy.",
                )
            )

        if len(content) > MAX_ATTACHMENT_BYTES:
            signals.append(
                Signal(
                    "attachment_too_large",
                    Severity.BLOCK,
                    f"{name} is {len(content) // 1024} KB, over "
                    f"{MAX_ATTACHMENT_BYTES // 1024} KB",
                    "Large attachments are scored by receivers, and a one-page "
                    "brief that does not fit is not a one-page brief.",
                )
            )

        if not content:
            signals.append(
                Signal(
                    "attachment_empty",
                    Severity.BLOCK,
                    f"{name} is empty",
                    "An empty attachment tells the reader a document was meant "
                    "to be here and is not.",
                )
            )
            continue

        for magic, description in _EXECUTABLE_MAGIC:
            if content.startswith(magic):
                signals.append(
                    Signal(
                        "attachment_is_executable",
                        Severity.BLOCK,
                        f"{name} is {description}, whatever it is named",
                        "Checked on the bytes, not the extension. Nothing in "
                        "this system should ever assemble one.",
                    )
                )
                break
        else:
            if kind == ("application", "pdf") and not content.startswith(_PDF_MAGIC):
                signals.append(
                    Signal(
                        "attachment_not_a_pdf",
                        Severity.BLOCK,
                        f"{name} is declared PDF but does not begin %PDF-",
                        "The file is mislabelled; mailing it would be mailing "
                        "something nobody has identified.",
                    )
                )

    return signals


def check_required_headers(headers: dict[str, str]) -> list[Signal]:
    signals: list[Signal] = []
    lowered = {k.lower(): v for k, v in headers.items()}

    if "list-unsubscribe" not in lowered:
        signals.append(
            Signal(
                "missing_list_unsubscribe",
                Severity.BLOCK,
                "no List-Unsubscribe header",
                "Gmail and Yahoo require it from bulk senders; without it "
                "recipients use 'report spam' to opt out.",
            )
        )
    elif "list-unsubscribe-post" not in lowered:
        signals.append(
            Signal(
                "missing_one_click_unsubscribe",
                Severity.BLOCK,
                "List-Unsubscribe present but not one-click (RFC 8058)",
                "Add 'List-Unsubscribe-Post: List-Unsubscribe=One-Click' and an "
                "https unsubscribe endpoint that accepts POST.",
            )
        )
    elif "https://" not in lowered.get("list-unsubscribe", ""):
        signals.append(
            Signal(
                "unsubscribe_not_https",
                Severity.WARN,
                "one-click unsubscribe declared without an https target",
                "One-click requires an https URL; a mailto: alone will not "
                "render the button.",
            )
        )
    return signals


# ==========================================================================
# Message construction
# ==========================================================================
#: Phrases that materially raise spam scores in cold outreach. Deliberately
#: short: a long list produces false positives on ordinary business English,
#: and the validator already rejects the manipulative phrasing separately.
_SPAM_PHRASES = (
    r"\b(?:100% free|risk[- ]free|no obligation|act now|limited time)\b",
    r"\b(?:click here|buy now|order now|sign up free)\b",
    r"\b(?:guarantee[ds]?|guaranteed results|make money|extra cash)\b",
    r"\b(?:congratulations|winner|you have been selected|dear friend)\b",
    r"\$\$\$|!!!+|\?\?\?+",
)


def check_message(
    *,
    subject: str,
    text_body: str,
    html_body: str | None,
    from_name: str,
    mailing_address: str | None,
) -> list[Signal]:
    """Construction checks that predict filtering."""
    signals: list[Signal] = []

    # ---- subject ---------------------------------------------------------
    if not subject.strip():
        signals.append(Signal("empty_subject", Severity.BLOCK, "subject is empty"))
    if len(subject) > 78:
        signals.append(
            Signal(
                "long_subject",
                Severity.WARN,
                f"subject is {len(subject)} characters",
                "Mobile clients truncate around 40; under 60 is safer.",
            )
        )
    letters = [c for c in subject if c.isalpha()]
    if len(letters) >= 8 and sum(c.isupper() for c in letters) / len(letters) > 0.5:
        signals.append(
            Signal(
                "shouting_subject",
                Severity.BLOCK,
                "subject is more than half capitals",
                "Sentence case reads as human and scores better.",
            )
        )
    if subject.count("!") > 1:
        signals.append(
            Signal("excessive_punctuation", Severity.WARN, "multiple exclamation marks")
        )
    if re.match(r"^\s*(?:re|fwd?)\s*:", subject, re.I):
        signals.append(
            Signal(
                "fake_reply_subject",
                Severity.BLOCK,
                "subject fakes a reply",
                "Filters detect this and it is deceptive.",
            )
        )

    # ---- body ------------------------------------------------------------
    if not text_body.strip():
        signals.append(Signal("empty_text_body", Severity.BLOCK, "no plain-text body"))

    if html_body and not text_body.strip():
        signals.append(
            Signal(
                "html_only",
                Severity.BLOCK,
                "HTML with no plain-text alternative",
                "Send multipart/alternative; HTML-only is a strong spam signal.",
            )
        )

    if html_body:
        text_chars = len(re.sub(r"<[^>]+>", "", html_body).strip())
        image_count = len(re.findall(r"<img\b", html_body, re.I))
        if image_count and text_chars < 200:
            signals.append(
                Signal(
                    "image_heavy",
                    Severity.BLOCK,
                    f"{image_count} image(s) with only {text_chars} characters of text",
                    "Image-only mail is a classic evasion pattern and is filtered.",
                )
            )

    combined = f"{subject}\n{text_body}"
    for pattern in _SPAM_PHRASES:
        match = re.search(pattern, combined, re.I)
        if match:
            signals.append(
                Signal(
                    "spam_phrase",
                    Severity.BLOCK,
                    f"contains {match.group(0)!r}",
                    "Rephrase; these raise spam scores on their own.",
                )
            )
            break

    links = re.findall(r"https?://[^\s<>\")]+", text_body)
    words = len(text_body.split())
    if len(links) > 5:
        signals.append(
            Signal(
                "too_many_links",
                Severity.BLOCK,
                f"{len(links)} links in the body",
                "A first cold email needs one or two.",
            )
        )
    elif links and words and (words / len(links)) < 40:
        signals.append(
            Signal("high_link_density", Severity.WARN, "high link-to-text ratio")
        )

    shorteners = ("bit.ly", "tinyurl.com", "t.co/", "goo.gl", "ow.ly", "is.gd")
    if any(s in text_body for s in shorteners):
        signals.append(
            Signal(
                "url_shortener",
                Severity.BLOCK,
                "body contains a URL shortener",
                "Shorteners hide the destination and are heavily penalised. "
                "Link to the real URL.",
            )
        )

    if not (mailing_address or "").strip():
        signals.append(
            Signal(
                "missing_postal_address",
                Severity.BLOCK,
                "no physical mailing address",
                "Required by CAN-SPAM and expected by filters.",
            )
        )
    elif mailing_address is not None and mailing_address.strip() not in text_body:
        signals.append(
            Signal(
                "address_not_in_body",
                Severity.BLOCK,
                "mailing address is configured but absent from the message",
            )
        )

    if not from_name.strip():
        signals.append(
            Signal(
                "missing_from_name",
                Severity.WARN,
                "no display name on the From: address",
                "A real name performs better than a bare address.",
            )
        )

    if words < 40:
        signals.append(
            Signal(
                "very_short_body",
                Severity.WARN,
                f"only {words} words",
                "Very short cold email reads as templated.",
            )
        )

    return signals


# ==========================================================================
# Reputation
# ==========================================================================
@dataclass(frozen=True, slots=True)
class ReputationWindow:
    """Delivery outcomes over a recent window, per sending domain."""

    sent: int
    delivered: int
    hard_bounced: int
    complained: int

    #: Days since the most recent hard bounce, or ``None`` for never bounced /
    #: nobody looked. A rate cannot tell "bouncing now" from "bounced a
    #: fortnight ago and the window has not rolled yet", and those are different
    #: states that deserve different answers -- see :func:`check_reputation`.
    days_since_bounce: int | None = None

    @property
    def complaint_rate(self) -> float:
        return self.complained / self.delivered if self.delivered else 0.0

    @property
    def bounce_rate(self) -> float:
        return self.hard_bounced / self.sent if self.sent else 0.0

    @property
    def has_signal(self) -> bool:
        return self.sent >= MIN_SAMPLE_FOR_RATES

    @property
    def bounces_are_historical(self) -> bool:
        """True when nothing has hard-bounced for long enough to call it quiet.

        Requires *positive* evidence: ``None`` does not qualify. A mailbox with
        a bad rate and no recorded bounce date is one nobody has measured, and
        an unmeasured mailbox must not be granted the benefit of the doubt.
        """
        return (
            self.days_since_bounce is not None
            and self.days_since_bounce >= BOUNCE_QUIET_DAYS
        )


def check_reputation(window: ReputationWindow) -> list[Signal]:
    """Stop sending before a receiver stops us.

    Rates are only meaningful above a minimum sample; below it, one complaint
    would look catastrophic and pause a healthy campaign.
    """
    signals: list[Signal] = []
    if not window.has_signal:
        return signals

    if window.complaint_rate >= COMPLAINT_RATE_PAUSE:
        signals.append(
            Signal(
                "complaint_rate_exceeded",
                Severity.BLOCK,
                f"complaint rate {window.complaint_rate:.3%} over "
                f"{window.delivered} delivered messages",
                "Pause and review targeting. Gmail's published ceiling is 0.3%, "
                "but reputation damage begins well below it.",
            )
        )
    elif window.complaint_rate >= COMPLAINT_RATE_WARN:
        signals.append(
            Signal(
                "complaint_rate_elevated",
                Severity.WARN,
                f"complaint rate {window.complaint_rate:.3%}",
                "Trending toward the pause threshold.",
            )
        )

    if window.bounce_rate >= BOUNCE_RATE_PAUSE and window.bounces_are_historical:
        # Over the threshold, but nothing has bounced in BOUNCE_QUIET_DAYS. The
        # rate is describing a period that has ended -- typically a list mailed
        # before verification existed -- and holding BLOCK here is what made the
        # bad rate self-perpetuating: no sends, so no clean sends, so no way for
        # the rate to fall. WARN keeps it visible and lets the probation
        # allowance in adaptive_limits actually deliver its five a day.
        #
        # Small on purpose. If the list really is bad, the next bounce arrives
        # within a day or two, days_since_bounce resets, and this returns to
        # BLOCK on its own.
        signals.append(
            Signal(
                "bounce_rate_historical",
                Severity.WARN,
                f"hard-bounce rate {window.bounce_rate:.2%} over {window.sent} "
                f"sends, but none in the last {window.days_since_bounce} days",
                "Sending resumes at a reduced volume so the rate can recover. "
                "It will pause again on the next hard bounce.",
            )
        )
    elif window.bounce_rate >= BOUNCE_RATE_PAUSE:
        signals.append(
            Signal(
                "bounce_rate_exceeded",
                Severity.BLOCK,
                f"hard-bounce rate {window.bounce_rate:.2%} over {window.sent} sends",
                "A high bounce rate tells receivers you do not know who you are "
                "mailing. Review how contacts are being discovered.",
            )
        )
    elif window.bounce_rate >= BOUNCE_RATE_WARN:
        signals.append(
            Signal(
                "bounce_rate_elevated",
                Severity.WARN,
                f"hard-bounce rate {window.bounce_rate:.2%}",
            )
        )

    return signals


# ==========================================================================
# Warm-up
# ==========================================================================
def warmup_day(first_send_at: dt.datetime | None, now: dt.datetime) -> int:
    """Which day of warm-up this mailbox is on, zero-indexed.

    A mailbox that has never sent is on day zero, and so is one whose first send
    is stamped in the future -- a clock skew or a backdated import should not
    hand anything a finished ramp.
    """
    if first_send_at is None:
        return 0
    return max(0, (now.date() - first_send_at.date()).days)


def warmup_limit(
    *, first_send_at: dt.datetime | None, now: dt.datetime, target: int
) -> int | None:
    """Maximum sends allowed today for a mailbox still warming up.

    ``target`` is the mailbox's configured daily limit -- the volume it is
    ramping *towards*. Warm-up is a fraction of that, never of some absolute
    figure chosen elsewhere, which is what makes the ramp mean the same thing
    for a mailbox configured for 50 a day and one configured for 500.

    Returns None once warm-up is complete, meaning only the configured quotas
    apply, and 0 for a mailbox configured to send nothing -- a disabled mailbox
    is not entitled to a warm-up floor.
    """
    if target <= 0:
        return 0
    day = warmup_day(first_send_at, now)
    if day >= WARMUP_DAYS:
        return None
    allowed = math.ceil(WARMUP_RAMP[day] * target)
    # Never above target: a ramp that overshoots the number a human configured
    # would be this module quietly raising someone else's limit.
    return max(MIN_WARMUP_VOLUME, min(allowed, target))


def stepped_warmup_limit(
    *,
    first_send_at: dt.datetime | None,
    now: dt.datetime,
    target: int,
    recent_peak_sends: int | None,
) -> int | None:
    """Today's allowance: the ramp position, bounded by demonstrated volume.

    ``recent_peak_sends`` is the largest number this mailbox has sent on any
    single recent day. ``None`` means nobody looked, which is not the same as
    zero and must not throttle a mailbox on an absence of data -- the ramp
    alone applies.
    """
    ramp = warmup_limit(first_send_at=first_send_at, now=now, target=target)
    if ramp is None or recent_peak_sends is None:
        return ramp
    stepped = math.ceil(recent_peak_sends * MAX_DAILY_STEP_UP)
    return max(MIN_WARMUP_VOLUME, min(ramp, stepped))


def check_warmup(
    *,
    first_send_at: dt.datetime | None,
    sent_today: int,
    now: dt.datetime,
    target: int,
    recent_peak_sends: int | None = None,
) -> list[Signal]:
    if target <= 0:
        # Not a warm-up state at all: either the mailbox is configured to send
        # nothing, or its identity row has gone. Saying "day 1 of warm-up allows
        # 0" would describe a ramp that is not happening.
        return [
            Signal(
                "no_sending_capacity",
                Severity.BLOCK,
                "the sending mailbox has no daily limit configured",
                "Set a daily send limit on the sender identity, or point the "
                "campaign at a mailbox that has one.",
            )
        ]
    limit = stepped_warmup_limit(
        first_send_at=first_send_at,
        now=now,
        target=target,
        recent_peak_sends=recent_peak_sends,
    )
    if limit is None or sent_today < limit:
        return []
    day = warmup_day(first_send_at, now)
    ramp = warmup_limit(first_send_at=first_send_at, now=now, target=target)
    # Which bound actually bit. An operator reading "day 14 allows 12" against a
    # ramp table that says 24 has been told something that looks like a bug.
    bound = (
        f"day {day + 1} of {WARMUP_DAYS} of warm-up allows {limit}"
        if ramp is None or limit >= ramp
        else (
            f"day {day + 1} of {WARMUP_DAYS} of warm-up would allow {ramp}, "
            f"stepped to {limit} until the mailbox has sent at that volume"
        )
    )
    return [
        Signal(
            "warmup_limit_reached",
            Severity.BLOCK,
            f"{bound} of the mailbox's {target} messages; {sent_today} sent",
            "Remaining messages are deferred to tomorrow. Ramping volume "
            "gradually is what stops a new mailbox looking compromised.",
        )
    ]


# ==========================================================================
# Combined preflight
# ==========================================================================
@dataclass(slots=True)
class DeliverabilityContext:
    subject: str
    text_body: str
    html_body: str | None
    from_name: str
    mailing_address: str | None
    headers: dict[str, str]
    reputation: ReputationWindow
    first_send_at: dt.datetime | None
    sent_today: int
    now: dt.datetime
    #: The sending mailbox's configured daily limit -- the volume warm-up ramps
    #: towards. Not a global figure: two mailboxes on the same domain can be
    #: configured for different volumes and each warms towards its own.
    warmup_target: int
    #: Result of titan.delivery.dns_auth.verify_sender_domain, when available.
    auth_errors: tuple[str, ...] = field(default=())
    #: Largest single-day send count in the recent window. None means nobody
    #: measured, which must not be read as zero.
    recent_peak_sends: int | None = None
    #: Documents going out with the message. Empty for every message before
    #: attachments existed, and checked here rather than at assembly so an
    #: attachment passes the same send boundary as every other property.
    attachments: tuple[Any, ...] = field(default=())


def evaluate(ctx: DeliverabilityContext) -> DeliverabilityReport:
    """Every deliverability check, in one report."""
    signals: list[Signal] = []

    for problem in ctx.auth_errors:
        signals.append(
            Signal(
                "authentication_failed",
                Severity.BLOCK,
                problem,
                "Fix the DNS record before sending; unauthenticated bulk mail "
                "is filtered or rejected outright.",
            )
        )

    signals.extend(check_required_headers(ctx.headers))
    signals.extend(check_attachments(ctx.attachments))
    signals.extend(
        check_message(
            subject=ctx.subject,
            text_body=ctx.text_body,
            html_body=ctx.html_body,
            from_name=ctx.from_name,
            mailing_address=ctx.mailing_address,
        )
    )
    signals.extend(check_reputation(ctx.reputation))
    signals.extend(
        check_warmup(
            first_send_at=ctx.first_send_at,
            sent_today=ctx.sent_today,
            now=ctx.now,
            target=ctx.warmup_target,
            recent_peak_sends=ctx.recent_peak_sends,
        )
    )
    return DeliverabilityReport(signals=tuple(signals))


__all__ = [
    "BOUNCE_RATE_PAUSE",
    "COMPLAINT_RATE_PAUSE",
    "MIN_SAMPLE_FOR_RATES",
    "MIN_WARMUP_VOLUME",
    "WARMUP_DAYS",
    "WARMUP_RAMP",
    "DeliverabilityContext",
    "DeliverabilityReport",
    "ReputationWindow",
    "Severity",
    "Signal",
    "build_headers",
    "check_message",
    "check_reputation",
    "check_required_headers",
    "check_warmup",
    "evaluate",
    "warmup_day",
    "warmup_limit",
]
