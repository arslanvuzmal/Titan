# Titan-OS control plane / workers.
# Multi-stage: the runtime image contains no build toolchain and runs as a
# non-root user with a read-only root filesystem (compose enforces the latter).
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN pip install --no-cache-dir uv==0.5.11

COPY apps/api/pyproject.toml apps/api/README.md* ./
COPY apps/api/titan ./titan
RUN uv pip install --system --no-cache .

FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1

# Fixed uid/gid so the compose `user:` directive and any mounted volume agree.
RUN groupadd --gid 10001 titan \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin titan

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

WORKDIR /app
COPY apps/api/titan ./titan
COPY apps/api/alembic.ini ./alembic.ini

USER 10001:10001
EXPOSE 8000

# Liveness only; readiness is /ready and is checked by the orchestrator.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "titan.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
