"""Warm-up traffic between the operator's own mailboxes.

Hermetic: SMTP and IMAP are both replaced, so nothing here opens a socket.

The first four tests are the ones that matter. Warm-up is the only part of this
system that composes a message and sends it without a draft, an approval, a
suppression check or a send gate — because it is not writing to anybody. The
moment that stops being true it becomes a machine for mailing fabricated
internal notes to strangers, so the tests check that it cannot, rather than
checking that it currently does not.
"""

from __future__ import annotations

import datetime as dt

import pytest
from titan.delivery.mailboxes import parse_mailboxes
from titan.delivery.warmup import (
    MAX_PER_PARTNER,
    WARMUP_HEADER,
    NotAParticipant,
    PlannedSend,
    _build,
    check_recipients_are_participants,
    describe_pool,
    participants_from,
    plan,
    send_round,
)

DAY = dt.date(2026, 8, 24)


def entry(address: str, *, imap: bool = True) -> dict:
    creds = {
        "host": "mail.example.test",
        "port": 465,
        "security": "ssl",
        "username": address,
        "password": f"not-a-real-password-{address}",
    }
    block = {"from_email": address, "smtp": dict(creds)}
    if imap:
        block["imap"] = dict(creds, port=993)
    return block


def pool(*addresses: str, days: dict[str, int] | None = None):
    registry = parse_mailboxes({"mailboxes": [entry(a) for a in addresses]})
    return participants_from(registry, days=days or {})


THREE = (
    "outreach@arslanvuzmallone.com",
    "sales@arslanvuzmallone.com",
    "projects@arslanvuzmallone.com",
)


# ==========================================================================
# It cannot reach a stranger
# ==========================================================================
def test_every_recipient_is_a_mailbox_we_hold_the_password_for() -> None:
    participants = pool(*THREE)

    sends = plan(participants, on_date=DAY)

    assert sends
    addresses = {p.address for p in participants}
    assert {s.recipient.address for s in sends} <= addresses
    assert {s.sender.address for s in sends} <= addresses


def test_a_plan_naming_an_outsider_is_refused() -> None:
    """Belt and braces over ``plan``, because the cost of being wrong is a
    stranger receiving a fabricated internal note from a business they have
    never heard of."""
    participants = pool(*THREE)
    outsider = pool("someone@a-real-business.test")[0]
    smuggled = PlannedSend(
        sender=participants[0],
        recipient=outsider,
        subject="Notes",
        body="...",
        message_id="<x@y>",
    )

    with pytest.raises(NotAParticipant, match="not one of the mailboxes"):
        check_recipients_are_participants([smuggled], participants)


def test_the_module_never_reaches_the_lead_tables() -> None:
    """Structural, not behavioural. A module that cannot import them cannot
    accidentally mail one."""
    import ast
    import pathlib

    source = pathlib.Path("titan/delivery/warmup.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert not any(
        module.startswith(("titan.db", "titan.outreach", "titan.intelligence"))
        for module in imported
    ), sorted(imported)


def test_a_send_only_mailbox_cannot_take_part() -> None:
    """It could be written to and never rescue the mail from spam, never read
    it, and never reply -- which is the whole of what warm-up is for."""
    registry = parse_mailboxes(
        {"mailboxes": [entry(THREE[0]), entry(THREE[1], imap=False)]}
    )

    participants = participants_from(registry)

    assert [p.address for p in participants] == [THREE[0]]


# ==========================================================================
# The mail itself
# ==========================================================================
def test_every_message_says_it_is_warm_up() -> None:
    """So a human reading the mailbox, and this module on a later pass, can
    tell it from something a person wrote."""
    send = plan(pool(*THREE), on_date=DAY)[0]

    assert _build(send)[WARMUP_HEADER] == "1"


def test_the_message_id_is_stable_for_a_day() -> None:
    """Running the command twice in a day must not be two messages."""
    first = plan(pool(*THREE), on_date=DAY)
    second = plan(pool(*THREE), on_date=DAY)

    assert [s.message_id for s in first] == [s.message_id for s in second]


def test_a_different_day_is_a_different_conversation() -> None:
    later = plan(pool(*THREE), on_date=DAY + dt.timedelta(days=1))
    today = plan(pool(*THREE), on_date=DAY)

    assert {s.message_id for s in today}.isdisjoint({s.message_id for s in later})


def test_nothing_in_the_corpus_reads_as_marketing() -> None:
    """These land in mailboxes a person opens. A warm-up corpus that reads like
    outreach teaches the filter exactly the wrong lesson."""
    from titan.delivery.warmup import OPENERS, RESPONSES

    banned = ("unsubscribe", "offer", "free", "click here", "buy", "discount")
    for subject, body in OPENERS:
        blob = f"{subject} {body}".lower()
        assert not any(word in blob for word in banned), subject
    for reply in RESPONSES:
        assert not any(word in reply.lower() for word in banned), reply


