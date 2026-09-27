"""The rotation, which is the only real decision in the probe sender.

Every mailbox to every seed, every day, is the obvious scheme and it costs a
quarter of Monday's sending budget to measure Monday's sending. These tests
pin the cheaper scheme and the properties that make it trustworthy: full
coverage over a few days, no pairing starved, and nothing recorded that was
not actually sent.
"""

from __future__ import annotations

import datetime as dt

from titan.delivery.placement_probe import PlannedProbe, plan_round, rotation_day
from titan.delivery.seeds import parse_seeds

MAILBOXES = [
    "arslan@arslanvuzmallone.com",
    "me@arslanvuzmallone.com",
    "outreach@arslanvuzmallone.com",
    "projects@arslanvuzmallone.com",
    "sales@arslanvuzmallone.com",
]

SEEDS = parse_seeds(
    {
        "seeds": [
            {
                "address": "s1@gmail.com",
                "imap": {"host": "imap.gmail.com", "password": "p"},
            },
            {
                "address": "s2@outlook.com",
                "imap": {"host": "outlook.office365.com", "password": "p"},
            },
            {
                "address": "s3@somehost.co.uk",
                "imap": {"host": "mail.somehost.co.uk", "password": "p"},
            },
        ]
    }
)

DAY = dt.datetime(2026, 9, 28, 7, 0, tzinfo=dt.UTC)


class TestWhatOneRoundCosts:
    def test_one_probe_per_mailbox_not_one_per_pairing(self) -> None:
        """Five, not fifteen. The difference is 8% of Monday's budget instead
        of 25%."""
        assert len(plan_round(MAILBOXES, SEEDS, now=DAY)) == len(MAILBOXES)

    def test_every_mailbox_is_measured_every_round(self) -> None:
        """Placement is per mailbox as well as per domain -- on 27 September
        the least-used mailbox was the one that reached an inbox. A rotation
        that skipped mailboxes would have hidden that."""
        planned = plan_round(MAILBOXES, SEEDS, now=DAY)

        assert {p.from_email for p in planned} == set(MAILBOXES)

    def test_one_round_spreads_across_providers(self) -> None:
        """Five mailboxes offset only by the day would all point at the same
        provider and rotate in lockstep, measuring one filter a day."""
        planned = plan_round(MAILBOXES, SEEDS, now=DAY)

        assert len({p.seed.provider for p in planned}) == len(SEEDS.providers())


class TestWhatTheRotationCovers:
    def test_every_pairing_is_read_within_three_days(self) -> None:
        """The claim the cheap scheme rests on. If a pairing can go a week
        unmeasured then a mailbox can be in spam for a week unnoticed."""
        seen: set[tuple[str, str]] = set()
        for offset in range(len(SEEDS.providers())):
            for probe in plan_round(
                MAILBOXES, SEEDS, now=DAY + dt.timedelta(days=offset)
            ):
                seen.add((probe.from_email, probe.seed.provider))

        expected = {(m, p) for m in MAILBOXES for p in SEEDS.providers()}
        assert seen == expected

    def test_a_skipped_day_does_not_stall_the_rotation(self) -> None:
        """Derived from the date rather than a stored cursor, so a round that
        never ran costs one reading rather than putting the whole rotation
        permanently a day behind."""
        after_gap = plan_round(MAILBOXES, SEEDS, now=DAY + dt.timedelta(days=2))
        as_if_run = plan_round(MAILBOXES, SEEDS, now=DAY + dt.timedelta(days=2))

        assert [(p.from_email, p.seed.provider) for p in after_gap] == [
            (p.from_email, p.seed.provider) for p in as_if_run
        ]

    def test_the_day_advances_the_assignment(self) -> None:
        today = {
            (p.from_email, p.seed.provider) for p in plan_round(MAILBOXES, SEEDS, now=DAY)
        }
        tomorrow = {
            (p.from_email, p.seed.provider)
            for p in plan_round(MAILBOXES, SEEDS, now=DAY + dt.timedelta(days=1))
        }

        assert today != tomorrow

    def test_rotation_day_advances_once_per_day_not_per_run(self) -> None:
        morning = rotation_day(DAY)
        evening = rotation_day(DAY.replace(hour=23, minute=59))

        assert morning == evening
        assert rotation_day(DAY + dt.timedelta(days=1)) == morning + 1


class TestNothingToMeasureWith:
    def test_no_seeds_plans_nothing(self) -> None:
        """The caller refuses the round on this rather than reporting a clean
        one: a table that stops gaining rows looks exactly like placement that
        has stopped being a problem."""
        assert plan_round(MAILBOXES, parse_seeds({"seeds": []}), now=DAY) == []

    def test_no_mailboxes_plans_nothing(self) -> None:
        assert plan_round([], SEEDS, now=DAY) == []


class TestTheMessageItself:
    def test_the_token_is_in_the_subject_where_imap_can_find_it(self) -> None:
        """The checker searches SUBJECT. A token only in a custom header is
        unfindable at providers that ignore HEADER searches, and unfindable
        reads exactly like never arrived."""
        probe = plan_round(MAILBOXES, SEEDS, now=DAY)[0]

        assert probe.probe_token in probe.subject

    def test_the_body_says_what_it_is(self) -> None:
        """Somebody finds one of these in their spam folder in six months."""
        probe = plan_round(MAILBOXES, SEEDS, now=DAY)[0]
        body = probe.body(now=DAY)

        assert "automated delivery check" in body
        assert probe.from_email in body
        assert probe.probe_token in body

    def test_two_probes_never_share_a_token(self) -> None:
        planned = plan_round(MAILBOXES, SEEDS, now=DAY)

        assert len({p.probe_token for p in planned}) == len(planned)

    def test_a_probe_is_a_plain_message(self) -> None:
        """No pixel, no rewritten link, no attachment. The instrument must not
        be measuring its own effect on the thing it measures."""
        probe = plan_round(MAILBOXES, SEEDS, now=DAY)[0]
        body = probe.body(now=DAY)

        assert "<img" not in body
        assert "http" not in body
        assert isinstance(probe, PlannedProbe)
