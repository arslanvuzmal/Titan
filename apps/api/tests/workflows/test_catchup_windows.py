"""How far back a missed occurrence stays worth running.

Frequency decides it: a job running several times a day is covered by its own
next run, and a daily job is not. That distinction was already found and fixed
once -- ``titan-mailbox-ramp`` recorded ``Total: 4, MissedCatchupWindow: 6``,
having missed more often than it ran, because one thirty-minute window applied
to everything and silently disabled every daily job on a machine that sleeps.

These tests pin the derivation itself, which had none of its own. The stricter
property they sit beside -- that no job may reach back past its own previous
occurrence -- is held in ``test_schedules.py`` and is the reason a longer window
for repair jobs was tried here and abandoned: housekeeping's next hourly run
sweeps the backlog anyway, so reaching further back bought almost nothing and
cost the guarantee against a thundering herd.
"""

from __future__ import annotations

import pytest
from titan.workflows.schedules import (
    CATCHUP_WINDOW,
    DAILY_CATCHUP_WINDOW,
    catchup_for,
)


class TestDerivedFromCron:
    @pytest.mark.parametrize("cron", ["25 * * * *", "*/15 * * * *", "17 * * * *"])
    def test_sub_daily_jobs_get_the_short_window(self, cron: str) -> None:
        assert catchup_for(cron) == CATCHUP_WINDOW

    @pytest.mark.parametrize("cron", ["10 6 * * *", "40 5 * * *", "0 8 * * 1"])
    def test_daily_jobs_get_the_long_window(self, cron: str) -> None:
        assert catchup_for(cron) == DAILY_CATCHUP_WINDOW
