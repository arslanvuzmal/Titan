"""A lead rests after being researched, instead of being re-picked for ever.

The planner orders candidates by score then age, which is deterministic, and
finishing a research run changes nothing that ordering reads. A high-scoring
lead whose site publishes no address therefore ends `no_eligible_contact`, is
set back to RESEARCHED -- a RESEARCHABLE_STATUS -- and sorts straight back to
the top of the next cycle. Nothing else can get past it.

Measured on 26 September before the fix: 1,978 research runs in 24 hours
across 136 distinct leads, each worked 19 to 24 times in a day, while the
estate sent 24 messages against a ceiling of 100.
"""

from __future__ import annotations

import datetime as dt
import inspect

from titan.activities import orchestration
from titan.db.enums import LeadStatus


def test_a_cooldown_exists_and_is_under_a_day() -> None:
    """Under 24h so a cycle that drifts later cannot cost a lead its turn."""
    assert orchestration.RESEARCH_COOLDOWN < dt.timedelta(hours=24)
    assert orchestration.RESEARCH_COOLDOWN >= dt.timedelta(hours=12)


def test_the_planner_filters_on_recent_runs() -> None:
    """Structural: the candidate query must exclude recently-researched leads."""
    src = inspect.getsource(orchestration)
    block = src.split("candidates = (")[1][:1200]
    assert "RESEARCH_COOLDOWN" in block
    assert "ResearchRun.lead_id == Lead.id" in block
    assert ".exists()" in block


def test_researched_is_still_researchable() -> None:
    """The cooldown must not be implemented by removing the status.

    RESEARCHED has to stay researchable -- that is how a lead gets re-measured
    when its evidence ages past 21 days. The fix is a rest period, not an exit.
    """
    assert LeadStatus.RESEARCHED in orchestration.RESEARCHABLE_STATUSES
    assert LeadStatus.QUALIFIED in orchestration.RESEARCHABLE_STATUSES


def test_the_cooldown_is_shorter_than_the_staleness_window() -> None:
    """It rations re-picking within a day; it must not block re-measurement.

    Evidence is re-measured at 21 days. A cooldown anywhere near that would
    turn a throughput guard into a silent block on the freshness cycle.
    """
    assert orchestration.RESEARCH_COOLDOWN < dt.timedelta(days=21)
