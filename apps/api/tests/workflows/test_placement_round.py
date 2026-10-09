"""The placement round, and the pause that makes it worth running.

Placement cannot be read at send time. A message sits in the inbox for a moment
before a filter moves it, so a round that checked immediately would report
almost everything as landing -- and would have called the 26 September probes,
all six of which were in spam, a clean sweep.

These pin the two things the workflow decides: that the wait happens, and that
it is skipped when there is nothing to wait for.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from coldops.workflows.placement import (
    DEFAULT_CRON,
    PlacementRoundWorkflow,
    placement_round_workflow_id,
)
from coldops.workflows.types import PlacementRoundInput, PlacementRoundResult
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

SOURCE = pathlib.Path("coldops/workflows/placement.py").read_text(encoding="utf-8")


def _stubs(sent: PlacementRoundResult, read: PlacementRoundResult):
    calls: list[str] = []

    @activity.defn(name="send_placement_probes")
    async def send(request: PlacementRoundInput) -> PlacementRoundResult:
        calls.append("send")
        return sent

    @activity.defn(name="read_placement_probes")
    async def read_(request: PlacementRoundInput) -> PlacementRoundResult:
        calls.append("read")
        return read

    return [send, read_], calls


async def _run(env: WorkflowEnvironment, client: Client, stubs, request):
    async with Worker(
        client,
        task_queue="placement-test",
        workflows=[PlacementRoundWorkflow],
        activities=stubs,
    ):
        return await client.execute_workflow(
            PlacementRoundWorkflow.run,
            request,
            id=f"placement-test-{id(request)}",
            task_queue="placement-test",
        )


@pytest.mark.asyncio
class TestTheWait:
    async def test_the_round_waits_before_looking(self) -> None:
        """Skipping the wait is the one change that would make every reading
        say inbox while nothing had improved."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            stubs, calls = _stubs(
                PlacementRoundResult(sent=5, recorded=5), PlacementRoundResult(recorded=5)
            )
            request = PlacementRoundInput(workspace_id="w", settle_minutes=60)

            result = await _run(env, env.client, stubs, request)

        assert calls == ["send", "read"]
        assert result.sent == 5
        assert result.recorded == 5

    async def test_both_halves_are_reported_not_just_the_read(self) -> None:
        """Five sent and one found is the shape of a mailbox being silently
        dropped rather than filed. Reporting only the read loses it."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            stubs, _ = _stubs(
                PlacementRoundResult(sent=5, recorded=5),
                PlacementRoundResult(recorded=1, folders=(("inbox", 1),)),
            )

            result = await _run(
                env, env.client, stubs, PlacementRoundInput(workspace_id="w")
            )

        assert (result.sent, result.recorded) == (5, 1)
        assert result.folders == (("inbox", 1),)


@pytest.mark.asyncio
class TestNothingToWaitFor:
    async def test_an_unconfigured_round_returns_without_sleeping(self) -> None:
        """No seeds means no probe to find in an hour. Sleeping anyway would
        leave a deployment looking like a round in progress for an hour before
        telling it that placement was never set up."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            stubs, calls = _stubs(
                PlacementRoundResult(skipped="no seed mailboxes configured"),
                PlacementRoundResult(recorded=99),
            )

            result = await _run(
                env, env.client, stubs, PlacementRoundInput(workspace_id="w")
            )

        assert calls == ["send"], "it went looking for probes it never sent"
        assert result.skipped == "no seed mailboxes configured"
        assert result.recorded == 0


class TestHowItIsScheduled:
    def test_it_runs_before_the_day_s_sending(self) -> None:
        """07:20, after the ramp at 06:10. Probes queue behind nothing, and the
        read lands around 08:20 -- while there is still a day left to act on
        what it says."""
        minute, hour, *_ = DEFAULT_CRON.split()

        assert (int(hour), int(minute)) == (7, 20)

    def test_one_schedule_per_workspace(self) -> None:
        """Two would each send a full round, doubling probe volume against
        every mailbox's cap to produce two readings of the same thing."""
        assert placement_round_workflow_id("a") != placement_round_workflow_id("b")
        assert placement_round_workflow_id("a") == placement_round_workflow_id("a")

    def test_the_settle_default_is_not_zero(self) -> None:
        """A zero default would silently restore the bug the pause exists for."""
        assert PlacementRoundInput(workspace_id="w").settle_minutes >= 30

    def test_a_bad_folder_is_not_retried(self) -> None:
        """The only ValueError either activity raises is a folder outside
        FOLDERS, which is a bug. Retrying would write it three more times."""
        assert 'non_retryable_error_types=["ValueError"]' in SOURCE

    def test_the_sleep_is_durable_rather_than_a_blocking_wait(self) -> None:
        """``workflow.sleep`` releases the worker; ``asyncio.sleep`` would hold
        one for an hour and lose the round on a deploy."""
        assert "workflow.sleep(" in SOURCE
        assert not re.search(r"asyncio\.sleep", SOURCE)
