"""TITAN Performance Profiling Utilities."""

import psutil
import logging
from typing import Dict, Any

logger = logging.getLogger("titan.profiling")


def get_memory_usage() -> Dict[str, float]:
    """Returns memory usage in MB."""
    process = psutil.Process()
    mem_info = process.memory_info()
    return {
        "rss_mb": mem_info.rss / 1024 / 1024,
        "vms_mb": mem_info.vms / 1024 / 1024,
        "percent": process.memory_percent(),
    }


def get_cpu_usage() -> float:
    """Returns CPU usage percentage."""
    return psutil.Process().cpu_percent(interval=0.1)


def profile_system_state() -> Dict[str, Any]:
    """Capture a snapshot of the system state for profiling."""
    mem = get_memory_usage()
    cpu = get_cpu_usage()

    state = {
        "memory": mem,
        "cpu_percent": cpu,
    }

    if mem["percent"] > 80.0:
        logger.warning(f"High memory usage detected: {mem['percent']:.1f}%")

    return state
