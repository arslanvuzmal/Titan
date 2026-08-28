"""Titan's own mailbox verification.

Hermetic: DNS is injected and :mod:`smtplib` is replaced, so nothing here
resolves a name or opens a socket.

The properties under test are mostly *refusals to act*. A verifier that probes
where the answer is worthless, or that calls a timeout "invalid", damages
either somebody else's mail server or your own list -- and both failures are
silent. So the assertions are about what does not happen as much as what does.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from titan.db.enums import SENDABLE_VERIFICATION_STATUSES, VerificationStatus
from titan.intelligence.mx import DomainDoesNotExist
from titan.intelligence.smtp_probe import (
    ProbeConfigError,
    SmtpProbeVerifier,
)

HELO = "mail.arslanvuzmallone.com"
FROM = "postmaster@arslanvuzmallone.com"


# ------------------------------------------------------------------- doubles
class FakeSmtp:
    """Records the whole conversation. Never speaks to anything."""

    opened: ClassVar[list[FakeSmtp]] = []
    #: (code, text) per RCPT, in order: decoy first, then the real address.
    script: ClassVar[list[tuple[int, bytes]]] = []
    #: Raised on connect, to stand in for a refused or timed-out connection.
    connect_error: ClassVar[Exception | None] = None

    def __init__(self, host, port, timeout=None, local_hostname=None):
        if FakeSmtp.connect_error is not None:
            raise FakeSmtp.connect_error
        self.host = host
        self.port = port
        self.local_hostname = local_hostname
        self.calls: list[tuple] = []
        self._replies = list(FakeSmtp.script)
        FakeSmtp.opened.append(self)

    def ehlo_or_helo_if_needed(self):
        self.calls.append(("ehlo",))

    def mail(self, sender):
        self.calls.append(("mail", sender))
        return 250, b"2.1.0 Ok"

    def rcpt(self, address):
        self.calls.append(("rcpt", address))
        return self._replies.pop(0)

    def data(self, message):  # pragma: no cover - must never be reached
        self.calls.append(("data", message))
        raise AssertionError("a verification probe must never send a message")

    def rset(self):
        self.calls.append(("rset",))
        return 250, b"2.0.0 Ok"

    def quit(self):
        self.calls.append(("quit",))
        return 221, b"2.0.0 Bye"

    @classmethod
    def reset(cls, *, script=(), connect_error=None):
        cls.opened = []
        cls.script = list(script)
        cls.connect_error = connect_error


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSmtp.reset()
    monkeypatch.setattr("titan.intelligence.smtp_probe.smtplib.SMTP", FakeSmtp)
    return FakeSmtp


def resolver_for(hosts: list[str], *, has_address: bool = True, nx: bool = False):
    def resolve(domain: str):
        if nx:
            raise DomainDoesNotExist(domain)
        return list(hosts), has_address

    return resolve


def probe(hosts: list[str], **kw) -> SmtpProbeVerifier:
    return SmtpProbeVerifier(
        helo_hostname=HELO,
        mail_from=FROM,
        min_domain_interval=0.0,
        resolver=resolver_for(hosts, **kw),
    )


# ------------------------------------------------------------ identification
def test_a_probe_must_have_a_real_hostname_to_introduce_itself_with() -> None:
    """An invented HELO name is the first thing a receiver checks."""
    with pytest.raises(ProbeConfigError, match="hostname"):
        SmtpProbeVerifier(helo_hostname="", mail_from=FROM)


def test_a_probe_must_have_a_sender_that_can_receive_a_complaint() -> None:
    """Probing with a null or fictional sender is the abusive kind."""
    with pytest.raises(ProbeConfigError, match="envelope sender"):
        SmtpProbeVerifier(helo_hostname=HELO, mail_from="")


async def test_the_configured_hostname_is_what_is_announced() -> None:
    FakeSmtp.reset(script=[(550, b"no such user"), (250, b"Ok")])

    await probe(["mx.small-dental.test"]).verify("sam@small-dental.test")

    assert FakeSmtp.opened[0].local_hostname == HELO
    assert ("mail", FROM) in FakeSmtp.opened[0].calls


# --------------------------------------------------- the operators not asked
async def test_a_google_workspace_domain_is_never_contacted() -> None:
    """The larger half of what keeps this defensible.

    Google accepts every recipient at RCPT and rate-limits probers, so the
    answer would be worthless and the asking would cost reputation. Not a
    degraded result -- UNKNOWN is the honest one, and no connection is opened.
    """
    result = await probe(["aspmx.l.google.com", "alt1.aspmx.l.google.com"]).verify(
        "sam@a-real-business.test"
    )

    assert result.status is VerificationStatus.UNKNOWN
    assert FakeSmtp.opened == []
    assert "google.com" in (result.detail or "")


@pytest.mark.parametrize(
    "mx_host",
    [
        "a-real-business-test.mail.protection.outlook.com",
        "mx1.emailsrvr.pphosted.com",
        "eu-smtp-inbound-1.mimecast.com",
        "mx.yandex.net",
    ],
)
async def test_the_other_uninformative_operators_are_never_contacted(
    mx_host: str,
) -> None:
    result = await probe([mx_host]).verify("sam@a-real-business.test")

    assert result.status is VerificationStatus.UNKNOWN
    assert FakeSmtp.opened == []


async def test_an_uninformative_answer_never_condemns_the_address() -> None:
    """UNKNOWN must not read as INVALID anywhere downstream."""
    result = await probe(["aspmx.l.google.com"]).verify("sam@a-real-business.test")

    assert not result.is_conclusive
    assert result.status not in SENDABLE_VERIFICATION_STATUSES


# ------------------------------------------------------------------- the DNS
async def test_a_domain_that_does_not_exist_is_invalid() -> None:
    result = await probe([], nx=True).verify("sam@not-registered.test")

    assert result.status is VerificationStatus.INVALID
    assert FakeSmtp.opened == []


async def test_a_domain_with_no_route_for_mail_is_invalid() -> None:
    result = await probe([], has_address=False).verify("sam@parked.test")

    assert result.status is VerificationStatus.INVALID


async def test_a_failed_lookup_is_unknown_not_invalid() -> None:
    """A bad minute on our resolver must not discard a real customer."""

    def broken(domain: str):
        raise OSError("resolver unreachable")

    verifier = SmtpProbeVerifier(
        helo_hostname=HELO, mail_from=FROM, min_domain_interval=0.0, resolver=broken
    )

    result = await verifier.verify("sam@small-dental.test")

    assert result.status is VerificationStatus.UNKNOWN
    assert FakeSmtp.opened == []


async def test_a_domain_with_only_an_address_record_is_probed_at_itself() -> None:
    """RFC 5321: no MX means the A record is the implicit destination."""
    FakeSmtp.reset(script=[(550, b"no"), (250, b"Ok")])

    result = await probe([], has_address=True).verify("sam@small-dental.test")

    assert result.status is VerificationStatus.PROVIDER_VERIFIED
    assert FakeSmtp.opened[0].host == "small-dental.test"


# ------------------------------------------------------------- the catch-all
async def test_a_domain_that_accepts_an_impossible_address_accepts_everything() -> None:
    """The failure that makes cheap verification worse than useless.

    If the server accepts a random local part, accepting the real one proves
    nothing -- and CATCH_ALL is not sendable on its own.
    """
    FakeSmtp.reset(script=[(250, b"Ok"), (250, b"Ok")])

    result = await probe(["mx.catchall.test"]).verify("sam@catchall.test")

    assert result.status is VerificationStatus.CATCH_ALL
    assert result.is_catch_all
    assert result.status not in SENDABLE_VERIFICATION_STATUSES


async def test_the_decoy_is_asked_about_first() -> None:
    """Order matters: asking the real address first and the decoy second would
    have already spent the answer by the time catch-all was known."""
    FakeSmtp.reset(script=[(550, b"no"), (250, b"Ok")])

    await probe(["mx.small-dental.test"]).verify("sam@small-dental.test")

    rcpts = [c[1] for c in FakeSmtp.opened[0].calls if c[0] == "rcpt"]
    assert rcpts[0].startswith("titan-verify-probe-")
    assert rcpts[1] == "sam@small-dental.test"


async def test_a_known_catch_all_domain_is_not_reopened() -> None:
    """A second address at the same domain learns nothing and costs a connection."""
    FakeSmtp.reset(script=[(250, b"Ok"), (250, b"Ok")])
    verifier = probe(["mx.catchall.test"])

    first = await verifier.verify("sam@catchall.test")
    second = await verifier.verify("alex@catchall.test")

    assert first.status is second.status is VerificationStatus.CATCH_ALL
    assert len(FakeSmtp.opened) == 1


# --------------------------------------------------------------- the verdict
async def test_an_accepted_recipient_on_a_selective_domain_is_verified() -> None:
    FakeSmtp.reset(script=[(550, b"5.1.1 no such user"), (250, b"2.1.5 Ok")])

    result = await probe(["mx.small-dental.test"]).verify("sam@small-dental.test")

    assert result.status is VerificationStatus.PROVIDER_VERIFIED
    assert result.status in SENDABLE_VERIFICATION_STATUSES


async def test_a_refused_recipient_on_a_selective_domain_is_invalid() -> None:
    FakeSmtp.reset(script=[(550, b"5.1.1 no such user"), (550, b"5.1.1 no such user")])

    result = await probe(["mx.small-dental.test"]).verify("gone@small-dental.test")

    assert result.status is VerificationStatus.INVALID


async def test_a_full_mailbox_is_risky_not_invalid() -> None:
    """552 is a real mailbox belonging to a real person having a bad week."""
    FakeSmtp.reset(script=[(550, b"no such user"), (552, b"mailbox full")])

    result = await probe(["mx.small-dental.test"]).verify("sam@small-dental.test")

    assert result.status is VerificationStatus.RISKY


@pytest.mark.parametrize("code", [450, 451, 421])
async def test_a_temporary_refusal_settles_nothing(code: int) -> None:
    """Greylisting is the normal first answer from a careful server."""
    FakeSmtp.reset(script=[(550, b"no such user"), (code, b"try again later")])

    result = await probe(["mx.small-dental.test"]).verify("sam@small-dental.test")

    assert result.status is VerificationStatus.UNKNOWN


async def test_a_refused_connection_is_unknown_not_invalid() -> None:
    """The failure was ours. Condemning their address for it is the one
    mistake here that nothing downstream would ever show."""
    FakeSmtp.reset(connect_error=OSError("connection refused"))

    result = await probe(["mx.small-dental.test"]).verify("sam@small-dental.test")

    assert result.status is VerificationStatus.UNKNOWN


async def test_a_temporary_answer_on_the_decoy_cannot_certify_the_real_one() -> None:
    """Caught by this test while it was still wrong.

    Only a permanent rejection of the decoy proves the domain is selective. A
    421 or a 450 is "not now" -- greylisting and rate limiting both look like
    this -- and reading it as "the domain rejects unknown recipients" would
    turn the very next 250 into a verified mailbox on the strength of an answer
    nobody gave. That is the false positive the whole module is arranged to
    avoid, arriving through the back door.
    """
    FakeSmtp.reset(script=[(421, b"too many connections"), (250, b"Ok")])

    result = await probe(["mx.small-dental.test"]).verify("sam@small-dental.test")

    assert result.status is VerificationStatus.UNKNOWN


# ------------------------------------------------- refused us, not them
async def test_a_blocklist_rejection_is_about_our_ip_not_their_mailbox() -> None:
    """Found on the first live run, and it would have been expensive.

    Three real addresses came back INVALID because the receiving servers
    refused the *connection*: one for a missing reverse-DNS record, two on
    Barracuda's reputation list. A server refusing the caller refuses the decoy
    and the real address alike, which is the same shape as a selective server
    rejecting a mailbox that does not exist.
    """
    FakeSmtp.reset(
        script=[
            (550, b"JunkMail rejected - is in an RBL: Reverse DNS (PTR) missing"),
            (550, b"JunkMail rejected - is in an RBL: Reverse DNS (PTR) missing"),
        ]
    )

    result = await probe(["mx.small-dental.test"]).verify("sam@small-dental.test")

    assert result.status is VerificationStatus.UNKNOWN
    assert "rbl" in (result.raw.get("sender_rejected") or "")


async def test_a_reputation_block_is_not_a_verdict_on_the_address() -> None:
    FakeSmtp.reset(
        script=[
            (550, b"http://www.barracudanetworks.com/reputation/?pr=1&ip=1.2.3.4"),
            (550, b"http://www.barracudanetworks.com/reputation/?pr=1&ip=1.2.3.4"),
        ]
    )

    result = await probe(["mx.small-dental.test"]).verify("sam@small-dental.test")

    assert result.status is VerificationStatus.UNKNOWN


async def test_a_bare_550_with_no_stated_reason_settles_nothing() -> None:
    """Both refused is the shape of a dead mailbox *and* the shape of a server
    refusing us. One connection cannot separate them on codes alone, so INVALID
    needs the reply to say the mailbox is the reason."""
    FakeSmtp.reset(script=[(550, b"5.7.1 rejected"), (550, b"5.7.1 rejected")])

    result = await probe(["mx.small-dental.test"]).verify("sam@small-dental.test")

    assert result.status is VerificationStatus.UNKNOWN


@pytest.mark.parametrize(
    "reply",
    [
        b"5.1.1 <sam@small-dental.test>: Recipient address rejected: User unknown",
        b"550 no such user here",
        b"550 5.1.1 The email account that you tried to reach does not exist",
        b"550 mailbox unavailable",
    ],
)
async def test_a_reply_that_names_the_mailbox_as_the_reason_is_invalid(
    reply: bytes,
) -> None:
    FakeSmtp.reset(script=[(550, b"550 5.1.1 user unknown"), (550, reply)])

    result = await probe(["mx.small-dental.test"]).verify("gone@small-dental.test")

    assert result.status is VerificationStatus.INVALID


async def test_the_health_line_reports_servers_that_refused_the_probe() -> None:
    """A rising count is one fact about this host's IP, not many facts about
    other people's mailboxes, and the operator needs it said that way."""
    FakeSmtp.reset(script=[(550, b"blocked by policy"), (550, b"blocked by policy")])
    verifier = probe(["mx.small-dental.test"])

    await verifier.verify("sam@small-dental.test")
    _, detail = await verifier.health_check()

    assert "refused the probe itself" in detail


