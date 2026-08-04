"""Deliverability tests.

The framing throughout: these do not prove mail reaches the inbox -- nothing
can. They prove Titan refuses to send mail that carries a known reason to be
filtered, and stops before reputation damage compounds.

DNS is injected, so these run with no network.
"""

from __future__ import annotations

import datetime as dt

import pytest
from titan.delivery import deliverability as d
from titan.delivery.dns_auth import (
    AuthResult,
    check_alignment,
    check_dkim,
    check_dmarc,
    check_spf,
    verify_sender_domain,
)

NOW = dt.datetime(2026, 8, 4, 12, 0, tzinfo=dt.UTC)

GOOD_KEY = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA" + "A" * 350


def resolver_for(records: dict[str, list[str]]):
    def _resolve(name: str) -> list[str]:
        return records.get(name, [])

    return _resolve


HEALTHY_DNS = {
    "mail.arslanvuzmallone.dev": ["v=spf1 include:resend.com ~all"],
    "resend._domainkey.mail.arslanvuzmallone.dev": [f"v=DKIM1; k=rsa; p={GOOD_KEY}"],
    "_dmarc.arslanvuzmallone.dev": [
        "v=DMARC1; p=reject; rua=mailto:dmarc@arslanvuzmallone.dev; pct=100"
    ],
}


# ==========================================================================
# SPF
# ==========================================================================
def test_valid_spf_passes() -> None:
    check = check_spf("mail.arslanvuzmallone.dev", resolver=resolver_for(HEALTHY_DNS))
    assert check.ok


def test_missing_spf_is_reported_with_a_remedy() -> None:
    check = check_spf("nowhere.test", resolver=resolver_for({}))
    assert check.result is AuthResult.MISSING
    assert "v=spf1" in check.detail


def test_two_spf_records_is_a_permanent_error() -> None:
    """Worse than having none: permerror fails authentication outright."""
    check = check_spf(
        "x.test",
        resolver=resolver_for(
            {"x.test": ["v=spf1 include:a.test ~all", "v=spf1 include:b.test ~all"]}
        ),
    )
    assert check.result is AuthResult.MISCONFIGURED
    assert "exactly one" in check.detail


def test_plus_all_is_rejected() -> None:
    check = check_spf(
        "x.test", resolver=resolver_for({"x.test": ["v=spf1 include:a.test +all"]})
    )
    assert check.result is AuthResult.MISCONFIGURED
    assert "entire internet" in check.detail


def test_too_many_spf_lookups_is_rejected() -> None:
    record = "v=spf1 " + " ".join(f"include:h{i}.test" for i in range(12)) + " ~all"
    check = check_spf("x.test", resolver=resolver_for({"x.test": [record]}))
    assert check.result is AuthResult.MISCONFIGURED
    assert "limit of 10" in check.detail


def test_soft_spf_warnings_do_not_block() -> None:
    check = check_spf(
        "x.test", resolver=resolver_for({"x.test": ["v=spf1 include:a.test ?all"]})
    )
    assert check.ok
    assert any("neutral" in w for w in check.warnings)


# ==========================================================================
# DKIM
# ==========================================================================
def test_valid_dkim_passes() -> None:
    check = check_dkim(
        "mail.arslanvuzmallone.dev", ("resend",), resolver=resolver_for(HEALTHY_DNS)
    )
    assert check.ok


def test_missing_dkim_is_reported() -> None:
    check = check_dkim("nowhere.test", ("resend",), resolver=resolver_for({}))
    assert check.result is AuthResult.MISSING


def test_empty_p_value_is_a_revoked_key() -> None:
    check = check_dkim(
        "x.test",
        ("s1",),
        resolver=resolver_for({"s1._domainkey.x.test": ["v=DKIM1; k=rsa; p="]}),
    )
    assert check.result is AuthResult.MISCONFIGURED
    assert "revokes" in check.detail


