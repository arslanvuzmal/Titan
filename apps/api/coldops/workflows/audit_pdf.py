"""The schedule that renders each first email's personal PDF ahead of sending.

Every five minutes, on the maintenance queue. The outbox worker never waits for
it: a first email whose PDF is not ready yet goes without one, and without the
sentence announcing it -- so the schedule's job is to be early, and a missed
run costs one attachment, never one email.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from coldops.workflows.queues import MAINTENANCE_QUEUE
    from coldops.workflows.types import RenderAuditPdfsInput, RenderAuditPdfsResult

#: Up to ten renders, each a Chromium launch of a few seconds.
RENDER_TIMEOUT = timedelta(minutes=8)

#: Retried once: a render that failed is simply tried again on the next run.
RENDER_RETRY = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=30))

DEFAULT_CRON = "*/5 * * * *"


@workflow.defn(name="AuditPdfWorkflow")
class AuditPdfWorkflow:
    """Render the personal PDFs the next first emails will carry. Scheduled by Temporal."""

    @workflow.run
    async def run(self, request: RenderAuditPdfsInput) -> RenderAuditPdfsResult:
        return await workflow.execute_activity(
            "render_audit_pdfs",
            request,
            task_queue=MAINTENANCE_QUEUE,
            start_to_close_timeout=RENDER_TIMEOUT,
            retry_policy=RENDER_RETRY,
            result_type=RenderAuditPdfsResult,
        )


def audit_pdf_workflow_id(workspace_id: str) -> str:
    return f"audit-pdf::{workspace_id}"


__all__ = ["DEFAULT_CRON", "AuditPdfWorkflow", "audit_pdf_workflow_id"]