# --------------------------------------------------------------- the conduct
async def test_the_probe_never_sends_a_message() -> None:
    """The property that makes this verification rather than mail.

    ``FakeSmtp.data`` raises, so a path that ever reached it would fail loudly
    rather than delivering something to a stranger.
    """
    FakeSmtp.reset(script=[(550, b"no"), (250, b"Ok")])

    await probe(["mx.small-dental.test"]).verify("sam@small-dental.test")

    verbs = [call[0] for call in FakeSmtp.opened[0].calls]
    assert "data" not in verbs
    assert verbs == ["ehlo", "mail", "rcpt", "rcpt", "rset", "quit"]


async def test_both_addresses_are_asked_on_one_connection() -> None:
    """Two connections would show a small mail server two callers."""
    FakeSmtp.reset(script=[(550, b"no"), (250, b"Ok")])

    await probe(["mx.small-dental.test"]).verify("sam@small-dental.test")

    assert len(FakeSmtp.opened) == 1


async def test_the_transaction_is_abandoned_explicitly() -> None:
    """RSET then QUIT, rather than dropping the socket mid-transaction."""
    FakeSmtp.reset(script=[(550, b"no"), (250, b"Ok")])

    await probe(["mx.small-dental.test"]).verify("sam@small-dental.test")

    verbs = [call[0] for call in FakeSmtp.opened[0].calls]
    assert verbs[-2:] == ["rset", "quit"]


