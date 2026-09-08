"""Deciding when the day is over, and writing the mail that says what it did.

The operator asked for this in one sentence -- "mail me every day after
sending all mails, and if they are not able to send, mail me too" -- and the
second half is the harder one. A report that only fires when the quota is
spent never fires on the day nothing was sent, which is the day it is most
worth having.
"""

from __future__ import annotations

import datetime as dt

from titan.delivery.day_digest import compose, day_is_over
from titan.delivery.day_report import DayReport, Deferral, MailboxDay

NOON = dt.datetime(2026, 9, 7, 12, 0, tzinfo=dt.UTC)
LATE = dt.datetime(2026, 9, 7, 23, 30, tzinfo=dt.UTC)


def mailbox(**overrides) -> MailboxDay:
    base = {
        "sender_identity_id": "11111111-1111-1111-1111-111111111111",
        "label": "primary",
        "from_email": "projects@arslanvuzmallone.com",
        "sent": 5,
        "allowed": 25,
        "configured": 50,
        "queued": 0,
        "health": "healthy",
        "health_as_of": dt.date(2026, 9, 7),
        "warmup_day": 7,
        "warmup_days": 21,
        "note": "5 of 50 a day; warm-up caps today at 25",
    }
    base.update(overrides)
    return MailboxDay(**base)


def report(**overrides) -> DayReport:
    base = {
        "as_of": NOON,
        "window_date": dt.date(2026, 9, 7),
        "sent": 5,
        "ceiling": 25,
        "delivered": 5,
        "bounced": 0,
        "complained": 0,
        "failed": 0,
        "queued": 0,
        "mailboxes": (mailbox(),),
    }
    base.update(overrides)
    return DayReport(**base)


# ==========================================================================
# When the day is over
# ==========================================================================
def test_the_day_is_over_once_every_mailbox_has_spent_its_allowance() -> None:
    """"After sending all quota" is a condition, not a clock.

    The estate's send windows run from Sydney to Vancouver, so there is no
    hour of the day at which sending is reliably finished. What there is, is
    the moment no mailbox can send anything more today.
    """
    spent = report(sent=25, mailboxes=(mailbox(sent=25, allowed=25),))

    assert day_is_over(spent, now=NOON) is True


def test_a_day_with_allowance_left_is_not_over_at_noon() -> None:
    """Planted violation: report as soon as anything has been sent.

    Mailing at the first send would arrive before the day had happened, and
    would be wrong about every number in it.
    """
    assert day_is_over(report(), now=NOON) is False


def test_a_day_that_never_spent_its_quota_is_over_when_the_day_ends() -> None:
    """Planted violation: fire only when the quota is spent.

    This is the half the operator actually asked for twice. A day where
    nothing could be sent -- every mailbox blocked, a bank holiday, the queue
    empty -- never reaches a spent quota, so a report gated on that alone
    would go silent on exactly the days worth reporting. The nine-day stall
    that started this would have produced no mail at all.
    """
    quiet = report(sent=0, mailboxes=(mailbox(sent=0, allowed=25),))

    assert day_is_over(quiet, now=NOON) is False
    assert day_is_over(quiet, now=LATE) is True


def test_a_day_with_no_mailboxes_at_all_still_ends() -> None:
    """Every mailbox blocked leaves nothing to sum, and an empty ``all()`` is
    True -- which would report at 00:01 rather than at the day's end. The
    guard is that a workspace with no capacity has not "spent" anything."""
    nothing = report(sent=0, ceiling=0, mailboxes=())

    assert day_is_over(nothing, now=NOON) is False
    assert day_is_over(nothing, now=LATE) is True


# ==========================================================================
# What the mail says
# ==========================================================================
def test_the_subject_carries_the_number_without_opening_the_mail() -> None:
    """The operator reads this on a phone. The one figure that decides whether
    to look further belongs where it is visible without opening anything."""
    subject, _ = compose(report(sent=14, ceiling=26), recipients=(), bounces=())

    assert "14" in subject and "26" in subject


def test_a_day_that_sent_nothing_says_so_in_the_subject() -> None:
    """Planted violation: use one subject line for every day.

    "0 of 26" and "24 of 26" must not look alike in a notification list. The
    day worth acting on is the one that has to announce itself.
    """
    quiet, _ = compose(
        report(sent=0, mailboxes=(mailbox(sent=0),)), recipients=(), bounces=()
    )
    busy, _ = compose(report(sent=24, ceiling=26), recipients=(), bounces=())

    assert "nothing" in quiet.lower()
    assert quiet != busy


