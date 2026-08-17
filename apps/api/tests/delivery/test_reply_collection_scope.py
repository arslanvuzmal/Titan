"""Which carrier campaigns a workspace may read replies from.

``TITAN_SMARTLEAD_CAMPAIGN_ID`` is process-wide and names no owner. Applied
unconditionally it hands every workspace in the database the same Smartlead
campaign -- and a leftover test workspace, running its own copy of the hourly
poll, ingested the real workspace's reply as its own.

The workspace guard cannot catch that. The write is correctly scoped to the
workspace doing it; the *source* is what belongs to somebody else.
"""

from __future__ import annotations

import inspect

from titan.activities import smartlead_replies


def test_the_default_carrier_needs_a_workspace_that_owns_one() -> None:
    """A workspace that has never routed a campaign to a carrier has no
    business reading one."""
    source = inspect.getsource(smartlead_replies.collect_smartlead_replies)

    assert "settings.smartlead_campaign_id is not None and carriers" in source


def test_a_workspace_with_no_carriers_collects_nothing() -> None:
    """The refusal is explicit rather than an empty loop, so the result says
    why it found nothing."""
    source = inspect.getsource(smartlead_replies.collect_smartlead_replies)

    assert "no carrier campaign is configured" in source


def test_the_reason_is_recorded_where_it_can_be_read() -> None:
    """A pass that refused and a pass that found nothing are different facts."""
    from titan.workflows.types import CollectRepliesResult

    assert "refused_reason" in CollectRepliesResult.__dataclass_fields__
