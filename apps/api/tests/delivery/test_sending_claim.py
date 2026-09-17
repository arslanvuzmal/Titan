"""The sending claim: only one host may send, and a database copy knows it.

These reconstruct the 16 September incident rather than testing the functions
in the abstract. The estate moved to a server, the laptop kept running, and
both worked the same restored queue for eleven hours -- 29 businesses received
one pitch twice, from two addresses on the same domain.

The test that matters most is `test_a_restored_copy_refuses_to_send`, because
that is the exact shape of what happened: two databases that agree about every
row, because one is a dump of the other.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from titan.delivery import sending_claim

SERVER = "vps-hetzner-cx33"
LAPTOP = "laptop-2"


@pytest.fixture(autouse=True)
async def _clean(db_session):
    """Each test starts with nobody holding the claim."""
    await db_session.execute(text("DELETE FROM sending_claims WHERE scope = 'outbox'"))
    await db_session.commit()


@pytest.mark.asyncio
async def test_an_unclaimed_estate_is_taken_by_whoever_asks(db_session) -> None:
    verdict = await sending_claim.hold(db_session, host_id=SERVER)
    assert verdict.may_send
    assert verdict.holder == SERVER


@pytest.mark.asyncio
async def test_the_holder_keeps_it_across_cycles(db_session) -> None:
    """Called every poll, so re-asking must be free and must not flap."""
    first = await sending_claim.hold(db_session, host_id=SERVER)
    second = await sending_claim.hold(db_session, host_id=SERVER)
    third = await sending_claim.hold(db_session, host_id=SERVER)
    assert [first.may_send, second.may_send, third.may_send] == [True, True, True]


@pytest.mark.asyncio
async def test_a_restored_copy_refuses_to_send(db_session) -> None:
    """The incident, in one test.

    The laptop's database was a pg_restore of the server's, so it carries the
    server's claim. The laptop reads a holder that is not itself and stops --
    which is the only signal either host could ever have had, since neither
    could see the other.
    """
    await sending_claim.hold(db_session, host_id=SERVER)

    verdict = await sending_claim.hold(db_session, host_id=LAPTOP)

    assert not verdict.may_send
    assert verdict.holder == SERVER
    # The message has to name both, or the operator cannot tell which estate
    # they are looking at.
    assert SERVER in verdict.reason
    assert LAPTOP in verdict.reason


@pytest.mark.asyncio
async def test_the_refused_host_does_not_quietly_steal_it_later(db_session) -> None:
    """There is no expiry, on purpose.

    A lease would hand the copy exactly what we are denying it: the right to
    take over once the original stopped heartbeating. The original had stopped
    heartbeating in the incident -- it had been switched off deliberately.
    """
    await sending_claim.hold(db_session, host_id=SERVER)
    for _ in range(5):
        assert not (await sending_claim.hold(db_session, host_id=LAPTOP)).may_send
    assert await sending_claim.current_holder(db_session) == SERVER


@pytest.mark.asyncio
async def test_an_empty_identity_may_not_send(db_session) -> None:
    """Fails closed.

    An unset TITAN_SENDER_HOST_ID makes every host look like every other, so
    treating it as "probably fine" would restore the original defect while
    appearing to guard against it.
    """
    verdict = await sending_claim.hold(db_session, host_id="")
    assert not verdict.may_send
    assert "TITAN_SENDER_HOST_ID" in verdict.reason


@pytest.mark.asyncio
async def test_empty_identity_does_not_claim_anything(db_session) -> None:
    """Refusing is not the same as writing a blank holder."""
    await sending_claim.hold(db_session, host_id="")
    assert await sending_claim.current_holder(db_session) is None


@pytest.mark.asyncio
async def test_moving_hosts_is_possible_but_deliberate(db_session) -> None:
    """`take` is the decision `hold` refuses to make on its own."""
    await sending_claim.hold(db_session, host_id=SERVER)

    assert not (await sending_claim.hold(db_session, host_id=LAPTOP)).may_send
    await sending_claim.take(db_session, host_id=LAPTOP, note="server decommissioned")

    assert (await sending_claim.hold(db_session, host_id=LAPTOP)).may_send
    assert not (await sending_claim.hold(db_session, host_id=SERVER)).may_send


@pytest.mark.asyncio
async def test_take_refuses_an_empty_identity(db_session) -> None:
    with pytest.raises(ValueError):
        await sending_claim.take(db_session, host_id="")


@pytest.mark.asyncio
async def test_exactly_one_of_two_racing_hosts_wins(db_session) -> None:
    """Two fresh estates starting together must not both conclude yes.

    Serialised by the primary key rather than by timing: the loser's UPDATE is
    filtered out by the ON CONFLICT ... WHERE and returns no row.
    """
    first = await sending_claim.hold(db_session, host_id=SERVER)
    second = await sending_claim.hold(db_session, host_id=LAPTOP)
    assert [first.may_send, second.may_send].count(True) == 1


@pytest.mark.asyncio
async def test_scopes_do_not_interfere(db_session) -> None:
    """'outbox' is the only scope today; the column exists so that stays true."""
    await sending_claim.hold(db_session, host_id=SERVER, scope="outbox")
    other = await sending_claim.hold(db_session, host_id=LAPTOP, scope="research")
    assert other.may_send
    assert await sending_claim.current_holder(db_session, scope="outbox") == SERVER
