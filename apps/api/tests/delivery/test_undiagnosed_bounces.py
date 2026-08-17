"""A bounce nobody diagnosed is not a soft bounce.

``SOFT`` is a finding: a DSN carrying a 4.x.x code, meaning a real mailbox that
was temporarily unable to accept. Smartlead reports ``is_bounced`` and nothing
else, and recording that as SOFT borrowed the confidence of a diagnosis nobody
made -- then spent it, by granting the address a third attempt.

What it cost, measured: one malformed address (``0606info@207dentalcare.com``,
a phone number scraped into an email) was mailed twice and produced two of the
five bounces behind a 6.2% rate. That rate halved every mailbox's daily volume.
"""

from __future__ import annotations

import inspect

from titan.activities import delivery_events
from titan.delivery.bounces import (
    SOFT_BOUNCES_TO_SUPPRESS,
    UNDIAGNOSED_BOUNCES_TO_SUPPRESS,
    BounceKind,
)


def test_there_is_a_kind_for_not_knowing() -> None:
    """Three states, because there are three: diagnosed permanent, diagnosed
    temporary, and undiagnosed."""
    assert {k.value for k in BounceKind} == {"hard", "soft", "unknown"}


def test_an_undiagnosed_bounce_is_given_fewer_chances() -> None:
    """The reasoning behind three does not survive losing the diagnosis.

    Three is right for a bounce a DSN *told us* was temporary. When the provider
    says only "this bounced", the second identical failure is the strongest
    evidence available.
    """
    assert UNDIAGNOSED_BOUNCES_TO_SUPPRESS == 2
    assert UNDIAGNOSED_BOUNCES_TO_SUPPRESS < SOFT_BOUNCES_TO_SUPPRESS


def test_a_diagnosed_soft_bounce_keeps_its_benefit_of_the_doubt() -> None:
    """A mailbox can plausibly be full twice in a month and belong to somebody
    who wants to hear from us. Nothing about that changed."""
    assert SOFT_BOUNCES_TO_SUPPRESS == 3


def test_the_carrier_poller_no_longer_claims_a_diagnosis() -> None:
    """Smartlead's statistics row carries no bounce code at all."""
    source = inspect.getsource(delivery_events)

    assert "kind=BounceKind.UNKNOWN" in source
    assert "kind=BounceKind.SOFT" not in source


def test_it_still_escalates_rather_than_suppressing_at_once() -> None:
    """Reading every bounce as hard takes the opposite risk: one temporarily
    full mailbox, permanently given up on, indistinguishable from an address
    that never existed."""
    assert UNDIAGNOSED_BOUNCES_TO_SUPPRESS > 1


def test_both_kinds_count_toward_the_same_total() -> None:
    """They are all evidence about the same address. Only how many it takes
    depends on what the provider could tell us about the latest one."""
    import titan.delivery.bounces as bounces

    query = inspect.getsource(bounces._soft_bounce_count)

    assert "'soft', 'unknown'" in query


def test_the_hold_message_says_which_kind_it_is() -> None:
    """It is written to ``status_reason`` and is what an operator reads when
    asking why a lead stopped."""
    source = inspect.getsource(__import__("titan.delivery.bounces", fromlist=["x"]))

    assert "undiagnosed" in source
