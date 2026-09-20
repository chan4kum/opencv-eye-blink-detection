"""OpenTelemetry tracing. Enabled only when a standard OTLP endpoint env var is present."""

from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from eye_blink import __version__
from eye_blink.config import Settings
from eye_blink.logging_config import get_logger

log = get_logger(__name__)


def tracing_enabled() -> bool:
    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))


def setup_tracing(settings: Settings) -> TracerProvider | None:
    """Install a global tracer provider exporting via OTLP/HTTP (configured by ``OTEL_*`` env vars)."""
    if not tracing_enabled():
        log.info("tracing_disabled", reason="no OTEL_EXPORTER_OTLP_ENDPOINT set")
        return None
    resource = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME", settings.service_name),
            "service.version": __version__,
            "deployment.environment.name": settings.environment,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    log.info("tracing_enabled")
    return provider


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("eye_blink", __version__)
