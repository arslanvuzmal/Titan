"""Two components disagreeing about how warm the same mailbox is.

``_capture_sender_health`` positioned a mailbox with ``_earliest(Titan's first
send, the provider's warm-up start)``; ``_check_deliverability`` used Titan's
first send alone. On 20 August the snapshot for ``sales@`` recorded day 13,
allowance 25, and the send gate enforced day 2, allowance 6 -- for the same
mailbox, on the same day, from the same worker.

The dashboard was not lying and neither was the gate. They were answering
different questions and only one of them decided anything.

Aligning them alone would have handed a day-13 allowance to a mailbox that had
never sent more than six in a day, and arriving at a volume is safe while
jumping to it is one of the patterns receivers watch for. So the ramp positions
by age and ``MAX_DAILY_STEP_UP`` bounds by evidence: the allowance may at most
double what the mailbox has actually demonstrated.
"""

from __future__ import annotations

import datetime as dt

from titan.delivery.deliverability import (
    MAX_DAILY_STEP_UP,
    MIN_WARMUP_VOLUME,
    WARMUP_DAYS,
    check_warmup,
    stepped_warmup_limit,
    warmup_limit,
)

NOW = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC)
TARGET = 50


def ramp_day(day: int) -> dt.datetime:
    """A first-send timestamp that puts the mailbox on ``day`` of the ramp."""
    return NOW - dt.timedelta(days=day)


def stepped(day: int, peak: int | None) -> int | None:
    return stepped_warmup_limit(
        first_send_at=ramp_day(day), now=NOW, target=TARGET, recent_peak_sends=peak
    )


# ------------------------------------------------------ bounding the jump


def test_the_allowance_cannot_jump_further_than_double() -> None:
    """The live case. Day 13 of the ramp allows 25; the mailbox had sent 6."""
    assert warmup_limit(first_send_at=ramp_day(13), now=NOW, target=TARGET) == 25
    assert stepped(13, 6) == 12


def test_it_reaches_the_ramp_within_days_not_weeks() -> None:
    """The bound costs days and never the destination. From six, against a
    day-13 allowance of 25: 12, then 24, then the ramp itself."""
    assert stepped(13, 6) == 12
    assert stepped(14, 12) == 24
    assert stepped(15, 24) == 31  # the ramp is now the lower bound, not the step


def test_a_mailbox_already_at_volume_is_not_held_back() -> None:
    """The bound only ever bites on a jump. A mailbox sending at its ramp
    position keeps its ramp position."""
    assert stepped(13, 25) == 25


def test_nothing_measured_is_not_a_peak_of_zero() -> None:
    """Planted violation: default ``recent_peak_sends`` to 0 and this fails.

    Zero would throttle every mailbox to the floor on an absence of data --
    including one quarantined for a week by the reputation gate, which would
    then be unable to send its way back out.
    """
    assert stepped(13, None) == 25
    assert stepped(13, 0) == MIN_WARMUP_VOLUME


def test_a_finished_ramp_stays_finished() -> None:
    """Past the ramp there is no warm-up limit at all, and the step bound must
    not reintroduce one -- the configured quota is the only ceiling left."""
    assert stepped(WARMUP_DAYS + 5, 6) is None


def test_the_step_multiple_is_a_multiple_not_an_increment() -> None:
    """Relative growth, matching the ramp's own geometry. A fixed increment
    would mean the same thing for a mailbox heading to 50 and one heading to
    500, which is exactly what the ramp's docstring rejects."""
    assert MAX_DAILY_STEP_UP > 1.0
    assert stepped(13, 4) == 8
    assert stepped(13, 10) == 20


# ------------------------------------------------------- what it reports


def test_a_stepped_refusal_says_so_rather_than_misquoting_the_ramp() -> None:
    """An operator reading "day 14 allows 12" against a ramp table that says 25
    has been told something that looks like a bug."""
    signals = check_warmup(
        first_send_at=ramp_day(13),
        sent_today=12,
        now=NOW,
        target=TARGET,
        recent_peak_sends=6,
    )

    assert len(signals) == 1
    detail = signals[0].detail
    assert "would allow 25" in detail
    assert "stepped to 12" in detail


def test_an_ordinary_ramp_refusal_reads_as_before() -> None:
    signals = check_warmup(
        first_send_at=ramp_day(1),
        sent_today=99,
        now=NOW,
        target=TARGET,
        recent_peak_sends=None,
    )

    assert len(signals) == 1
    assert "stepped" not in signals[0].detail
    assert "of warm-up allows" in signals[0].detail


def test_the_two_paths_now_read_the_same_field() -> None:
    """Planted violation: revert the gate to ``min(sent_at)`` alone and this
    fails. The disagreement was invisible precisely because each half was
    correct on its own."""
    import inspect

    from titan.delivery import outbox_worker

    gate = inspect.getsource(outbox_worker.OutboxWorker._check_deliverability)
    health = inspect.getsource(outbox_worker.OutboxWorker._capture_sender_health)

    assert "_earliest(" in gate
    assert "_earliest(" in health
    assert "warmup_started_at" in gate


# ------------------------------------------- the bound must not feed itself


def test_the_peak_window_excludes_today() -> None:
    """Planted violation: drop ``sent_at < :today_start`` and this fails.

    A bound on day-over-day growth cannot count today's own sends as evidence
    for how much may be sent today. With today included the bound raised itself
    as it was consumed -- six sent permitted twelve, the twelfth made the peak
    twelve which permitted twenty-four -- and one batch walked sales@ from six
    to its full ramp allowance of twenty-five in a single burst on 20 August.

    The jump the bound exists to prevent, produced by the bound.
    """
    import inspect

    from titan.delivery import outbox_worker

    gate = inspect.getsource(outbox_worker.OutboxWorker._check_deliverability)
    peak_query = gate[gate.index("recent_peak_sends = (") :]

    assert "today_start" in peak_query
    assert "sent_at < :today_start" in peak_query


def test_the_bound_is_stable_as_the_day_is_consumed() -> None:
    """The property the query above buys, stated on the pure function.

    Yesterday's peak is a fixed number for the whole of today, so the allowance
    it implies does not move while today's sends land against it.
    """
    yesterdays_peak = 6

    allowance = stepped(13, yesterdays_peak)
    for _sent_so_far in range(allowance):
        assert stepped(13, yesterdays_peak) == allowance
