"""The operator's own end-to-end test lifts the paused campaign, and nothing else."""

from __future__ import annotations

from coldops.config import Settings
from coldops.db.enums import CampaignStatus, ContactSource
from coldops.delivery.operator_test import is_operator_test, listed
from coldops.policy.engine import OPERATOR_TEST_EXEMPT, DenyCode, evaluate_send

from tests.policy.test_send_authorization import sendable_context


def codes(decision) -> set[DenyCode]:
    return {d.code for d in decision.denials}


def settings_with(recipients: str) -> Settings:
    return Settings(test_recipients=recipients)


def test_a_paused_campaign_holds_cold_mail_but_not_the_test() -> None:
    cold = evaluate_send(sendable_context(campaign_status=CampaignStatus.PAUSED))
    assert DenyCode.CAMPAIGN_NOT_ACTIVE in codes(cold)

    test = evaluate_send(
        sendable_context(campaign_status=CampaignStatus.PAUSED, is_operator_test=True)
    )
    assert test.allowed, test.denials


def test_suppression_still_stops_the_test() -> None:
    test = evaluate_send(
        sendable_context(
            is_suppressed=True, suppression_reason="unsubscribe", is_operator_test=True
        )
    )
    assert DenyCode.SUPPRESSED in codes(test)


def test_the_exemption_is_only_the_paused_campaign() -> None:
    assert OPERATOR_TEST_EXEMPT == {DenyCode.CAMPAIGN_NOT_ACTIVE}


def test_both_facts_are_needed() -> None:
    settings = settings_with("me@example.com")
    assert is_operator_test(
        settings, recipient="Me@Example.com", source=ContactSource.MANUAL_ENTRY
    )
    # Published on a site: ordinary cold mail, listed or not.
    assert not is_operator_test(
        settings, recipient="me@example.com", source=ContactSource.FIRST_PARTY_WEBSITE
    )
    # Entered by hand but never listed.
    assert not is_operator_test(
        settings, recipient="other@example.com", source=ContactSource.MANUAL_ENTRY
    )


def test_nothing_is_a_test_until_an_inbox_is_listed() -> None:
    settings = Settings()
    assert settings.test_recipients == ()
    assert not listed(settings, "me@example.com")


def test_the_setting_takes_commas_or_json() -> None:
    assert settings_with("a@x.com, b@y.com").test_recipients == ("a@x.com", "b@y.com")
    assert settings_with('["a@x.com"]').test_recipients == ("a@x.com",)
