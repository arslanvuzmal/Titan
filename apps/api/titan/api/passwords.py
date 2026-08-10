"""Passcode hashing for ``auth_mode="local"``.

The pre-existing local login took an email address and a workspace slug and no
secret at all, so it had to be refused in every deployed environment -- the
environment check *was* the authentication. This module supplies the missing
half, which lets the route be gated on knowing a secret instead of on where the
process happens to be running.

argon2id, with two properties the call site depends on:

* **An unknown account and a wrong passcode cost the same.** ``verify`` is run
  against a fixed dummy hash when there is no stored hash, so response timing
  does not separate "no such user" from "wrong passcode". Returning the same
  message is not enough on its own when one branch skips a 50ms KDF.
* **A null hash never authenticates.** A user row with ``password_hash IS NULL``
  -- every row that existed before this change -- is unusable for login rather
  than passwordless. There is no configuration in which the absence of a
  credential is treated as possession of one.
"""

from __future__ import annotations

from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

#: argon2-cffi's own defaults (RFC 9106's second recommended profile: 64 MiB,
#: t=3). Raising these later is safe -- `needs_rehash` re-hashes on next login.
_HASHER = PasswordHasher()

#: Verified against when no stored hash exists. The value is not a fallback
#: credential: nothing can produce it, because nothing knows it.
_ABSENT_ACCOUNT_HASH = _HASHER.hash("titan-os::no-such-account::not-a-passcode")


class PasscodeRejected(ValueError):
    """The proposed passcode does not meet the configured minimum."""


@dataclass(frozen=True, slots=True)
class VerifyResult:
    ok: bool
    #: True when the stored hash used weaker parameters than are current. The
    #: caller re-hashes and stores, so cost keeps up with hardware without ever
    #: asking the operator to change their passcode.
    needs_rehash: bool = False


def hash_passcode(raw: str) -> str:
    return _HASHER.hash(raw)


def verify_passcode(stored: str | None, raw: str) -> VerifyResult:
    """Check ``raw`` against ``stored``. Never raises on a bad passcode."""
    candidate = stored if stored is not None else _ABSENT_ACCOUNT_HASH
    try:
        _HASHER.verify(candidate, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return VerifyResult(ok=False)

    if stored is None:
        # Only reachable if someone learns the placeholder, which is a source
        # constant rather than a secret -- so this branch must still fail.
        return VerifyResult(ok=False)

    try:
        return VerifyResult(ok=True, needs_rehash=_HASHER.check_needs_rehash(stored))
    except InvalidHashError:  # pragma: no cover - verify already accepted it
        return VerifyResult(ok=True)


def check_strength(raw: str, *, minimum_length: int) -> None:
    """Raise ``PasscodeRejected`` if the passcode is below the configured floor.

    Deliberately only a length floor. Composition rules ("one symbol, one
    digit") shrink the search space an attacker has to cover and push people
    toward predictable substitutions; length and a working lockout do the real
    work here. A short all-numeric passcode is only defensible *because*
    ``login_max_attempts`` makes online guessing impractical -- it would not
    survive an offline attack on a leaked hash.
    """
    if len(raw) < minimum_length:
        raise PasscodeRejected(
            f"passcode must be at least {minimum_length} characters; got {len(raw)}"
        )
    if raw != raw.strip():
        raise PasscodeRejected(
            "passcode has leading or trailing whitespace, which is easy to "
            "type differently next time"
        )


__all__ = [
    "PasscodeRejected",
    "VerifyResult",
    "check_strength",
    "hash_passcode",
    "verify_passcode",
]
