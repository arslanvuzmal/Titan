FROM python:3.11-slim

WORKDIR /app

# Create non-root user
RUN useradd -m -u 1000 titan

# Copy application source
COPY apps/api/pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir uvicorn gunicorn fastapi pydantic

COPY apps/api/app/ ./app/

# Set permissions
RUN chown -R titan:titan /app

USER titan

EXPOSE 8000

CMD ["gunicorn", "app.main:app", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000"]
