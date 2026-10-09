"""The placement gate: a mailbox sends cold mail only on a recent inbox reading."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from coldops.delivery import deliverability, placement_gate
from coldops.delivery.placement_gate import (
    BELOW_FLOOR,
    DOMAIN_RESTING,
    UNMEASURED,
    Reading,
    assess,
    rest_until,
)

NOW = dt.datetime(2026, 10, 10, 9, 0, tzinfo=dt.UTC)
BOX = "arslan@vuzmalstudio.com"
SIBLING = "hello@vuzmalstudio.com"


def r(when: dt.datetime, folder: str, box: str = BOX) -> Reading:
    return Reading(from_email=box, sent_at=when, folder=folder)


def hours_ago(h: float) -> dt.datetime:
    return NOW - dt.timedelta(hours=h)


def day(d: int, hour: int = 7) -> dt.datetime:
    return dt.datetime(2026, 10, d, hour, 0, tzinfo=dt.UTC)


# -------------------------------------------------------------- the floor
def test_no_readings_is_unmeasured_not_fine() -> None:
    verdict = assess(BOX, [], now=NOW)
    assert not verdict.may_send
    assert verdict.code == UNMEASURED


def test_unread_probes_do_not_count_as_measured() -> None:
    verdict = assess(BOX, [r(hours_ago(2), "unknown")], now=NOW)
    assert verdict.code == UNMEASURED


def test_readings_older_than_48_hours_do_not_count() -> None:
    verdict = assess(BOX, [r(hours_ago(49), "inbox")], now=NOW)
    assert verdict.code == UNMEASURED


def test_inbox_at_the_floor_may_send() -> None:
    readings = [r(hours_ago(3), "inbox")] * 7 + [r(hours_ago(3), "spam")] * 3
    verdict = assess(BOX, readings, now=NOW)
    assert verdict.may_send
    assert verdict.reach == 0.7
    assert verdict.measured == 10


def test_under_the_floor_is_refused_with_the_numbers() -> None:
    readings = [
        r(hours_ago(3), "inbox"),
        r(hours_ago(3), "spam"),
        r(hours_ago(3), "spam"),
    ]
    verdict = assess(BOX, readings, now=NOW)
    assert verdict.code == BELOW_FLOOR
    assert "1 of 3" in verdict.detail


def test_promotions_is_not_inbox() -> None:
    readings = [r(hours_ago(3), "promotions")] * 4
    assert assess(BOX, readings, now=NOW).code == BELOW_FLOOR


def test_missing_counts_as_measured_and_bad() -> None:
    readings = [r(hours_ago(3), "missing")] * 2 + [r(hours_ago(3), "inbox")]
    assert assess(BOX, readings, now=NOW).code == BELOW_FLOOR


def test_a_siblings_good_readings_do_not_vouch_for_this_mailbox() -> None:
    readings = [r(hours_ago(3), "inbox", SIBLING)] * 5
    assert assess(BOX, readings, now=NOW).code == UNMEASURED


def test_address_case_does_not_matter() -> None:
    readings = [r(hours_ago(3), "inbox", BOX.upper())]
    assert assess(BOX, readings, now=NOW).may_send


# -------------------------------------------------------------- domain rest
def test_two_consecutive_bad_days_rest_the_whole_domain() -> None:
    readings = [
        r(day(6), "spam", SIBLING),
        r(day(7), "spam", SIBLING),
        # this mailbox is fine today, and still may not send
        r(hours_ago(2), "inbox"),
    ]
    verdict = assess(BOX, readings, now=NOW)
    assert verdict.code == DOMAIN_RESTING
    assert verdict.rest_until == dt.datetime(2026, 10, 22, tzinfo=dt.UTC)


def test_rest_ends_after_fourteen_days() -> None:
    readings = [r(day(6), "spam"), r(day(7), "spam"), r(hours_ago(2), "inbox")]
    later = dt.datetime(2026, 10, 22, 0, 1, tzinfo=dt.UTC)
    readings.append(r(later - dt.timedelta(hours=1), "inbox"))
    assert assess(BOX, readings, now=later).may_send


def test_a_gap_day_breaks_the_run() -> None:
    readings = [r(day(5), "spam"), r(day(7), "spam"), r(hours_ago(2), "inbox")]
    assert rest_until(readings, domain="vuzmalstudio.com") is None
    assert assess(BOX, readings, now=NOW).may_send


def test_one_bad_day_then_a_good_one_does_not_rest() -> None:
    readings = [r(day(8), "spam"), r(day(9), "inbox"), r(hours_ago(2), "inbox")]
    assert assess(BOX, readings, now=NOW).may_send


def test_another_domain_never_rests_this_one() -> None:
    other = "x@arslanvuzmallone.com"
    readings = [
        r(day(6), "spam", other),
        r(day(7), "spam", other),
        r(hours_ago(2), "inbox"),
    ]
    assert assess(BOX, readings, now=NOW).may_send


# -------------------------------------------------------------- send boundary
def _ctx(placement: object | None) -> deliverability.DeliverabilityContext:
    return deliverability.DeliverabilityContext(
        subject="A booking page that closes at five",
        text_body="Hello",
        html_body=None,
        from_name="Arslan",
        mailing_address=None,
        headers={},
        reputation=deliverability.ReputationWindow(
            sent=0, delivered=0, hard_bounced=0, complained=0
        ),
        first_send_at=None,
        sent_today=0,
        now=NOW,
        warmup_target=25,
        placement=placement,
    )


def test_a_failing_verdict_blocks_at_the_send_boundary() -> None:
    report = deliverability.evaluate(_ctx(assess(BOX, [], now=NOW)))
    assert UNMEASURED in {s.code for s in report.blocking}


def test_gate_off_adds_no_signal() -> None:
    report = deliverability.evaluate(_ctx(None))
    assert not {s.code for s in report.signals} & placement_gate.CODES


def test_a_passing_verdict_adds_no_signal() -> None:
    passing = assess(BOX, [r(hours_ago(1), "inbox")], now=NOW)
    report = deliverability.evaluate(_ctx(passing))
    assert not {s.code for s in report.signals} & placement_gate.CODES


def test_check_placement_accepts_any_verdict_shaped_object() -> None:
    fake = SimpleNamespace(may_send=False, code=BELOW_FLOOR, detail="x")
    assert deliverability.check_placement(fake)[0].code == BELOW_FLOOR
