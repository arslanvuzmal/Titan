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

    Three ways to be finished. Every mailbox having spent its allowance is the
    operator's own phrasing -- "after sending all quota". The clock is the
    backstop for the day that never gets there.

    The third is the one that made this work at all. The first two both assume
    the estate is still running when the day ends, and it is not: this runs on
    a laptop that is closed at night. On 8 September the whole stack was off
    from 15:15 until 12:43 the next day, and in eleven runs this function had
    never once returned True -- the hour was never >= 23 while anything was
    watching, and the quota never spent because there was nobody up to spend
    it. The report was never late. It was structurally unreachable.

    So a day that is simply *in the past* is over. That is true whether or not
    anybody was awake to see it end, and it is what lets the morning's first
    run post yesterday's figures.
    """
    if report.window_date < now.date():
        return True
    if now.hour >= LAST_HOUR:
        return True
    # `all()` over an empty pool is True, which would call a workspace with no
    # sending mailboxes "finished" one minute after midnight. A workspace that
    # cannot send has not spent anything.
    if not report.mailboxes:
        return False
    return all(box.remaining <= 0 for box in report.mailboxes)


#: The hour the earliest market's send window opens, in UTC.
#:
#: Sydney is the first of the day and 08:00 there is well before 08:00 UTC, but
#: what matters here is the operator's own morning: he opens the dashboard at
#: about seven UTC and everything before then is legitimately quiet. Used only
#: to tell "the day has not started" from "the day did not happen".
FIRST_WINDOW_HOUR = 7


def day_state(report: DayReport) -> str:
    """One sentence saying what the number means.

    The panel exists so that a day sending forty and a day sending nothing
    cannot be mistaken for each other -- and the first version printed a bare
    count, which made 0 at seven in the morning and 0 at six in the evening
    look exactly alike. The operator sees the first one every day and reads it
    as a fault, correctly, because nothing on screen said otherwise.

    Four states, and the zero splits into three of them.
    """
    hour = report.as_of.hour
    if report.sent == 0:
        if hour < FIRST_WINDOW_HOUR:
            return "Not started — the first send window has not opened yet"
        if report.queued:
            return f"Not started — {report.queued} waiting for a window to open"
        return "Nothing sent, and nothing waiting to go — worth looking at"
    if report.remaining <= 0:
        return f"Today's allowance is spent — {report.sent} sent"
    return f"Sending — {report.sent} of {report.ceiling} so far"


def compose(
    report: DayReport,
    *,
    recipients: tuple[tuple[str, str, str], ...],
    bounces: tuple[tuple[str, str, str], ...],
    alarms: tuple[tuple[str, int, str], ...] = (),
) -> tuple[str, str]:
    """The subject and body of the day's mail.

    ``recipients`` is (address, campaign, mailbox) per message sent; ``bounces``
    is (address, kind, reason). Both are passed in rather than queried here so
    this stays a function of its inputs and the wording can be tested without a
    database.

    ``alarms`` is (kind, count, newest title) for everything still open in the
    operator queue, and it is the reason this mail is worth sending at all.

    Every failure this estate had on 9 September was already detected and had
    already filed a notification: the wedged schedule, the blocked mailboxes,
    the stalled campaigns. 646 of them were sitting unread, and the operator
    found out about each one by asking. A system that notices everything and
    tells nobody is not autonomous, it is just well instrumented -- so the
    day's mail now carries what needs a person, not only what happened.
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

    if alarms:
        # First after the mailboxes, deliberately. This is the section that
        # exists to be acted on; the sending figures are context for it.
        total = sum(count for _kind, count, _title in alarms)
        lines += ["", f"NEEDS YOU ({total})"]
        for kind, count, newest in alarms:
            lines.append(f"  {count:>4}  {kind:<22} {newest}")

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


__all__ = ["FIRST_WINDOW_HOUR", "LAST_HOUR", "compose", "day_is_over", "day_state"]
