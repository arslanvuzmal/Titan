"""Finding a probe in a seed mailbox, and the answer that would mislead.

The failure this guards against is not a crash. It is a confident "inbox" for
a message sitting in Gmail's Promotions tab -- technically delivered, never
read, and the exact place cold outreach goes to die. A folder walk cannot see
the difference, because Promotions is a label on an INBOX message rather than
a mailbox of its own.
"""

from __future__ import annotations

import pytest
from coldops.delivery.folder_search import (
    GMAIL_PROMOTIONS,
    find_probe_blocking,
)
from coldops.delivery.mailbox import ImapConfig

TOKEN = "tkn-abc123"
CONFIG = ImapConfig(host="imap.gmail.com", username="seed@gmail.com", password="x")


class FakeImap:
    """An IMAP server that holds one message in one place.

    ``where`` is the folder it lives in; ``promoted`` marks it as carrying
    Gmail's promotions category, which is independent of the folder because
    that is exactly how Gmail models it.
    """

    def __init__(
        self, where: str | None, *, promoted: bool = False, absent: tuple[str, ...] = ()
    ):
        self.where = where
        self.promoted = promoted
        self.absent = absent
        self.selected: str | None = None
        self.searches: list[tuple[str, str]] = []
        self.readonly_calls: list[bool] = []
        self.logged_out = False

    def select(self, folder, readonly=False):
        self.readonly_calls.append(readonly)
        if folder in self.absent:
            return "NO", [b"nonexistent"]
        self.selected = folder
        return "OK", [b"1"]

    def search(self, charset, criteria):
        self.searches.append((self.selected or "", criteria))
        if self.selected != self.where:
            return "OK", [b""]
        if GMAIL_PROMOTIONS in criteria and not self.promoted:
            return "OK", [b""]
        if TOKEN not in criteria:
            return "OK", [b""]
        return "OK", [b"7"]

    def logout(self):
        self.logged_out = True

    def close(self):
        pass


@pytest.fixture
def connect(monkeypatch):
    """Swap the real socket for a fake, keeping everything else real."""

    def _install(server: FakeImap) -> FakeImap:
        monkeypatch.setattr(
            "coldops.delivery.folder_search._ImapConnection.open", lambda self: server
        )
        return server

    return _install


class TestTheAnswerThatWouldMislead:
    def test_a_promoted_message_is_not_reported_as_inbox(self, connect) -> None:
        """The whole reason this module asks Gmail a second question."""
        connect(FakeImap("INBOX", promoted=True))

        verdict = find_probe_blocking(CONFIG, "gmail", TOKEN)

        assert verdict.folder == "promotions"
        assert "category:promotions" in verdict.found_in

    def test_an_ordinary_inbox_message_is_still_inbox(self, connect) -> None:
        """The promotions question must not answer yes for everything."""
        connect(FakeImap("INBOX", promoted=False))

        assert find_probe_blocking(CONFIG, "gmail", TOKEN).folder == "inbox"

    def test_the_promotions_question_is_only_asked_of_gmail(self, connect) -> None:
        """``X-GM-RAW`` is a Gmail extension. Sending it to Outlook is either
        an error or, worse, silently ignored and answered for INBOX."""
        server = connect(FakeImap("INBOX"))

        find_probe_blocking(CONFIG, "outlook", TOKEN)

        assert not any(GMAIL_PROMOTIONS in c for _, c in server.searches)


class TestWhereItLooks:
    def test_spam_is_checked_before_inbox(self, connect) -> None:
        """A false "inbox" stops you acting; a false "spam" only makes you
        look again. So the order resolves ties toward looking again."""
        server = connect(FakeImap("[Gmail]/Spam"))

        verdict = find_probe_blocking(CONFIG, "gmail", TOKEN)

        assert verdict.folder == "spam"
        assert server.searches[-1][0] == "[Gmail]/Spam"

    def test_outlook_junk_is_found(self, connect) -> None:
        connect(FakeImap("Junk Email"))

        assert find_probe_blocking(CONFIG, "outlook", TOKEN).folder == "spam"

    def test_an_unknown_provider_still_gets_searched(self, connect) -> None:
        """A seed at a small host is the population we most want measured."""
        connect(FakeImap("INBOX"))

        assert find_probe_blocking(CONFIG, "other", TOKEN).folder == "inbox"

    def test_a_folder_that_does_not_exist_does_not_abort_the_round(self, connect) -> None:
        """Folder names vary by locale and provider. One renamed mailbox must
        not turn into a day with no measurement at all."""
        connect(FakeImap("INBOX", absent=("Junk Email", "Junk")))

        assert find_probe_blocking(CONFIG, "outlook", TOKEN).folder == "inbox"


class TestNotFinding:
    def test_a_probe_nowhere_to_be_found_is_missing(self, connect) -> None:
        """Accepted by the server, then filed where nobody will look. A real
        answer, and a bad one."""
        connect(FakeImap(None))

        assert find_probe_blocking(CONFIG, "gmail", TOKEN).folder == "missing"

    def test_missing_carries_what_was_searched(self, connect) -> None:
        """ "Not found" is only trustworthy if the list is long, so the list
        travels with the verdict instead of being assumed."""
        connect(FakeImap(None))

        verdict = find_probe_blocking(CONFIG, "gmail", TOKEN)

        assert "INBOX" in verdict.searched
        assert "[Gmail]/Spam" in verdict.searched


class TestItLeavesTheMailboxAlone:
    def test_every_select_is_readonly(self, connect) -> None:
        """A probe left unread in spam is evidence that survives to the next
        run. A probe this code marked seen is a reading nobody can check."""
        server = connect(FakeImap("INBOX"))

        find_probe_blocking(CONFIG, "gmail", TOKEN)

        assert server.readonly_calls and all(server.readonly_calls)

    def test_the_connection_is_closed_even_when_nothing_is_found(self, connect) -> None:
        server = connect(FakeImap(None))

        find_probe_blocking(CONFIG, "gmail", TOKEN)

        assert server.logged_out
