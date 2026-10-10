"""Is this message the operator's own end-to-end test?

``coldops e2e`` sends one real message through the whole pipeline -- research,
draft, a human approval, the outbox, the carrier -- to an inbox the operator
owns, so that seen, replied and classified can be watched happening on the
dashboard rather than inferred.

Two facts make a message that test, and both are required:

* the recipient is listed in ``COLDOPS_TEST_RECIPIENTS`` -- set on the server,
  by a person, never by the pipeline; and
* the address was entered by hand (``manual_entry``). Discovery never writes
  that source, so a business that happens to publish a listed address on its
  own site is still ordinary cold mail.

What the test is excused, and nothing more: the cold-mail placement gate (a
domain resting from spam readings would otherwise hold the one message that
measures it), and the test campaign being paused (it is paused so the campaign
loop never discovers or follows up from it). Approval, suppression, sender
authentication, unsubscribe, validation and the kill switch all still hold.
"""

from __future__ import annotations

from coldops.config import Settings
from coldops.db.enums import ContactSource


def listed(settings: Settings, email: str | None) -> bool:
    """Whether the operator named this address as one of their own."""
    if not email:
        return False
    wanted = email.strip().lower()
    return any(wanted == r.strip().lower() for r in settings.test_recipients)


def is_operator_test(
    settings: Settings, *, recipient: str | None, source: ContactSource | str | None
) -> bool:
    """Both conditions, so neither alone can claim the exemptions."""
    return str(source) == ContactSource.MANUAL_ENTRY.value and listed(settings, recipient)


__all__ = ["is_operator_test", "listed"]
