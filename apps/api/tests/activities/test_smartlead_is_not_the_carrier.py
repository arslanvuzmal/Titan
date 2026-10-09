"""With ColdOps's own SMTP pool carrying the mail, nothing calls Smartlead.

The ramp, the delivery-event poll and the reply collector all read from
Smartlead. Since the pool replaced it on 24 August each of them failed every
run with an authentication error -- a dead key still in the environment --
which read like a broken ramp rather than a carrier no longer in use.
"""

from __future__ import annotations

import uuid

import pytest
from coldops.activities import delivery_events, mailbox_ramp, smartlead_replies
from coldops.config import get_settings
from coldops.workflows.types import (
    CollectRepliesInput,
    PollDeliveryEventsInput,
    RampMailboxesInput,
)


@pytest.fixture
def own_pool(monkeypatch):
    monkeypatch.setenv("COLDOPS_EMAIL_PROVIDER", "smtp_pool")
    monkeypatch.setenv("COLDOPS_SMARTLEAD_API_KEY", "a-dead-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _explode(*_a, **_k):
    raise AssertionError("Smartlead was called")


async def test_the_ramp_stands_down(own_pool, monkeypatch):
    monkeypatch.setattr(
        "coldops.providers.smartlead.SmartleadClient.from_settings", _explode
    )
    result = await mailbox_ramp.ramp_mailboxes(
        RampMailboxesInput(workspace_id=str(uuid.uuid4()))
    )
    assert "not the carrier" in (result.unavailable or "")


async def test_the_delivery_poll_stands_down(own_pool, monkeypatch):
    monkeypatch.setattr(
        "coldops.providers.smartlead.SmartleadClient.from_settings", _explode
    )
    result = await delivery_events.poll_delivery_events(
        PollDeliveryEventsInput(workspace_id=str(uuid.uuid4()))
    )
    assert "not the carrier" in (result.unavailable or "")


async def test_the_reply_collector_stands_down(own_pool, monkeypatch):
    monkeypatch.setattr(
        "coldops.providers.smartlead.SmartleadClient.from_settings", _explode
    )
    result = await smartlead_replies.collect_smartlead_replies(
        CollectRepliesInput(workspace_id=str(uuid.uuid4()))
    )
    assert "not the carrier" in (result.refused_reason or "")
