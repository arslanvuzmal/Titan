"""Grading what fetched a message: a person, Apple's proxy, or a scanner."""

from __future__ import annotations

import datetime as dt

from titan.delivery import engagement as e

LATE = dt.timedelta(hours=3)
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15"
GMAIL = "Mozilla/5.0 (Windows NT 5.1; rv:11.0) Gecko Firefox/11.0 (via ggpht.com GoogleImageProxy)"
OUTLOOK = "Microsoft Office/16.0 (Windows NT 10.0; Microsoft Outlook 16.0.17928; Pro)"
CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/129.0 Safari/537.36"


def test_apple_proxy_is_delivery_not_reading() -> None:
    g = e.grade(e.OPEN, user_agent=IPHONE, client_ip="17.58.101.7", since_send=LATE)
    assert g.grade == e.DELIVERED
    assert not g.seen


def test_gmail_proxy_is_likely_seen() -> None:
    g = e.grade(e.OPEN, user_agent=GMAIL, client_ip="66.249.84.1", since_send=LATE)
    assert g.grade == e.LIKELY
    assert g.seen


def test_a_mail_client_after_the_scanner_window_is_likely() -> None:
    g = e.grade(e.OPEN, user_agent=OUTLOOK, client_ip="81.2.69.1", since_send=LATE)
    assert g.grade == e.LIKELY


def test_a_fetch_seconds_after_sending_is_a_scanner() -> None:
    g = e.grade(
        e.OPEN,
        user_agent=OUTLOOK,
        client_ip="81.2.69.1",
        since_send=dt.timedelta(seconds=8),
    )
    assert g.grade == e.MACHINE
    assert "scanner window" in g.reason


def test_named_gateways_and_scripts_are_machines() -> None:
    for ua in (
        "Mimecast-Url-Protect",
        "python-requests/2.32",
        "curl/8.4",
        "Barracuda Sentinel",
    ):
        assert (
            e.grade(e.VISIT, user_agent=ua, client_ip=None, since_send=LATE).grade
            == e.MACHINE
        )


def test_no_user_agent_is_a_machine() -> None:
    assert (
        e.grade(e.VISIT, user_agent="", client_ip=None, since_send=LATE).grade
        == e.MACHINE
    )


def test_a_browser_visit_is_likely_and_the_beacon_confirms_it() -> None:
    visit = e.grade(e.VISIT, user_agent=CHROME, client_ip="81.2.69.1", since_send=LATE)
    assert visit.grade == e.LIKELY
    beacon = e.grade(
        e.VISIT_CONFIRMED,
        user_agent=CHROME,
        client_ip=None,
        since_send=None,
        dwell_seconds=5,
    )
    assert beacon.grade == e.CONFIRMED


def test_a_beacon_without_the_stay_is_not_confirmation() -> None:
    beacon = e.grade(
        e.VISIT_CONFIRMED,
        user_agent=CHROME,
        client_ip=None,
        since_send=None,
        dwell_seconds=1,
    )
    assert beacon.grade == e.MACHINE


def test_apple_rule_applies_to_opens_only() -> None:
    """A visit from Apple's network is somebody on an Apple device clicking."""
    g = e.grade(e.VISIT, user_agent=CHROME, client_ip="17.1.2.3", since_send=LATE)
    assert g.grade == e.LIKELY


def test_unknown_send_time_does_not_count_as_fast() -> None:
    assert (
        e.grade(e.OPEN, user_agent=OUTLOOK, client_ip=None, since_send=None).grade
        == e.LIKELY
    )


def test_client_ip_prefers_our_proxys_header() -> None:
    headers = {"x-real-ip": "81.2.69.1", "x-forwarded-for": "10.0.0.1, 81.2.69.1"}
    assert e.client_ip_of(headers, "172.18.0.5") == "81.2.69.1"
    assert e.client_ip_of({}, "172.18.0.5") == "172.18.0.5"
