import os
from contextlib import contextmanager
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

# Initialize TracerProvider
provider = TracerProvider()

# For production, you would configure an OTLP endpoint (e.g., to Jaeger, Datadog, or GCP Cloud Trace).
# For local dev and demonstration, we use ConsoleSpanExporter to print traces.
otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
if otlp_endpoint:
    # Use OTLP exporter if configured (requires opentelemetry-exporter-otlp)
    pass
else:
    # Fallback to console
    processor = BatchSpanProcessor(ConsoleSpanExporter())
    provider.add_span_processor(processor)

trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)


@contextmanager
def LLMTracer(model_name: str, temperature: float = 0.7):
    """
    Context manager for tracing LLM executions and calculating costs.
    Usage:
        with LLMTracer("gpt-4o", temperature=0.5) as span:
            response = call_llm(...)
            span.set_attribute("llm.token_count", response.tokens)
    """
    with tracer.start_as_current_span("llm_execution") as span:
        span.set_attribute("llm.model", model_name)
        span.set_attribute("llm.temperature", temperature)
        try:
            yield span
        finally:
            # At this point, the caller should have populated token counts
            # Let's auto-calculate cost based on model (mock pricing)
            tokens = span.attributes.get("llm.token_count", 0)
            if model_name.startswith("gpt-4"):
                cost = (tokens / 1000) * 0.01  # Mock $0.01 per 1k tokens
            else:
                cost = (tokens / 1000) * 0.001
            span.set_attribute("llm.cost_usd", cost)
