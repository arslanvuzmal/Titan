"""Telling the operator about a reply *now*, not in tomorrow's report.

A reply used to write a ``tasks`` row and stop. The other channel,
``push_notification``, is a chat webhook that has never had a URL configured,
so in practice a person who answered was surfaced in the next daily report --
up to a day later. This is the path that closes that gap.

Pure unit tests: the durable record is covered by
``test_reply_notifications.py`` against a real database, and what matters here
is which notifications escalate to a human's inbox and which stay quiet.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from titan.notify import operator as op
from titan.notify.operator import (
    MAILED_INSTANTLY,
    NotificationKind,
    OperatorNotification,
    mail_notification,
)

pytestmark = pytest.mark.asyncio

NOW = dt.datetime(2026, 9, 10, 9, 14, 22, tzinfo=dt.UTC)


def notification(kind: NotificationKind, **overrides) -> OperatorNotification:
    base = {
        "task_id": uuid.uuid4(),
        "workspace_id": uuid.uuid4(),
        "kind": kind,
        "title": "Harborline Legal replied and wants a call",
        "description": "Yes, please -- can you do Thursday afternoon?",
        "lead_id": uuid.uuid4(),
        "priority": 90,
        "created_at": NOW,
    }
    base.update(overrides)
    return OperatorNotification(**base)


class _Mailer:
    """Stands in for the one path allowed to mail without the outbox."""

    def __init__(self, fails: bool = False) -> None:
        self.sent: list[dict[str, str]] = []
        self.fails = fails

    async def __call__(self, *, subject: str, body: str) -> str:
        if self.fails:
            raise OSError("smtp unreachable")
        self.sent.append({"subject": subject, "body": body})
        return "operator@example.test"


@pytest.fixture
def mailer(monkeypatch) -> _Mailer:
    sent = _Mailer()
    monkeypatch.setattr("titan.notify.operator_mail.mail_the_operator", sent)
    return sent


# ------------------------------------------------------- a person answered
@pytest.mark.parametrize(
    "kind",
    [
        NotificationKind.CLIENT_AGREED,
        NotificationKind.REPLY_NEEDS_READING,
        NotificationKind.REPLY_DECLINED,
    ],
)
async def test_a_human_reply_reaches_the_inbox(kind, mailer) -> None:
    """Planted violation: record the task and stop.

    All three are a person who wrote back. Being told promptly that somebody
    said no is worth as much as being told they said yes -- it is the
    difference between closing a thread and wondering about it.
    """
    assert await mail_notification(notification(kind)) is True
    assert len(mailer.sent) == 1


async def test_the_mail_carries_the_reply_itself(mailer) -> None:
    """An alert that only says "you have a reply" is a prompt to go and look.

    The webhook deliberately withholds the body, because it copies a
    prospect's words into a third-party system nobody told them about. This
    goes to the operator's own mailbox, where the reply already arrived.
    """
    await mail_notification(notification(NotificationKind.CLIENT_AGREED))

    body = mailer.sent[0]["body"]
    assert "can you do Thursday afternoon?" in body
    assert "Harborline Legal replied and wants a call" in body
    assert "2026-09-10 09:14 UTC" in body


async def test_the_subject_says_it_is_a_reply(mailer) -> None:
    """It arrives beside the daily report and everything else. The first word
    has to distinguish it."""
    await mail_notification(notification(NotificationKind.CLIENT_AGREED))

    assert mailer.sent[0]["subject"].startswith("Reply: ")


# ------------------------------------------------------ a machine did not
@pytest.mark.parametrize(
    "kind",
    [
        NotificationKind.DELIVERABILITY_ALERT,
        NotificationKind.CAMPAIGN_STALLED,
        NotificationKind.PIPELINE_ALERT,
        NotificationKind.APPROVAL_NEEDED,
        NotificationKind.CONVERSATION_ACTIVE,
    ],
)
async def test_an_operational_alarm_stays_out_of_the_inbox(kind, mailer) -> None:
    """Planted violation: mail every notification.

    None of these is a person. They belong to the daily report, and a channel
    that fires for machines is a channel that gets filtered -- which would cost
    the one alert that mattered.
    """
    assert await mail_notification(notification(kind)) is False
    assert mailer.sent == []


async def test_nothing_recorded_means_nothing_sent(mailer) -> None:
    """``record_notification`` returns None when the dedupe key already
    existed, so a folder re-read or a provider retry produces no second mail.
    The de-duplication is the insert's doing; this only has to respect it."""
    assert await mail_notification(None) is False
    assert mailer.sent == []


# ---------------------------------------------------------------- failure
async def test_an_unreachable_mail_server_never_fails_the_ingest(monkeypatch) -> None:
    """Planted violation: let the exception out.

    This runs after the commit. The reply is already recorded and the task row
    is the durable guarantee; an SMTP problem must not turn a successful ingest
    into a failed poll that retries the whole batch.
    """
    monkeypatch.setattr("titan.notify.operator_mail.mail_the_operator", _Mailer(fails=True))

    assert await mail_notification(notification(NotificationKind.CLIENT_AGREED)) is False


async def test_a_reply_with_no_body_still_alerts(mailer) -> None:
    """The body is the useful part, not the trigger. A reply we could not read
    is still a person waiting."""
    await mail_notification(
        notification(NotificationKind.CLIENT_AGREED, description=None)
    )

    assert len(mailer.sent) == 1
    assert "no body was captured" in mailer.sent[0]["body"]


async def test_every_mailed_kind_is_a_human_reply() -> None:
    """The set is the policy. Guarded so a later notification kind cannot be
    added to it without somebody deciding that it should wake a person."""
    assert MAILED_INSTANTLY == {
        NotificationKind.CLIENT_AGREED,
        NotificationKind.REPLY_NEEDS_READING,
        NotificationKind.REPLY_DECLINED,
    }
    assert op.NotificationKind.WEEKLY_REPORT not in MAILED_INSTANTLY