def test_the_body_names_why_each_message_is_waiting() -> None:
    """Planted violation: report the totals and drop the deferral reasons.

    "14 of 26" prompts exactly one question, and a report that cannot answer
    it sends the operator to psql -- which is the errand this replaces.
    """
    held = report(
        sent=14,
        deferrals=(
            Deferral(
                reason="outside_campaign_send_window: Mon 31 Aug is a holiday in GB",
                count=63,
                next_attempt_at=None,
            ),
        ),
    )

    _, body = compose(held, recipients=(), bounces=())

    assert "63" in body
    assert "holiday" in body


def test_the_body_lists_who_was_written_to() -> None:
    """The operator asked for all the details, and the recipients are the
    detail that cannot be reconstructed from any total."""
    _, body = compose(
        report(sent=1),
        recipients=(("hello@example-dental.test", "Dentists Leeds UK", "projects@"),),
        bounces=(),
    )

    assert "hello@example-dental.test" in body
    assert "Dentists Leeds UK" in body


def test_a_bounce_is_reported_with_its_reason_not_just_counted() -> None:
    """A count says something went wrong. The reason says whether it matters:
    a soft bounce never touches reputation and a hard one suppresses an
    address for good."""
    _, body = compose(
        report(sent=5, bounced=1),
        recipients=(),
        bounces=(("info@zahnzentrum.test", "soft", "mailbox full"),),
    )

    assert "info@zahnzentrum.test" in body
    assert "soft" in body


def test_no_delivery_confirmations_is_not_reported_as_none_delivered() -> None:
    """Planted violation: print the delivered count unconditionally.

    Caught by reading a real report rather than by a test. The SMTP pool
    receives no delivery receipts at all -- confirmation would have to come
    from a provider webhook, and there is none -- so `delivered` is
    structurally zero on this deployment. Printed beside "Sent 48" it reads as
    48 failures, every day, for ever: a number that is always wrong in the
    alarming direction is worse than no number.
    """
    _, body = compose(report(sent=48, delivered=0), recipients=(), bounces=())

    assert "Delivered 0" not in body
    assert "not reported" in body


def test_a_delivery_count_that_exists_is_still_shown() -> None:
    """A provider that does report delivery must not have it hidden."""
    _, body = compose(report(sent=48, delivered=46), recipients=(), bounces=())

    assert "46" in body


# ==========================================================================
# A zero that explains itself
# ==========================================================================
def test_an_early_morning_zero_is_not_the_same_as_a_broken_day() -> None:
    """Planted violation: render the count and nothing else.

    The operator opens this first thing and reads "0". At 07:00 that is
    normal -- the send windows have not opened. At 17:00 it means nothing ran
    all day. The panel exists precisely so those two are told apart, and
    printing a bare number made them identical inside the panel itself.
    """
    from titan.delivery.day_digest import day_state

    quiet_morning = report(sent=0, as_of=dt.datetime(2026, 9, 8, 6, 30, tzinfo=dt.UTC))
    quiet_evening = report(sent=0, as_of=dt.datetime(2026, 9, 8, 18, 0, tzinfo=dt.UTC))

    assert day_state(quiet_morning) != day_state(quiet_evening)
    assert "not started" in day_state(quiet_morning).lower()
    assert "nothing" in day_state(quiet_evening).lower()


def test_a_day_that_is_sending_says_so_rather_than_counting() -> None:
    sending = report(sent=46, as_of=dt.datetime(2026, 9, 8, 9, 0, tzinfo=dt.UTC))

    from titan.delivery.day_digest import day_state

    assert "sending" in day_state(sending).lower()


def test_a_spent_day_is_finished_not_stalled() -> None:
    """Planted violation: read a spent quota as a stall.

    Every mailbox at its cap is the system working, and calling it "nothing
    sent" would raise an alarm on the best possible day.
    """
    from titan.delivery.day_digest import day_state

    spent = report(
        sent=25,
        mailboxes=(mailbox(sent=25, allowed=25),),
        as_of=dt.datetime(2026, 9, 8, 15, 0, tzinfo=dt.UTC),
    )

    assert "spent" in day_state(spent).lower() or "done" in day_state(spent).lower()
