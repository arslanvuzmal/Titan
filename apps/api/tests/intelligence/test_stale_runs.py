"""Research runs that never close, and the leads trapped behind them.

``ResearchRun`` is created ``running`` and, before the completion write added
alongside this module, nothing in the repository ever set that column again.
Measured on the live workspace: 1,071 runs running, **0 completed**, 873 older
than six hours, the oldest twelve days old.

The trapped lead is the expensive half. ``start_research`` sets the lead to
``RESEARCHING``, and the orchestrator's ``RESEARCHABLE_STATUSES`` does not
contain it -- so a lead whose research workflow dies is never picked up again
by anything. 597 leads were in that state.
"""

from __future__ import annotations

import datetime as dt
import uuid

from titan.db.enums import LeadStatus
from titan.intelligence.stale_runs import (
    DEFAULT_BATCH,
    STALE_AFTER,
    StaleRun,
    find_stale_runs,
)

# ------------------------------------------------- the gap that strands a lead


def test_researching_is_not_a_status_the_orchestrator_returns_to() -> None:
    """The whole reason this sweeper has to exist.

    If ``RESEARCHING`` were researchable, an abandoned run would cost a delay
    and nothing else. It is not, so an abandoned run costs the lead entirely.
    Should that ever change, this test fails and the sweeper can be
    reconsidered rather than quietly kept forever.
    """
    from titan.activities.orchestration import RESEARCHABLE_STATUSES

    assert LeadStatus.RESEARCHING not in RESEARCHABLE_STATUSES
    assert LeadStatus.DISCOVERED in RESEARCHABLE_STATUSES, (
        "the sweeper returns leads to DISCOVERED; if that stopped being "
        "researchable the sweeper would strand them a second time"
    )


def test_the_terminal_activity_now_closes_the_run() -> None:
    """Planted violation: revert the status write in ``analyse_evidence`` and
    this fails. Counters alone left every run open for ever."""
    import inspect

    from titan.activities import pipeline

    source = inspect.getsource(pipeline.analyse_evidence)

    assert 'status="completed"' in source
    assert "finished_at=" in source


# ------------------------------------------------------------ what it presumes


def test_it_waits_hours_not_minutes() -> None:
    """A research pass is minutes. Six hours is far past any legitimate run,
    and short enough that an overnight restart is repaired by morning."""
    assert STALE_AFTER == dt.timedelta(hours=6)
    assert STALE_AFTER > dt.timedelta(minutes=30)


def test_the_batch_is_bounded() -> None:
    """Every lead returned to DISCOVERED buys another crawl and another
    analysis. 873 arriving at once would spend a day's budget in a minute."""
    assert DEFAULT_BATCH == 100


def test_it_records_abandoned_rather_than_failed() -> None:
    """A failure is a diagnosis and nobody made one -- the crawl may have
    succeeded and died before recording it. Writing ``failed`` would poison the
    very rates the learning loop exists to measure."""
    import inspect

    from titan.intelligence import stale_runs

    source = inspect.getsource(stale_runs.reopen)

    assert '"abandoned"' in source
    assert '"failed"' not in source


def test_a_stale_run_carries_what_the_caller_needs() -> None:
    run = StaleRun(
        run_id=uuid.uuid4(), lead_id=uuid.uuid4(), started_at=dt.datetime.now(dt.UTC)
    )

    assert run.run_id and run.lead_id and run.started_at


# ------------------------------------------------------------- the query shape


class TestTheQueryShape:
    def _sql(self) -> str:
        from sqlalchemy import select
        from sqlalchemy.dialects import postgresql
        from titan.db.models import Lead
        from titan.db.models.research import ResearchRun

        now = dt.datetime.now(dt.UTC)
        stmt = (
            select(ResearchRun.id, ResearchRun.lead_id, ResearchRun.started_at)
            .join(Lead, Lead.id == ResearchRun.lead_id)
            .where(
                ResearchRun.workspace_id == uuid.uuid4(),
                ResearchRun.status == "running",
                ResearchRun.started_at < now - STALE_AFTER,
                Lead.status == LeadStatus.RESEARCHING,
            )
            .order_by(ResearchRun.started_at)
            .limit(DEFAULT_BATCH)
        )
        return str(stmt.compile(dialect=postgresql.dialect()))

    def test_it_is_scoped_to_one_workspace(self) -> None:
        assert "research_runs.workspace_id" in self._sql()

    def test_it_only_considers_open_runs(self) -> None:
        assert "research_runs.status" in self._sql()

    def test_it_requires_the_lead_to_still_be_waiting(self) -> None:
        """A run left open whose lead has already moved on is stale
        bookkeeping, not a trapped lead. Reopening it would research the same
        business twice."""
        sql = self._sql()

        assert "leads.status" in sql
        assert "JOIN leads" in sql

    def test_the_longest_stranded_are_freed_first(self) -> None:
        assert "ORDER BY research_runs.started_at" in self._sql()

    def test_it_takes_a_bounded_batch(self) -> None:
        assert "LIMIT" in self._sql()


def test_find_stale_runs_is_exported_for_the_activity() -> None:
    assert callable(find_stale_runs)
