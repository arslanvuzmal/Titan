"""TITAN Health Check Endpoints."""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Dict, Any

router = APIRouter()


class HealthStatus(BaseModel):
    status: str
    version: str
    checks: Dict[str, Any]


async def check_database() -> Dict[str, str]:
    """Mock DB health check."""
    return {"status": "ok"}


async def check_redis() -> Dict[str, str]:
    """Mock Redis health check."""
    return {"status": "ok"}


async def check_temporal() -> Dict[str, str]:
    """Mock Temporal health check."""
    return {"status": "ok"}


async def check_llm_api() -> Dict[str, str]:
    """Mock LLM API health check."""
    return {"status": "ok"}


@router.get("/health", response_model=HealthStatus)
async def health_check() -> HealthStatus:
    """Comprehensive health check."""
    checks = {}

    # Database health
    checks["database"] = await check_database()

    # Redis health
    checks["redis"] = await check_redis()

    # Temporal health
    checks["temporal"] = await check_temporal()

    # LLM API health
    checks["llm_api"] = await check_llm_api()

    overall_status = (
        "healthy"
        if all(v.get("status") == "ok" for v in checks.values())
        else "unhealthy"
    )

    return HealthStatus(status=overall_status, version="0.12.0", checks=checks)


@router.get("/ready")
async def readiness_check() -> Dict[str, str]:
    """Readiness probe for Kubernetes."""
    return {"status": "ready"}


@router.get("/live")
async def liveness_check() -> Dict[str, str]:
    """Liveness probe for Kubernetes."""
    return {"status": "alive"}
