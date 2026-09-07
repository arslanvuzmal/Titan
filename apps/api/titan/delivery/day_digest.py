"""The day's sending, written as a mail the operator actually reads.

Asked for in one sentence: *mail me every day after sending all mails, with
all details -- and if they are not able to send, mail me too.* The second half
is the harder one and it decides the shape of the first.

**"After sending all quota" is a condition, not a clock.** The send windows on
this workspace run from Sydney to Vancouver, so there is no hour at which
sending is reliably finished; what there is, is the moment no mailbox can send
anything more today. That is what :func:`day_is_over` waits for.

**But a day that never spends its quota still has to report.** Every mailbox
blocked, a bank holiday, an empty queue -- none of those reach a spent
allowance, and a report gated on that alone would go quiet on precisely the
days worth hearing about. The four silent days that prompted this would have
produced no mail at all. So the day also ends when the day ends.

**It is not sent through the outbox, and that is not an optimisation.** A
report about the day's sending that was itself a tracked message would consume
outreach quota, enter the bounce statistics, and distort the numbers it exists
to report. It goes over SMTP directly and never becomes a row in ``messages``.
"""

from __future__ import annotations

import datetime as dt

from titan.delivery.day_report import DayReport

#: The hour after which an unspent day is called finished anyway.
#:
#: 23:00 UTC rather than midnight: the quota engine counts from midnight UTC,
#: so a report composed after the boundary would describe a day that had
#: already been replaced by an empty one.
LAST_HOUR = 23


def day_is_over(report: DayReport, *, now: dt.datetime) -> bool:
    """Whether there is nothing more this day can send.

    Two ways to be finished, and both are needed. Every mailbox having spent
    its allowance is the operator's own phrasing -- "after sending all quota".
    The clock is the backstop for the day that never gets there.
    """
    if now.hour >= LAST_HOUR:
        return True
    # `all()` over an empty pool is True, which would call a workspace with no
    # sending mailboxes "finished" one minute after midnight. A workspace that
    # cannot send has not spent anything.
    if not report.mailboxes:
        return False
    return all(box.remaining <= 0 for box in report.mailboxes)


def compose(
    report: DayReport,
    *,
    recipients: tuple[tuple[str, str, str], ...],
    bounces: tuple[tuple[str, str, str], ...],
) -> tuple[str, str]:
    """The subject and body of the day's mail.

    ``recipients`` is (address, campaign, mailbox) per message sent; ``bounces``
    is (address, kind, reason). Both are passed in rather than queried here so
    this stays a function of its inputs and the wording can be tested without a
    database.
    """
    date = report.window_date.isoformat()
    if report.sent == 0:
        subject = f"Titan sent nothing today ({date})"
    else:
        subject = f"Titan sent {report.sent} of {report.ceiling} today ({date})"

    lines: list[str] = [
        subject,
        "",
        f"Sent      {report.sent} of a ceiling of {report.ceiling}",
        # Never a bare zero beside a non-zero "sent". The SMTP pool receives
        # no delivery receipts -- confirmation would have to come from a
        # provider webhook and there is none -- so this is structurally zero
        # on the current carrier. "Delivered 0" under "Sent 48" reads as 48
        # failures, every day, and a number that is always wrong in the
        # alarming direction is worse than no number at all.
        (
            f"Delivered {report.delivered}"
            if report.delivered
            else "Delivered not reported by this carrier"
        ),
        f"Bounced   {report.bounced}",
        f"Failed    {report.failed}",
        f"Waiting   {report.queued}",
        "",
        "MAILBOXES",
    ]
    for box in report.mailboxes:
        # The throttle's own sentence, not a status colour. "5 of 50 a day;
        # warm-up caps today at 5" answers the next question before it is
        # asked; "degraded" sends the reader back to the database.
        lines.append(f"  {box.from_email:<34} {box.sent:>3} sent   {box.note}")
    if not report.mailboxes:
        lines.append("  none configured")

    if report.deferrals:
        # The gap between sent and ceiling is a question, and this is the
        # answer to it. Reporting the totals without these is what sent the
        # operator to psql in the first place.
        lines += ["", "WHY THE REST IS WAITING"]
        for held in report.deferrals:
            lines.append(f"  {held.count:>4}  {held.reason}")

    if bounces:
        lines += ["", "BOUNCES"]
        for address, kind, reason in bounces:
            lines.append(f"  {address:<40} {kind:<8} {reason}")

    if recipients:
        lines += ["", f"WRITTEN TO ({len(recipients)})"]
        for address, campaign, mailbox in recipients:
            lines.append(f"  {address:<40} {campaign:<30} via {mailbox}")

    lines += ["", f"Read at {report.as_of.isoformat(timespec='seconds')}."]
    return subject, "\n".join(lines)


__all__ = ["LAST_HOUR", "compose", "day_is_over"]
