"""The settings recovered from the deployed environment.

The deployed service sets seven TITAN_* variables this repository's Settings
model did not define. Because `extra="ignore"`, they were read straight past:
a deploy of this tree would have run with Smartlead silently disabled and no
error anywhere.

These tests pin the two properties that matter about the recovery -- that the
names are now readable, and that they arrived switched off.
"""

from __future__ import annotations

import pytest
from titan.config import Settings

RECOVERED = [
    "owner_title",
    "owner_years_experience",
    "smartlead_import_enabled",
    "smartlead_production_enabled",
    "smartlead_sandbox_campaign_id",
    "smartlead_test_recipients",
    "smartlead_webhook_secret",
]


@pytest.mark.parametrize("name", RECOVERED)
def test_the_deployed_environment_is_readable(name: str) -> None:
    """Each recovered variable maps to a real field.

    `extra="ignore"` is the right default -- a stray variable should not stop a
    service booting -- but it also means a *missing* field is invisible. The
    only defence is asserting the field exists.
    """
    assert name in Settings.model_fields


def test_recovering_a_switch_does_not_arrive_with_it_on() -> None:
    """Defaults are the safe end, not the value production happens to hold.

    The deployed environment sets smartlead_production_enabled=true. Copying
    that into the default would mean any process started without an explicit
    environment -- a test runner, a one-off script, a new worker -- routes to
    the live campaign instead of the sandbox.
    """
    settings = Settings()
    assert settings.smartlead_production_enabled is False
    assert settings.smartlead_import_enabled is False
    assert settings.smartlead_sandbox_campaign_id is None
    assert settings.smartlead_test_recipients == ()
    assert settings.smartlead_webhook_secret is None


def test_a_webhook_secret_is_not_exposed_by_repr() -> None:
    """It gates whether a callback may mark a lead replied; a leaked one lets
    anyone stop a sequence."""
    settings = Settings(smartlead_webhook_secret="not-the-real-secret")
    assert "not-the-real-secret" not in repr(settings)
    assert settings.smartlead_webhook_secret is not None
    assert settings.smartlead_webhook_secret.get_secret_value() == "not-the-real-secret"


def test_test_recipients_parses_the_deployed_comma_separated_form() -> None:
    """The deployed service sets this comma-separated.

    pydantic-settings parses a tuple field from the environment as JSON, so
    without the before-validator the real deployed value raises on boot. This
    is the difference between the recovered environment being readable and
    being loadable, and it would have surfaced as a crash on first deploy
    rather than as anything findable by reading the code.
    """
    settings = Settings(
        smartlead_test_recipients="arslan@example.com,outreach@example.com"
    )
    assert settings.smartlead_test_recipients == (
        "arslan@example.com",
        "outreach@example.com",
    )


def test_test_recipients_still_accepts_a_json_array() -> None:
    """The documented pydantic-settings form must keep working."""
    settings = Settings(smartlead_test_recipients='["a@example.com"]')
    assert settings.smartlead_test_recipients == ("a@example.com",)


def test_the_real_deployed_environment_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load Settings the way a container does: from the environment.

    This is the case that actually broke. pydantic-settings decodes a complex
    field by calling json.loads inside the env source, *before* validators run,
    so the comma-separated value the deployed service sets raised SettingsError
    on startup and no field validator could intervene. Passing the same string
    as an init kwarg does not reproduce it -- only the environment path does,
    which is why this test sets real environment variables.
    """
    monkeypatch.setenv(
        "TITAN_SMARTLEAD_TEST_RECIPIENTS",
        "arslan@arslanvuzmallone.com,outreach@arslanvuzmallone.com",
    )
    monkeypatch.setenv("TITAN_SMARTLEAD_PRODUCTION_ENABLED", "true")
    monkeypatch.setenv("TITAN_SMARTLEAD_SANDBOX_CAMPAIGN_ID", "3770055")

    settings = Settings()

    assert settings.smartlead_test_recipients == (
        "arslan@arslanvuzmallone.com",
        "outreach@arslanvuzmallone.com",
    )
    assert settings.smartlead_production_enabled is True
    assert settings.smartlead_sandbox_campaign_id == 3770055


def test_blank_and_padded_entries_are_dropped() -> None:
    """A trailing comma is the most likely way this gets typed, and an empty
    string would otherwise become an allowed recipient that matches nothing."""
    settings = Settings(smartlead_test_recipients=" a@example.com , , b@example.com,")
    assert settings.smartlead_test_recipients == ("a@example.com", "b@example.com")
