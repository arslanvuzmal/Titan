from langchain_core.callbacks import BaseCallbackHandler
from typing import Any, Dict, List, Optional
from uuid import UUID
from .tracer import tracer

class TITANTracingCallback(BaseCallbackHandler):
    """
    A custom LangChain/LangGraph callback handler that creates OpenTelemetry spans
    for every LangGraph node execution.
    """
    def __init__(self):
        super().__init__()
        # We need to map LangChain run_ids to OTel spans
        self.span_map = {}

    def _truncate_payload(self, payload: Any) -> str:
        """Truncates payload to 1000 characters to prevent span bloat."""
        str_val = str(payload)
        if len(str_val) > 1000:
            return str_val[:1000] + "... [TRUNCATED]"
        return str_val

    def on_chain_start(
        self, serialized: Dict[str, Any], inputs: Dict[str, Any], *, run_id: UUID, parent_run_id: Optional[UUID] = None, **kwargs: Any
    ) -> Any:
        span_name = serialized.get("name", "langgraph_node")
        span = tracer.start_span(span_name)
        span.set_attribute("langchain.inputs", self._truncate_payload(inputs))
        self.span_map[run_id] = span

    def on_chain_end(
        self, outputs: Dict[str, Any], *, run_id: UUID, parent_run_id: Optional[UUID] = None, **kwargs: Any
    ) -> Any:
        if run_id in self.span_map:
            span = self.span_map[run_id]
            span.set_attribute("langchain.outputs", self._truncate_payload(outputs))
            span.end()
            del self.span_map[run_id]

    def on_chain_error(
        self, error: Exception, *, run_id: UUID, parent_run_id: Optional[UUID] = None, **kwargs: Any
    ) -> Any:
        if run_id in self.span_map:
            span = self.span_map[run_id]
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(error))
            span.end()
            del self.span_map[run_id]

    def on_tool_start(
        self, serialized: Dict[str, Any], input_str: str, *, run_id: UUID, parent_run_id: Optional[UUID] = None, **kwargs: Any
    ) -> Any:
        tool_name = serialized.get("name", "unknown_tool")
        span = tracer.start_span(f"tool:{tool_name}")
        span.set_attribute("tool.input", self._truncate_payload(input_str))
        self.span_map[run_id] = span

    def on_tool_end(
        self, output: str, *, run_id: UUID, parent_run_id: Optional[UUID] = None, **kwargs: Any
    ) -> Any:
        if run_id in self.span_map:
            span = self.span_map[run_id]
            span.set_attribute("tool.output", self._truncate_payload(output))
            span.end()
            del self.span_map[run_id]

    def on_tool_error(
        self, error: Exception, *, run_id: UUID, parent_run_id: Optional[UUID] = None, **kwargs: Any
    ) -> Any:
        if run_id in self.span_map:
            span = self.span_map[run_id]
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(error))
            span.end()
            del self.span_map[run_id]
