from prometheus_client import Histogram, Counter, generate_latest, CONTENT_TYPE_LATEST
from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter()

# --- Prometheus Metrics Definitions ---

# Histogram for tracking the duration of agent executions
AGENT_EXECUTION_DURATION = Histogram(
    "titan_agent_execution_duration_seconds",
    "Time spent executing LangGraph agents",
    ["agent_name"]
)

# Counter for tracking LLM token usage, labeled by model
LLM_TOKENS_TOTAL = Counter(
    "titan_llm_tokens_total",
    "Total number of tokens consumed by LLMs",
    ["model_name"]
)

# Counter for tool executions, tracking success/failure rates
TOOL_CALLS_TOTAL = Counter(
    "titan_tool_calls_total",
    "Total number of tool calls made",
    ["tool_name", "status"]
)

# Histogram for tracking Human-in-the-Loop decision times
HITL_APPROVAL_TIME = Histogram(
    "titan_hitl_approval_time_seconds",
    "Time spent waiting for a human to approve a pending task",
    ["workflow_type"]
)

@router.get("")
async def metrics():
    """
    Exposes Prometheus-compatible metrics.
    Scraping this endpoint allows visualizing data in Grafana.
    """
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )
