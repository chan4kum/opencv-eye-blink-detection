"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.exceptions import HTTPException as StarletteHTTPException

from eye_blink import __version__
from eye_blink.api.middleware import ObservabilityMiddleware
from eye_blink.api.routes import ops, v1
from eye_blink.api.state import AppState
from eye_blink.config import Settings, get_settings
from eye_blink.errors import AppError
from eye_blink.jobs import JobBus
from eye_blink.landmarker import verify_model
from eye_blink.logging_config import configure_logging, get_logger
from eye_blink.metrics import BUILD_INFO
from eye_blink.service import BlinkService
from eye_blink.storage import ObjectStore
from eye_blink.telemetry import setup_tracing

log = get_logger(__name__)

PROBLEM_JSON = "application/problem+json"


def build_service(settings: Settings) -> BlinkService:
    """Verify the pinned model, create the landmarker pool and warm it up (blocking, CPU-bound)."""
    verify_model(settings.model_path, settings.model_sha256)
    service = BlinkService(settings)
    service.warmup()
    return service


def problem(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    headers: dict[str, str] | None = None,
    extra: dict[str, object] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "type": f"urn:eye-blink:problem:{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "request_id": getattr(request.state, "request_id", None),
    }
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return problem(
            request, status=exc.status, code=exc.code, title=exc.title, detail=exc.detail, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return problem(
            request,
            status=422,
            code="validation-error",
            title="Request validation failed",
            detail="one or more request parameters are invalid",
            extra={"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(
            request,
            status=exc.status_code,
            code=f"http-{exc.status_code}",
            title=str(exc.detail),
            detail=str(exc.detail),
            headers=dict(exc.headers or {}),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_exception", path=request.url.path, exc_info=exc)
        return problem(
            request,
            status=500,
            code="internal-error",
            title="Internal server error",
            detail="an unexpected error occurred; quote the request_id when reporting it",
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        tracer_provider = setup_tracing(settings)
        # Model load + warm-up is blocking CPU work: keep it off the event loop.
        service = await anyio.to_thread.run_sync(build_service, settings)
        state = AppState(settings=settings, service=service)
        BUILD_INFO.labels(
            version=__version__, model_sha256=settings.model_sha256[:12], signal=settings.signal.value
        ).set(1)
        if settings.async_enabled:
            state.store = ObjectStore(settings)
            state.bus = JobBus(settings)
            await state.bus.connect()
        if not settings.api_key_hashes:
            log.warning("authentication_disabled", environment=settings.environment)
        app.state.eb = state
        app.state.settings = settings
        log.info(
            "started",
            version=__version__,
            environment=settings.environment,
            signal=settings.signal.value,
            async_enabled=settings.async_enabled,
        )
        try:
            yield
        finally:
            state.shutting_down = True  # readiness flips to 503 while in-flight requests drain
            await anyio.to_thread.run_sync(service.close)
            if state.bus is not None:
                await state.bus.close()
            if tracer_provider is not None:
                await asyncio.to_thread(tracer_provider.shutdown)
            log.info("stopped")

    app = FastAPI(
        title="Eye Blink Detection Service",
        version=__version__,
        description="Eye-blink detection: eye state per image, async video analysis, and a live WebSocket stream.",
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.add_middleware(ObservabilityMiddleware)
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allow_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )
    install_error_handlers(app)
    app.include_router(ops)
    app.include_router(v1)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    if settings.environment != "test":
        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
    return app
