"""TITAN Structured Logging with Correlation IDs."""

import logging
import json
from datetime import datetime


class JSONFormatter(logging.Formatter):
    """JSON log formatter for production."""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add extra fields if present
        if hasattr(record, "trace_id"):
            log_data["trace_id"] = str(getattr(record, "trace_id"))
        if hasattr(record, "span_id"):
            log_data["span_id"] = str(getattr(record, "span_id"))
        if hasattr(record, "organization_id"):
            log_data["organization_id"] = str(getattr(record, "organization_id"))

        return json.dumps(log_data)


def setup_logging(level: str = "INFO") -> None:
    """Configure structured logging."""
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())

    logger = logging.getLogger("titan")
    logger.setLevel(level)
    logger.addHandler(handler)
