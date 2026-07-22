"""TITAN Temporal Workflow Tracing Interceptors."""

from opentelemetry import trace
from temporalio.worker import (
    Interceptor,
    ActivityInboundInterceptor,
    WorkflowInboundInterceptor,
)
from temporalio.worker import ExecuteActivityInput, ExecuteWorkflowInput
from typing import Any

tracer = trace.get_tracer(__name__)


class TracingActivityInboundInterceptor(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        with tracer.start_as_current_span(f"Activity: {input.activity}") as span:
            span.set_attribute("activity.id", input.activity_id)
            try:
                return await super().execute_activity(input)
            except Exception as e:
                span.record_exception(e)
                raise


class TracingWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        with tracer.start_as_current_span(f"Workflow: {input.workflow}") as span:
            span.set_attribute("workflow.id", input.id)
            span.set_attribute("workflow.run_id", input.run_id)
            try:
                return await super().execute_workflow(input)
            except Exception as e:
                span.record_exception(e)
                raise


class TracingInterceptor(Interceptor):
    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return TracingActivityInboundInterceptor(next)

    def intercept_workflow(
        self, next: WorkflowInboundInterceptor
    ) -> WorkflowInboundInterceptor:
        return TracingWorkflowInboundInterceptor(next)
