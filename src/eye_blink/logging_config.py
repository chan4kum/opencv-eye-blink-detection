"""Structured logging (JSON in production) with trace correlation."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from opentelemetry import trace

from eye_blink.config import Settings


def _add_trace_context(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event["trace_id"] = format(ctx.trace_id, "032x")
        event["span_id"] = format(ctx.span_id, "016x")
    return event


def configure_logging(settings: Settings) -> None:
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_trace_context,
        structlog.processors.StackInfoRenderer(),
    ]
    renderer: Any = structlog.processors.JSONRenderer() if settings.log_json else structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level)
    # uvicorn installs its own handlers; route everything through ours. We log requests ourselves.
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers[:] = []
    access.propagate = False
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("nats").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)  # pulled in by MediaPipe; noisy at INFO


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
