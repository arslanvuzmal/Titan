"""Passcode hashing.

Pure functions, no database. The route-level behaviour they support -- lockout,
indistinguishable failures -- is covered in test_api_security.py.
"""

from __future__ import annotations

import pytest
from titan.api.passwords import (
    PasscodeRejected,
    check_strength,
    hash_passcode,
    verify_passcode,
)


def test_a_hash_verifies_against_its_own_passcode() -> None:
    digest = hash_passcode("a-real-passcode")
    assert verify_passcode(digest, "a-real-passcode").ok


def test_the_wrong_passcode_does_not_verify() -> None:
    digest = hash_passcode("a-real-passcode")
    assert not verify_passcode(digest, "a-real-passcod").ok
    assert not verify_passcode(digest, "").ok


def test_the_same_passcode_hashes_differently_each_time() -> None:
    """Distinct salts. Equal hashes would make the store a rainbow table and
    reveal which accounts share a passcode."""
    assert hash_passcode("shared") != hash_passcode("shared")


def test_a_null_hash_never_verifies() -> None:
    """The state of every account that predates passcodes.

    If this returned ok, enabling local login would have granted a session to
    anyone who could guess a username -- exactly the hole the environment gate
    was compensating for.
    """
    for attempt in ("", "anything", "titan-os::no-such-account::not-a-passcode"):
        assert not verify_passcode(None, attempt).ok


def test_a_corrupt_hash_is_a_failure_not_a_crash() -> None:
    """A truncated column value must reject the login, not 500 it."""
    assert not verify_passcode("$argon2id$not-a-real-hash", "anything").ok
    assert not verify_passcode("", "anything").ok


def test_a_current_hash_does_not_ask_to_be_rehashed() -> None:
    result = verify_passcode(hash_passcode("current"), "current")
    assert result.ok
    assert result.needs_rehash is False


def test_a_weaker_hash_asks_to_be_rehashed() -> None:
    """Cost parameters rise over time; login is the one moment the plaintext
    is available to re-hash with the current ones."""
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("old")
    result = verify_passcode(weak, "old")
    assert result.ok
    assert result.needs_rehash is True


@pytest.mark.parametrize("raw", ["", "12345", "short"])
def test_short_passcodes_are_refused(raw: str) -> None:
    with pytest.raises(PasscodeRejected):
        check_strength(raw, minimum_length=6)


@pytest.mark.parametrize("raw", [" 637206", "637206 ", "\t637206"])
def test_surrounding_whitespace_is_refused(raw: str) -> None:
    with pytest.raises(PasscodeRejected):
        check_strength(raw, minimum_length=6)


def test_a_six_digit_passcode_is_accepted() -> None:
    """Only defensible because login_max_attempts makes online guessing
    impractical. Stated here so the tradeoff is visible in the test suite and
    not only in a docstring."""
    check_strength("637206", minimum_length=6)
