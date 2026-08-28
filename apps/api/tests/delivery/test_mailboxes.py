"""The per-mailbox credential file.

Hermetic: nothing opens a socket, and no real credential appears anywhere in
this file. What is being tested is the refusal behaviour -- a mailbox file is
the one place in Titan where a quiet, partial success would put mail on the
wire under the wrong authentication.
"""

from __future__ import annotations

import json

import pytest
from titan.delivery.mailboxes import (
    MailboxConfigError,
    load_mailboxes,
    parse_mailboxes,
)


def document(**overrides) -> dict:
    mailbox = {
        "from_email": "outreach@arslanvuzmallone.com",
        "smtp": {
            "host": "smtp.example.test",
            "port": 465,
            "security": "ssl",
            "username": "outreach@arslanvuzmallone.com",
            "password": "not-a-real-password",
        },
        "imap": {
            "host": "imap.example.test",
            "port": 993,
            "security": "ssl",
            "username": "outreach@arslanvuzmallone.com",
            "password": "not-a-real-password",
        },
    }
    mailbox.update(overrides)
    return {"mailboxes": [mailbox]}


# ------------------------------------------------------------------ parsing
def test_a_mailbox_is_reachable_by_the_address_it_sends_as() -> None:
    """The lookup key is the From address, because that is what the pool picks."""
    registry = parse_mailboxes(document())

    account = registry.get("outreach@arslanvuzmallone.com")
    assert account is not None
    assert account.smtp.host == "smtp.example.test"
    assert account.imap is not None


def test_the_lookup_is_case_insensitive() -> None:
    """Addresses arrive from the database in whatever case they were stored in."""
    registry = parse_mailboxes(document())

    assert registry.get("Outreach@ArslanVuzmalLone.com") is not None


def test_a_plus_tag_is_a_different_mailbox() -> None:
    """An SMTP server treats them as different accounts, so this must too.

    Folding them together would authenticate one address as another, which is
    the whole failure this module exists to prevent.
    """
    registry = parse_mailboxes(document())

    assert registry.get("outreach+leads@arslanvuzmallone.com") is None


def test_a_mailbox_without_imap_is_allowed_but_reported() -> None:
    """Sending is possible; nobody reading the replies is worth knowing about."""
    registry = parse_mailboxes(document(imap=None))

    assert len(registry) == 1
    assert registry.readable() == []


def test_a_disabled_mailbox_keeps_its_credential_and_leaves_the_pool() -> None:
    """Better than deleting the block and having to retype a password."""
    registry = parse_mailboxes(document(enabled=False))

    assert len(registry) == 0


# ------------------------------------------------------------------ refusal
def test_the_template_placeholder_is_refused() -> None:
    """A file that was created and never filled in fails here, not at a provider."""
    body = document()
    body["mailboxes"][0]["smtp"]["password"] = "PASTE_APP_PASSWORD_HERE"

    with pytest.raises(MailboxConfigError, match="placeholder"):
        parse_mailboxes(body)


def test_cleartext_smtp_is_refused_to_anywhere_but_a_capture_server() -> None:
    """'none' exists for Mailpit. Anywhere else it puts a password on the wire."""
    body = document()
    body["mailboxes"][0]["smtp"]["security"] = "none"

    with pytest.raises(MailboxConfigError, match="clear text"):
        parse_mailboxes(body)


def test_cleartext_smtp_is_allowed_to_the_local_capture_server() -> None:
    body = document()
    body["mailboxes"][0]["smtp"] = {
        "host": "mailpit",
        "port": 1025,
        "security": "none",
        "username": "outreach@arslanvuzmallone.com",
        "password": "not-a-real-password",
    }
    body["mailboxes"][0]["imap"] = None

    assert len(parse_mailboxes(body)) == 1


def test_cleartext_imap_is_refused_everywhere() -> None:
    """There is no local-capture equivalent for *reading* mail.

    Cleartext here would put the mailbox password and the full text of every
    reply on the wire in the clear.
    """
    body = document()
    body["mailboxes"][0]["imap"]["security"] = "none"
    body["mailboxes"][0]["imap"]["host"] = "mailpit"

    with pytest.raises(MailboxConfigError, match="never accepted"):
        parse_mailboxes(body)


def test_one_address_may_only_have_one_credential() -> None:
    body = document()
    body["mailboxes"].append(dict(body["mailboxes"][0]))

    with pytest.raises(MailboxConfigError, match="twice"):
        parse_mailboxes(body)


def test_a_missing_password_is_refused_rather_than_sent_without_one() -> None:
    body = document()
    del body["mailboxes"][0]["smtp"]["password"]

    with pytest.raises(MailboxConfigError, match="required"):
        parse_mailboxes(body)


def test_one_bad_mailbox_refuses_the_whole_file() -> None:
    """A pool that silently shrank looks exactly like a pool that is working."""
    body = document()
    good = json.loads(json.dumps(body["mailboxes"][0]))
    good["from_email"] = "sales@arslanvuzmallone.com"
    body["mailboxes"][0]["smtp"]["password"] = "changeme"
    body["mailboxes"].append(good)

    with pytest.raises(MailboxConfigError):
        parse_mailboxes(body)


# ------------------------------------------------------------------ loading
def test_no_file_configured_is_an_empty_registry_not_an_error() -> None:
    """Whether that is fatal depends on the provider, so the caller decides."""
    assert len(load_mailboxes(None)) == 0


def test_a_missing_file_is_an_error() -> None:
    """Unlike an unset path: a path that was set and points nowhere is a typo."""
    with pytest.raises(MailboxConfigError, match="does not exist"):
        load_mailboxes("/nonexistent/mailboxes.json")


def test_malformed_json_says_so(tmp_path) -> None:
    path = tmp_path / "mailboxes.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(MailboxConfigError, match="not valid JSON"):
        load_mailboxes(path)


def test_a_loaded_file_round_trips(tmp_path) -> None:
    path = tmp_path / "mailboxes.json"
    path.write_text(json.dumps(document()), encoding="utf-8")

    registry = load_mailboxes(path)

    assert registry.addresses() == ["outreach@arslanvuzmallone.com"]


# ---------------------------------------------------------------- redaction
def test_nothing_that_renders_a_mailbox_renders_its_password() -> None:
    """The only rendering that exists says whether a password is set."""
    registry = parse_mailboxes(document())

    rendered = json.dumps(registry.redacted())

    assert "not-a-real-password" not in rendered
    assert '"password": "set"' in rendered