def test_short_key_and_test_mode_are_warnings() -> None:
    check = check_dkim(
        "x.test",
        ("s1",),
        resolver=resolver_for({"s1._domainkey.x.test": ["v=DKIM1; t=y; p=" + "A" * 200]}),
    )
    assert check.ok
    joined = " ".join(check.warnings)
    assert "1024-bit" in joined
    assert "test mode" in joined


# ==========================================================================
# DMARC
# ==========================================================================
def test_valid_dmarc_passes() -> None:
    check = check_dmarc("arslanvuzmallone.dev", resolver=resolver_for(HEALTHY_DNS))
    assert check.ok
    assert "p=reject" in check.detail


def test_missing_dmarc_names_the_bulk_sender_requirement() -> None:
    check = check_dmarc("nowhere.test", resolver=resolver_for({}))
    assert check.result is AuthResult.MISSING
    assert "Gmail and Yahoo require" in check.detail


def test_p_none_passes_but_warns() -> None:
    check = check_dmarc(
        "x.test", resolver=resolver_for({"_dmarc.x.test": ["v=DMARC1; p=none"]})
    )
    assert check.ok
    joined = " ".join(check.warnings)
    assert "monitors but does not protect" in joined
    assert "rua=" in joined


# ==========================================================================
# Alignment -- the check most setups miss
# ==========================================================================
def test_exact_domain_match_aligns() -> None:
    assert check_alignment(
        from_domain="a.test", sending_domain="a.test", dmarc_record="v=DMARC1; p=reject"
    ).ok


def test_subdomain_aligns_in_relaxed_mode() -> None:
    """The common real setup: From: the apex, signing from a subdomain."""
    check = check_alignment(
        from_domain="arslanvuzmallone.dev",
        sending_domain="mail.arslanvuzmallone.dev",
        dmarc_record="v=DMARC1; p=reject",
    )
    assert check.ok
    assert "relaxed" in check.detail


def test_subdomain_fails_in_strict_mode() -> None:
    """Strict alignment is where a correct-looking setup silently fails."""
    check = check_alignment(
        from_domain="arslanvuzmallone.dev",
        sending_domain="mail.arslanvuzmallone.dev",
        dmarc_record="v=DMARC1; p=reject; adkim=s; aspf=s",
    )
    assert not check.ok
    assert "strict alignment" in check.detail


def test_unrelated_domains_never_align() -> None:
    check = check_alignment(
        from_domain="arslanvuzmallone.dev",
        sending_domain="sendgrid-shared.test",
        dmarc_record="v=DMARC1; p=none",
    )
    assert not check.ok
    assert "DMARC will fail" in check.detail


def test_full_verification_of_a_healthy_domain() -> None:
    report = verify_sender_domain(
        from_email="arslan@arslanvuzmallone.dev",
        sending_domain="mail.arslanvuzmallone.dev",
        dkim_selectors=("resend",),
        resolver=resolver_for(HEALTHY_DNS),
    )
    assert report.ok, report.blocking_errors
    assert report.blocking_errors == []


def test_full_verification_reports_every_failure_at_once() -> None:
    report = verify_sender_domain(
        from_email="arslan@unconfigured.test",
        sending_domain="unconfigured.test",
        resolver=resolver_for({}),
    )
    assert not report.ok
    assert len(report.blocking_errors) >= 3


# ==========================================================================
# Headers
# ==========================================================================
def test_built_headers_include_one_click_unsubscribe() -> None:
    headers = d.build_headers(
        message_id_domain="mail.arslanvuzmallone.dev",
        unsubscribe_url="https://arslanvuzmallone.dev/u/abc",
        unsubscribe_mailto="mailto:unsub@mail.arslanvuzmallone.dev",
        campaign_id="c1",
        now=NOW,
    )
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "https://" in headers["List-Unsubscribe"]
    assert headers["Message-ID"].endswith("@mail.arslanvuzmallone.dev>")
    assert headers["Precedence"] == "bulk"


