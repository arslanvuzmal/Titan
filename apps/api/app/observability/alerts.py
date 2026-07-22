"""TITAN Alerting Rules Configuration."""

from dataclasses import dataclass
from typing import List
import logging

logger = logging.getLogger("titan.alerts")


@dataclass
class AlertRule:
    name: str
    description: str
    severity: str
    threshold: float
    window_minutes: int


CRITICAL_ALERTS = [
    AlertRule(
        "high_agent_failure_rate", "Agent failure rate > 10%", "critical", 0.10, 5
    ),
    AlertRule("high_llm_api_errors", "LLM API error rate > 5%", "critical", 0.05, 5),
    AlertRule(
        "stuck_temporal_workflow",
        "Temporal workflow stuck > 30 min",
        "critical",
        1800,
        30,
    ),
    AlertRule(
        "db_pool_exhausted", "Database connection pool exhausted", "critical", 1.0, 1
    ),
]

WARNING_ALERTS = [
    AlertRule("high_agent_latency", "Avg agent latency > 10s", "warning", 10.0, 5),
    AlertRule("slow_hitl_approval", "HITL approval time > 1 hour", "warning", 3600, 60),
    AlertRule(
        "token_usage_spike", "Token usage spike > 200% of baseline", "warning", 2.0, 60
    ),
    AlertRule("high_memory_usage", "Memory usage > 80%", "warning", 0.8, 5),
]


def check_alerts(metrics_data: dict) -> List[str]:
    """Evaluate alerts based on current metrics data."""
    triggered_alerts = []
    # Implementation of alert evaluation logic would go here
    return triggered_alerts