# ---------------------------------------------------------------- the syntax
@pytest.mark.parametrize("address", ["", "not-an-address", "@nothing.test", "sam@", "sam@localhost"])
async def test_an_unusable_address_is_refused_without_a_lookup(address: str) -> None:
    result = await probe(["mx.small-dental.test"]).verify(address)

    assert result.status is VerificationStatus.INVALID
    assert FakeSmtp.opened == []


# ----------------------------------------------------------------- the wiring
def test_an_unconfigured_probe_falls_back_to_verifying_nothing() -> None:
    """A missing hostname must not take the discovery pipeline down, and the
    null verifier is safe in the direction that matters: it can never mark an
    address sendable."""
    from titan.intelligence.verifier import build_verifier, reset_verifier_cache

    reset_verifier_cache()

    class Settings:
        smtp_probe_helo = None
        smtp_probe_mail_from = None

    try:
        assert build_verifier("smtp_probe", Settings()).name == "null"
    finally:
        reset_verifier_cache()


def test_the_probe_is_built_once_rather_than_once_per_address() -> None:
    """Its catch-all cache and its per-domain spacing are the whole of what
    stops it opening fifty connections to one small mail server. A fresh
    instance per address has none of them."""
    from titan.intelligence.verifier import build_verifier, reset_verifier_cache

    reset_verifier_cache()

    class Settings:
        smtp_probe_helo = HELO
        smtp_probe_mail_from = FROM
        smtp_probe_timeout_seconds = 12
        smtp_probe_concurrency = 4

    try:
        first = build_verifier("smtp_probe", Settings())
        second = build_verifier("smtp_probe", Settings())
        assert first is second
    finally:
        reset_verifier_cache()
