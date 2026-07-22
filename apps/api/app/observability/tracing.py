"""TITAN Distributed Tracing with OpenTelemetry."""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup_tracing(service_name: str = "titan-api") -> None:
    """Initialize OpenTelemetry tracing."""
    resource = Resource.create({"service.name": service_name})
    tracer_provider = TracerProvider(resource=resource)

    # Export to Jaeger/Tempo for local dev
    otlp_exporter = OTLPSpanExporter(endpoint="localhost:4317", insecure=True)
    processor = BatchSpanProcessor(otlp_exporter)
    tracer_provider.add_span_processor(processor)
    trace.set_tracer_provider(tracer_provider)
