"""The seed file, and the two mistakes it exists to make impossible.

A seed is an address ColdOps owns at a provider it wants measured. The file is
separate from ``mailboxes.json`` for one reason worth testing: a seed that
finds its way into the sender pool is cold outreach going out from somebody's
personal Gmail.
"""

from __future__ import annotations

import json

import pytest
from coldops.delivery.seeds import SeedConfigError, load_seeds, parse_seeds

GMAIL = {
    "address": "seed@gmail.com",
    "imap": {"host": "imap.gmail.com", "password": "app-password"},
}
OUTLOOK = {
    "address": "seed@outlook.com",
    "imap": {"host": "outlook.office365.com", "password": "app-password"},
}


class TestWhatTheFileSays:
    def test_a_seed_is_read_back_with_its_credential(self) -> None:
        registry = parse_seeds({"seeds": [GMAIL]})

        seed = registry.all()[0]
        assert seed.address == "seed@gmail.com"
        assert seed.imap.host == "imap.gmail.com"
        assert seed.imap.port == 993, "the IMAP default, not restated per entry"

    def test_the_provider_is_derived_and_not_declared(self) -> None:
        """A file that could claim a gmail.com address measures Outlook is a
        file that reports the wrong filter's verdict indefinitely."""
        registry = parse_seeds({"seeds": [dict(GMAIL, provider="outlook"), OUTLOOK]})

        assert {s.address: s.provider for s in registry.all()} == {
            "seed@gmail.com": "gmail",
            "seed@outlook.com": "outlook",
        }

    def test_providers_come_back_in_a_stable_order(self) -> None:
        """The probe rotation indexes into this. An order that varies between
        runs would re-probe one provider and skip another, silently."""
        one = parse_seeds({"seeds": [GMAIL, OUTLOOK]}).providers()
        other = parse_seeds({"seeds": [OUTLOOK, GMAIL]}).providers()

        assert one == other == ["gmail", "outlook"]

    def test_a_disabled_seed_keeps_its_credential_and_leaves_the_rotation(self) -> None:
        registry = parse_seeds({"seeds": [dict(GMAIL, enabled=False), OUTLOOK]})

        assert [s.address for s in registry.all()] == ["seed@outlook.com"]


class TestWhatTheFileRefuses:
    def test_sending_credentials_in_a_seed_are_refused(self) -> None:
        """The whole reason this is a separate file."""
        with pytest.raises(SeedConfigError, match="never sent from"):
            parse_seeds({"seeds": [dict(GMAIL, smtp={"host": "x", "password": "y"})]})

    def test_a_seed_nobody_can_read_is_refused(self) -> None:
        """An address with no IMAP measures nothing, and would sit in the
        rotation consuming a probe a day to produce no reading."""
        with pytest.raises(SeedConfigError, match="measures nothing"):
            parse_seeds({"seeds": [{"address": "seed@gmail.com"}]})

    def test_cleartext_imap_is_refused_even_to_a_local_server(self) -> None:
        """``_endpoint`` already refuses cleartext to a remote host. It permits
        it to a loopback capture server, which is right for sending and wrong
        here: this connection reads the mail as well as authenticating, and
        there is no local-capture case for reading a real Gmail account.
        """
        bad = {
            "address": "s@gmail.com",
            "imap": {"host": "localhost", "password": "p", "security": "none"},
        }

        with pytest.raises(SeedConfigError, match="cleartext"):
            parse_seeds({"seeds": [bad]})

    def test_the_same_address_twice_is_refused(self) -> None:
        """Two entries means two probes a round to one mailbox, and a rotation
        that thinks it covers more providers than it does."""
        with pytest.raises(SeedConfigError, match="listed twice"):
            parse_seeds({"seeds": [GMAIL, dict(GMAIL)]})

    def test_one_bad_entry_refuses_the_whole_file(self) -> None:
        """All-or-nothing, like the mailbox file: a silently dropped seed is a
        provider that has quietly stopped being measured, which looks exactly
        like a provider that is behaving."""
        with pytest.raises(SeedConfigError):
            parse_seeds({"seeds": [GMAIL, {"address": "not-an-address"}]})

    def test_a_file_without_a_seeds_list_names_what_it_found(self) -> None:
        with pytest.raises(SeedConfigError, match="got dict"):
            parse_seeds({"seeds": {"address": "s@gmail.com"}})


class TestLoading:
    def test_no_path_is_an_empty_registry_rather_than_an_error(self) -> None:
        """Placement measurement is an addition. A deployment without it sends
        exactly as it did before."""
        assert len(load_seeds(None)) == 0

    def test_a_missing_file_is_an_error(self) -> None:
        """Unlike an unset path. Naming a file that is not there is a
        deployment that believes it is measuring and is not."""
        with pytest.raises(SeedConfigError, match="does not exist"):
            load_seeds("/nowhere/seeds.json")

    def test_the_file_round_trips(self, tmp_path) -> None:
        path = tmp_path / "seeds.json"
        path.write_text(json.dumps({"seeds": [GMAIL, OUTLOOK]}), encoding="utf-8")

        registry = load_seeds(path)

        assert len(registry) == 2
        assert registry.for_provider("gmail")[0].address == "seed@gmail.com"

    def test_nothing_redacted_carries_a_password(self) -> None:
        seed = parse_seeds({"seeds": [GMAIL]}).all()[0]

        assert "app-password" not in json.dumps(seed.redacted())
        assert seed.redacted()["imap"]["password"] == "set"
