"""Structured JSON logging with redaction wired in.

The pre-0.2 code had a redaction module that nothing called (gap analysis
H-19, K-02). Here the redactor sits inside the formatter, so a secret cannot
reach a log line by being passed to ``logger.info`` -- the only way to log is
through this path.

Every record carries the correlation fields from mission section 19 when the
caller supplies them via ``extra=``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from typing import Any

from titan.security.redaction import redact, redact_string

#: Fields the logging module puts on every record; not part of our payload.
_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)

#: Correlation fields promoted to the top level of the log line when present.
CONTEXT_FIELDS = (
    "request_id",
    "workflow_id",
    "activity_id",
    "workspace_id",
    "campaign_id",
    "lead_id",
    "message_id",
    "outbox_id",
    "provider",
    "model",
    "error_code",
    "retry",
    "duration_ms",
    "cost_usd",
)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, environment: str) -> None:
        super().__init__()
        self.service = service
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "env": self.environment,
            "logger": record.name,
            # redact_string also strips control characters, which is what stops
            # an attacker-supplied field from forging extra log lines.
            "msg": redact_string(record.getMessage()),
        }

        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = redact(value)

        if record.exc_info:
            payload["exception"] = redact_string(self.formatException(record.exc_info))

        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(
    *, level: str = "INFO", service: str = "titan", environment: str = "local"
) -> None:
    """Install the JSON formatter on the root logger. Idempotent."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service=service, environment=environment))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # These are chatty at INFO and say nothing Titan needs.
    for noisy in ("sqlalchemy.engine.Engine", "httpx", "httpcore", "temporalio"):
        logging.getLogger(noisy).setLevel("WARNING")


__all__ = ["CONTEXT_FIELDS", "JsonFormatter", "configure_logging"]
