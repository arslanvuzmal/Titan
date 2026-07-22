"""TITAN LangGraph Callback Handler for Tracing."""

from typing import Any, Dict, List
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from opentelemetry import trace
from .metrics import LLM_TOKENS, LLM_COST, AGENT_EXECUTIONS, AGENT_DURATION
import time

tracer = trace.get_tracer(__name__)


class LangGraphTracer(BaseCallbackHandler):
    """Custom callback handler for tracing LangGraph executions."""

    def __init__(self, agent_type: str):
        self.agent_type = agent_type
        self.start_times: Dict[str, float] = {}

    def on_llm_start(
        self, serialized: Dict[str, Any], prompts: List[str], **kwargs: Any
    ) -> Any:
        """Run when LLM starts running."""
        run_id = str(kwargs.get("run_id"))
        self.start_times[run_id] = time.time()

        with tracer.start_as_current_span(f"{self.agent_type}.llm_call") as span:
            span.set_attribute("agent.type", self.agent_type)
            if "model" in kwargs.get("invocation_params", {}):
                span.set_attribute("llm.model", kwargs["invocation_params"]["model"])

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> Any:
        """Run when LLM ends running."""
        run_id = str(kwargs.get("run_id"))
        duration = time.time() - self.start_times.pop(run_id, time.time())

        AGENT_DURATION.labels(agent_type=self.agent_type).observe(duration)
        AGENT_EXECUTIONS.labels(agent_type=self.agent_type, status="success").inc()

        if response.llm_output and "token_usage" in response.llm_output:
            token_usage = response.llm_output["token_usage"]
            model = response.llm_output.get("model_name", "unknown")

            prompt_tokens = token_usage.get("prompt_tokens", 0)
            completion_tokens = token_usage.get("completion_tokens", 0)

            LLM_TOKENS.labels(model=model, type="prompt").inc(prompt_tokens)
            LLM_TOKENS.labels(model=model, type="completion").inc(completion_tokens)

            # Simple cost estimation
            cost = (prompt_tokens * 0.00001) + (completion_tokens * 0.00003)
            LLM_COST.labels(model=model).inc(cost)

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> Any:
        """Run when LLM errors."""
        AGENT_EXECUTIONS.labels(agent_type=self.agent_type, status="error").inc()
