"""The schedule for the shadow round: every model in shadow reads what has arrived.

Every fifteen minutes, on the maintenance queue. Nothing it records is acted
on; it is how a challenger earns -- or fails to earn -- a promotion.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from coldops.workflows.queues import MAINTENANCE_QUEUE
    from coldops.workflows.types import MlShadowInput, MlShadowResult

#: Ten model calls at most, each bounded by the gateway's own timeout.
SHADOW_TIMEOUT = timedelta(minutes=12)

#: Safe to retry: every write is keyed on (model, subject).
SHADOW_RETRY = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=30))

DEFAULT_CRON = "7,22,37,52 * * * *"


@workflow.defn(name="MlShadowWorkflow")
class MlShadowWorkflow:
    """Let the models in shadow read the latest replies. Scheduled by Temporal."""

    @workflow.run
    async def run(self, request: MlShadowInput) -> MlShadowResult:
        return await workflow.execute_activity(
            "run_ml_shadow",
            request,
            task_queue=MAINTENANCE_QUEUE,
            start_to_close_timeout=SHADOW_TIMEOUT,
            retry_policy=SHADOW_RETRY,
            result_type=MlShadowResult,
        )


def ml_shadow_workflow_id(workspace_id: str) -> str:
    return f"ml-shadow::{workspace_id}"


__all__ = ["DEFAULT_CRON", "MlShadowWorkflow", "ml_shadow_workflow_id"]
