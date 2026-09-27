"""The report, and the three ways a placement number lies.

Every one of these encodes a way that counting probes can produce a sentence
that is true and useless: silence rendered as success, Promotions rendered as
inbox, and one domain-wide average hiding the one mailbox worth stopping.
"""

from __future__ import annotations

from titan.delivery.placement_report import Placement, placements, render, worst_first


def p(**kw) -> Placement:
    base = dict(
        from_email="me@arslanvuzmallone.com",
        provider="gmail",
        probes=0,
        inbox=0,
        promotions=0,
        spam=0,
        missing=0,
        unchecked=0,
    )
    return Placement(**{**base, **kw})


class TestSilenceIsNotSuccess:
    def test_an_unchecked_probe_has_no_reach_rather_than_zero(self) -> None:
        """Zero is a finding -- everything was junked. "Nobody looked" must
        never render as that finding."""
        assert p(probes=3, unchecked=3).reach is None

    def test_unchecked_probes_are_out_of_the_denominator(self) -> None:
        """Two checked, both inbox, one never looked at. That is 100% of what
        was measured, not 67% of what was sent."""
        assert p(probes=3, inbox=2, unchecked=1).reach == 1.0

    def test_the_verdict_for_nothing_measured_says_so(self) -> None:
        assert p(probes=2, unchecked=2).verdict == "not measured"

    def test_an_empty_report_does_not_read_as_good_news(self) -> None:
        text = render([], days=14)

        assert "Nothing here is evidence that placement is fine" in text

    def test_unchecked_probes_are_called_out_below_the_table(self) -> None:
        text = render([p(probes=4, inbox=2, unchecked=2)], days=14)

        assert "never looked at" in text


class TestPromotionsIsNotInbox:
    def test_a_promoted_probe_does_not_count_as_seen(self) -> None:
        """Delivered, not spam, and in the folder cold outreach goes to be
        ignored. Every tool that treats "not spam" as "inbox" gets this wrong."""
        assert p(probes=2, inbox=0, promotions=2).reach == 0.0

    def test_the_report_names_the_promotions_tab(self) -> None:
        text = render([p(probes=2, promotions=2)], days=14)

        assert "Promotions" in text
        assert "Not counted as seen" in text


class TestTheAverageThatHidesTheAnswer:
    def test_each_mailbox_keeps_its_own_verdict(self) -> None:
        """On 27 September five mailboxes shared one domain and one of them
        reached an inbox. A domain average would have said "partly working"
        and left the actionable question unanswered."""
        rows = [
            p(from_email="arslan@x.com", probes=3, inbox=3),
            p(from_email="outreach@x.com", probes=3, spam=3),
        ]

        assert [x.verdict for x in rows] == ["landing", "filtered"]

    def test_the_worst_pairing_is_read_first(self) -> None:
        good = p(from_email="arslan@x.com", probes=3, inbox=3)
        bad = p(from_email="outreach@x.com", probes=3, spam=3)

        assert worst_first([good, bad])[0] is bad

    def test_an_unmeasured_pairing_outranks_even_total_failure(self) -> None:
        """A mailbox nobody probed is a worse position than one known to be in
        spam: one of the two you can decide about."""
        failing = p(from_email="a@x.com", probes=3, spam=3)
        unknown = p(from_email="b@x.com", probes=3, unchecked=3)

        assert worst_first([failing, unknown])[0] is unknown

    def test_the_same_mailbox_can_differ_by_provider(self) -> None:
        rows = [
            p(provider="gmail", probes=2, spam=2),
            p(provider="other", probes=2, inbox=2),
        ]

        assert {r.provider: r.verdict for r in rows} == {
            "gmail": "filtered",
            "other": "landing",
        }


class TestVerdicts:
    def test_everything_landing_is_landing(self) -> None:
        assert p(probes=4, inbox=4).verdict == "landing"

    def test_half_and_half_is_patchy(self) -> None:
        assert p(probes=4, inbox=2, spam=2).verdict == "patchy"

    def test_spam_with_no_inbox_is_filtered(self) -> None:
        assert p(probes=4, spam=4).verdict == "filtered"

    def test_accepted_and_vanished_is_not_landing_rather_than_filtered(self) -> None:
        """ "missing" is its own failure: accepted by the server and filed
        where nobody will look. Calling it filtered would point the fix at
        spam rules that are not involved."""
        assert p(probes=4, missing=4).verdict == "not landing"


class TestReadingTheRows:
    def test_database_rows_become_placements(self) -> None:
        rows = [
            {
                "from_email": "me@x.com",
                "provider": "gmail",
                "probes": 3,
                "inbox": 1,
                "promotions": 1,
                "spam": 1,
                "missing": 0,
                "unchecked": 0,
                "last_probe": None,
            }
        ]

        [placement] = placements(rows)

        assert placement.measured == 3
        # One of three reaching the inbox, with a probe in spam. Not "patchy":
        # a third getting through while another third is junked is a mailbox
        # being filtered, and the verdict should read as the problem it is.
        assert placement.verdict == "filtered"

    def test_a_null_count_reads_as_zero(self) -> None:
        """Postgres returns NULL for an empty aggregate; a None in an int
        field would fail arithmetic in the renderer rather than here."""
        rows = [
            {
                "from_email": "me@x.com",
                "provider": "gmail",
                "probes": 1,
                "inbox": None,
                "promotions": None,
                "spam": None,
                "missing": None,
                "unchecked": None,
            }
        ]

        assert placements(rows)[0].inbox == 0