def test_missing_list_unsubscribe_blocks() -> None:
    signals = d.check_required_headers({"Date": "x"})
    assert any(s.code == "missing_list_unsubscribe" for s in signals)
    assert all(s.severity is d.Severity.BLOCK for s in signals)


def test_unsubscribe_without_one_click_blocks() -> None:
    """The specific omission that drives recipients to 'report spam'."""
    signals = d.check_required_headers({"List-Unsubscribe": "<mailto:unsub@x.test>"})
    assert any(s.code == "missing_one_click_unsubscribe" for s in signals)


# ==========================================================================
# Message construction
# ==========================================================================
GOOD_BODY = (
    "Hi there,\n\n"
    "On example.test the booking button returns a 404, so anyone who clicks it "
    "cannot reach your booking form. That is the step most likely to be used by "
    "someone ready to act. I build booking and follow-up fixes for practices of "
    "this size, and could outline what it would take in about ten minutes.\n\n"
    "Would a short call next week be useful?\n\n"
    "Arslan Vuzmal Lone\n"
    "https://arslanvuzmallone.dev\n"
    "12 Fictional Row, Testville\n"
)


def message_signals(**overrides):
    kwargs = {
        "subject": "A broken button on your booking page",
        "text_body": GOOD_BODY,
        "html_body": None,
        "from_name": "Arslan Vuzmal Lone",
        "mailing_address": "12 Fictional Row, Testville",
    }
    kwargs.update(overrides)
    return d.check_message(**kwargs)


def test_a_well_formed_message_produces_no_blocking_signal() -> None:
    blocking = [s for s in message_signals() if s.severity is d.Severity.BLOCK]
    assert blocking == [], [s.detail for s in blocking]


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"subject": ""}, "empty_subject"),
        ({"subject": "URGENT ACTION REQUIRED NOW"}, "shouting_subject"),
        ({"subject": "Re: our chat"}, "fake_reply_subject"),
        ({"text_body": GOOD_BODY + "\nAct now, limited time!"}, "spam_phrase"),
        ({"text_body": GOOD_BODY + "\nhttps://bit.ly/x"}, "url_shortener"),
        ({"mailing_address": None}, "missing_postal_address"),
        ({"mailing_address": "Somewhere else entirely"}, "address_not_in_body"),
    ],
)
def test_known_spam_signals_block(overrides, code) -> None:
    signals = message_signals(**overrides)
    blocking = {s.code for s in signals if s.severity is d.Severity.BLOCK}
    assert code in blocking, [s.detail for s in signals]


def test_html_without_plain_text_blocks() -> None:
    signals = d.check_message(
        subject="Hello",
        text_body="",
        html_body="<p>Only HTML here</p>",
        from_name="A",
        mailing_address="12 Row",
    )
    codes = {s.code for s in signals}
    assert "html_only" in codes or "empty_text_body" in codes


def test_image_only_message_blocks() -> None:
    signals = d.check_message(
        subject="Look",
        text_body=GOOD_BODY,
        html_body='<img src="a.png"><img src="b.png">',
        from_name="A",
        mailing_address="12 Fictional Row, Testville",
    )
    assert "image_heavy" in {s.code for s in signals}


def test_too_many_links_blocks() -> None:
    body = GOOD_BODY + "\n" + "\n".join(f"https://x{i}.test" for i in range(8))
    assert "too_many_links" in {s.code for s in message_signals(text_body=body)}


# ==========================================================================
# Reputation
# ==========================================================================
def test_small_samples_produce_no_signal() -> None:
    """One complaint in ten sends is 10% and means nothing."""
    window = d.ReputationWindow(sent=10, delivered=10, hard_bounced=0, complained=1)
    assert d.check_reputation(window) == []


def test_complaint_rate_above_threshold_blocks() -> None:
    window = d.ReputationWindow(
        sent=1000, delivered=1000, hard_bounced=0, complained=5
    )  # 0.5%
    codes = {s.code for s in d.check_reputation(window)}
    assert "complaint_rate_exceeded" in codes