# ==========================================================================
# Volume
# ==========================================================================
def test_a_cold_mailbox_starts_small() -> None:
    participants = pool(*THREE, days={THREE[0].lower(): 0})

    sends = [s for s in plan(participants, on_date=DAY) if s.sender.address == THREE[0]]

    assert len(sends) == 2


def test_a_warm_mailbox_carries_more() -> None:
    cold = pool(*THREE, days={THREE[0].lower(): 0})
    warm = pool(*THREE, days={THREE[0].lower(): 19})

    def count(participants):
        return len(
            [s for s in plan(participants, on_date=DAY) if s.sender.address == THREE[0]]
        )

    assert count(warm) > count(cold)


def test_no_partner_is_written_to_more_than_a_colleague_would_be() -> None:
    """Three a day to one person is correspondence; twelve is a machine."""
    participants = pool(*THREE, days={a.lower(): 19 for a in THREE})

    sends = plan(participants, on_date=DAY)

    for sender in THREE:
        per_partner: dict[str, int] = {}
        for send in sends:
            if send.sender.address != sender:
                continue
            per_partner[send.recipient.address] = (
                per_partner.get(send.recipient.address, 0) + 1
            )
        assert all(n <= MAX_PER_PARTNER for n in per_partner.values()), per_partner


def test_a_pair_never_gets_the_same_note_twice_in_a_day() -> None:
    """Choosing with replacement put the same subject in front of the same
    colleague three times in one morning. Two identical messages to one person
    on one day is a machine signature, which is what warm-up must not teach."""
    participants = pool(*THREE, days={a.lower(): 19 for a in THREE})

    seen: dict[tuple[str, str], set[str]] = {}
    for send in plan(participants, on_date=DAY):
        key = (send.sender.address, send.recipient.address)
        subjects = seen.setdefault(key, set())
        assert send.subject not in subjects, f"{key} repeated {send.subject!r}"
        subjects.add(send.subject)


def test_nobody_writes_to_themselves() -> None:
    for send in plan(pool(*THREE), on_date=DAY):
        assert send.sender.address != send.recipient.address


def test_one_mailbox_cannot_warm_itself() -> None:
    assert plan(pool(THREE[0]), on_date=DAY) == []


# ==========================================================================
# What the pool is worth, said out loud
# ==========================================================================
def test_a_single_domain_pool_says_it_is_weak() -> None:
    """Three mailboxes on one domain mostly never leave the server, so they
    build nothing at Gmail. Saying so is more useful than a green tick."""
    description = describe_pool(pool(*THREE))

    assert "never leaves the server" in description
    assert "another provider" in description


def test_a_cross_provider_pool_says_it_works() -> None:
    description = describe_pool(
        pool(THREE[0], "arslan.test@gmail.test", "arslan.test@outlook.test")
    )

    assert "3 domains" in description


def test_a_pool_of_one_says_what_is_missing() -> None:
    assert "at least a sender and a receiver" in describe_pool(pool(THREE[0]))


# ==========================================================================
# Sending
# ==========================================================================
async def test_a_message_already_delivered_is_not_sent_again(monkeypatch) -> None:
    """The command is safe to run twice in a day."""
    participants = pool(*THREE)
    sends = plan(participants, on_date=DAY)
    delivered = {s.message_id for s in sends}

    async def already(participant, *, timeout_seconds=30.0):
        return delivered

    attempted: list[str] = []

    def never(send, *, timeout):
        attempted.append(send.message_id)
        return None

    monkeypatch.setattr("titan.delivery.warmup.already_delivered", already)
    monkeypatch.setattr("titan.delivery.warmup._smtp_send", never)

    report = await send_round(sends)

    assert report.skipped_already_sent == len(sends)
    assert report.sent == 0
    assert attempted == []


async def test_a_failed_send_is_counted_and_named(monkeypatch) -> None:
    participants = pool(*THREE)
    sends = plan(participants, on_date=DAY)[:2]

    async def none_delivered(participant, *, timeout_seconds=30.0):
        return set()

    def refuse(send, *, timeout):
        return "SMTPAuthenticationError: 535 bad credentials"

    monkeypatch.setattr("titan.delivery.warmup.already_delivered", none_delivered)
    monkeypatch.setattr("titan.delivery.warmup._smtp_send", refuse)

    report = await send_round(sends)

    assert report.failed == 2
    assert report.sent == 0
    assert all("535" in line for line in report.errors)
