# ---------------------------------------
# Stage 1: Builder
# ---------------------------------------
FROM python:3.11-slim as builder

# Install uv for fast dependency resolution
RUN pip install --no-cache-dir uv

WORKDIR /build

# Copy dependency files
COPY apps/api/pyproject.toml apps/api/uv.lock ./apps/api/
COPY packages/contracts/ ./packages/contracts/

WORKDIR /build/apps/api

# Export requirements using uv and install into a virtual environment
RUN uv pip compile pyproject.toml -o requirements.txt && \
    python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt && \
    /opt/venv/bin/pip install --no-cache-dir -e ../../packages/contracts && \
    /opt/venv/bin/pip install --no-cache-dir uvicorn[standard] gunicorn

# ---------------------------------------
# Stage 2: Runtime
# ---------------------------------------
FROM python:3.11-slim

WORKDIR /app

# Create a non-root user
RUN groupadd -r titan && useradd -r -g titan titan

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Ensure the virtual environment is in the PATH
ENV PATH="/opt/venv/bin:$PATH"

# Copy application source code
COPY apps/api/app/ ./app/

# Set ownership to the non-root user
RUN chown -R titan:titan /app

# Switch to the non-root user
USER titan

# Expose API port
EXPOSE 8000

# Run FastAPI via Gunicorn with Uvicorn workers
CMD ["gunicorn", "app.main:app", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000"]