def test_complaint_threshold_is_stricter_than_gmails_ceiling() -> None:
    """0.3% is where Gmail acts; damage starts earlier, so Titan stops earlier."""
    assert d.COMPLAINT_RATE_PAUSE < 0.003


def test_high_bounce_rate_blocks() -> None:
    window = d.ReputationWindow(
        sent=1000, delivered=950, hard_bounced=30, complained=0
    )  # 3%
    codes = {s.code for s in d.check_reputation(window)}
    assert "bounce_rate_exceeded" in codes


def test_healthy_reputation_produces_nothing() -> None:
    window = d.ReputationWindow(sent=1000, delivered=980, hard_bounced=5, complained=0)
    assert d.check_reputation(window) == []


# ==========================================================================
# Warm-up
# ==========================================================================
def test_a_brand_new_domain_is_limited_to_the_first_day() -> None:
    assert d.warmup_limit(first_send_at=None, now=NOW) == d.WARMUP_SCHEDULE[0]


def test_warmup_limit_rises_each_day() -> None:
    start = NOW - dt.timedelta(days=5)
    assert d.warmup_limit(first_send_at=start, now=NOW) == d.WARMUP_SCHEDULE[5]
    assert d.WARMUP_SCHEDULE[5] > d.WARMUP_SCHEDULE[0]


def test_warmup_ends_after_the_schedule() -> None:
    start = NOW - dt.timedelta(days=d.WARMUP_DAYS + 1)
    assert d.warmup_limit(first_send_at=start, now=NOW) is None


def test_exceeding_the_daily_warmup_limit_blocks() -> None:
    signals = d.check_warmup(first_send_at=None, sent_today=20, now=NOW)
    assert signals and signals[0].code == "warmup_limit_reached"


def test_within_the_warmup_limit_is_allowed() -> None:
    assert d.check_warmup(first_send_at=None, sent_today=5, now=NOW) == []


# ==========================================================================
# Combined
# ==========================================================================
def healthy_context(**overrides) -> d.DeliverabilityContext:
    kwargs = {
        "subject": "A broken button on your booking page",
        "text_body": GOOD_BODY,
        "html_body": None,
        "from_name": "Arslan Vuzmal Lone",
        "mailing_address": "12 Fictional Row, Testville",
        "headers": {
            "List-Unsubscribe": "<https://arslanvuzmallone.dev/u/a>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
        "reputation": d.ReputationWindow(
            sent=500, delivered=490, hard_bounced=2, complained=0
        ),
        "first_send_at": NOW - dt.timedelta(days=60),
        "sent_today": 10,
        "now": NOW,
        "auth_errors": (),
    }
    kwargs.update(overrides)
    return d.DeliverabilityContext(**kwargs)


def test_a_healthy_message_passes_every_check() -> None:
    report = d.evaluate(healthy_context())
    assert report.ok, [s.detail for s in report.blocking]


def test_authentication_failures_block_delivery() -> None:
    report = d.evaluate(
        healthy_context(auth_errors=("DMARC: no DMARC record on _dmarc.x.test",))
    )
    assert not report.ok
    assert any(s.code == "authentication_failed" for s in report.blocking)


def test_the_report_serialises_for_storage() -> None:
    payload = d.evaluate(healthy_context(subject="")).to_json()
    assert payload["ok"] is False
    assert any(s["code"] == "empty_subject" for s in payload["signals"])


def test_every_blocking_signal_carries_a_remedy_or_a_clear_detail() -> None:
    """An operator who is blocked must be able to act on the message."""
    report = d.evaluate(
        healthy_context(
            subject="ACT NOW LIMITED TIME",
            mailing_address=None,
            headers={},
        )
    )
    assert not report.ok
    for signal in report.blocking:
        assert signal.detail
        assert signal.remedy or len(signal.detail) > 20
