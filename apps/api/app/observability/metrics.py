"""TITAN Prometheus Metrics Exporter."""

from prometheus_client import Counter, Histogram, Gauge, generate_latest
from fastapi import Response, APIRouter

router = APIRouter()

# Agent Metrics
AGENT_EXECUTIONS = Counter(
    "titan_agent_executions_total",
    "Total number of agent executions",
    ["agent_type", "status"],
)

AGENT_DURATION = Histogram(
    "titan_agent_execution_duration_seconds",
    "Agent execution duration",
    ["agent_type"],
    buckets=[0.1, 0.5, 1.0, 5.0, 10.0, 30.0],
)

# LLM Metrics
LLM_TOKENS = Counter(
    "titan_llm_tokens_total",
    "Total LLM tokens consumed",
    ["model", "type"],  # token_type: prompt or completion
)

LLM_COST = Counter("titan_llm_cost_usd_total", "Total LLM cost in USD", ["model"])

# Tool Metrics
TOOL_CALLS = Counter(
    "titan_tool_calls_total", "Total tool calls", ["tool_name", "status"]
)

# HITL Metrics
HITL_APPROVAL_TIME = Histogram(
    "titan_hitl_approval_time_seconds",
    "Time taken for human approval",
    ["workflow_type"],
    buckets=[60, 300, 900, 1800, 3600],
)

# System Metrics
ACTIVE_WORKFLOWS = Gauge(
    "titan_active_workflows", "Number of active Temporal workflows"
)


@router.get("")
def metrics_endpoint() -> Response:
    """FastAPI endpoint for Prometheus scraping."""
    return Response(generate_latest(), media_type="text/plain")
