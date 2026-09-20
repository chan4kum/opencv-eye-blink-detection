"""Pure-ASGI middleware: request IDs, security headers, access logs and HTTP metrics."""

from __future__ import annotations

import re
import time
import uuid
from typing import TYPE_CHECKING

import structlog

from eye_blink.logging_config import get_logger
from eye_blink.metrics import HTTP_DURATION, HTTP_IN_FLIGHT, HTTP_REQUESTS

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = get_logger("eye_blink.access")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{8,128}$")
_QUIET_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})
_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        supplied = headers.get("x-request-id", "")
        request_id = supplied if _REQUEST_ID_RE.match(supplied) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        path: str = scope["path"]
        method: str = scope["method"]
        status = 500
        start = time.perf_counter()
        HTTP_IN_FLIGHT.inc()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                hdrs: list[tuple[bytes, bytes]] = message.setdefault("headers", [])
                hdrs.append((b"x-request-id", request_id.encode()))
                hdrs.append((b"x-content-type-options", b"nosniff"))
                hdrs.append((b"referrer-policy", b"no-referrer"))
                if not path.startswith(_DOC_PATHS):
                    hdrs.append((b"cache-control", b"no-store"))
                    hdrs.append((b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            HTTP_IN_FLIGHT.dec()
            route = scope.get("route")
            route_label = getattr(route, "path", None) or "unmatched"  # low-cardinality template, never raw path
            HTTP_REQUESTS.labels(method, route_label, str(status)).inc()
            HTTP_DURATION.labels(method, route_label).observe(elapsed)
            emit = log.debug if path in _QUIET_PATHS else log.info
            emit(
                "http_request",
                method=method,
                path=path,
                route=route_label,
                status=status,
                duration_ms=round(elapsed * 1000, 2),
            )
