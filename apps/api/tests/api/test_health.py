"""The health view: is cold mail moving, and if not, why."""

from __future__ import annotations

import pytest
from titan.api import health
from titan.delivery.placement_gate import UNMEASURED

from tests.api.test_api_security import auth
from tests.api.test_crm import client, crm  # noqa: F401  (fixtures)


@pytest.mark.asyncio
async def test_the_view_answers_for_the_seeded_workspace(client, crm) -> None:  # noqa: F811
    response = await client.get("/api/v1/health", headers=auth(crm["token"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) >= {
        "cold_mail",
        "cold_mail_moving",
        "mailboxes",
        "replies_waiting",
        "seen_7d",
        "grades",
    }
    # The fixture's lead scores 88.
    assert body["grades"]["A"] >= 1


@pytest.mark.asyncio
async def test_it_needs_a_session(client) -> None:  # noqa: F811
    response = await client.get("/api/v1/health")
    assert response.status_code in (401, 403)


def _status(code: str | None) -> health.MailboxStatus:
    return health.MailboxStatus(
        from_email="arslan@arslanvuzmallone.com",
        sending=code is None,
        code=code,
        detail="x",
        reach=None,
        measured=0,
        rest_until=None,
    )


def test_no_test_inboxes_is_named_as_the_reason() -> None:
    sentence, moving = health._cold_mail_sentence(True, 0, [_status(UNMEASURED)])
    assert not moving
    assert "no test inboxes" in sentence
    assert "Replies" in sentence


def test_a_sending_mailbox_is_named() -> None:
    sentence, moving = health._cold_mail_sentence(True, 3, [_status(None)])
    assert moving
    assert "arslan@arslanvuzmallone.com" in sentence


def test_the_gate_off_is_said_plainly() -> None:
    sentence, moving = health._cold_mail_sentence(False, 0, [_status(None)])
    assert moving
    assert "gate is off" in sentence
