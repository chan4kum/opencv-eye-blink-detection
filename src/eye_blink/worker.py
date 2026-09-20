"""Queue worker: consumes detection jobs from NATS JetStream.

Run with ``uvicorn eye_blink.worker:create_app --factory``. The process is a small FastAPI
app so it shares probes, metrics, logging and tracing with the API, and so SIGTERM triggers the
same graceful-shutdown path: stop pulling, finish in-flight jobs, then disconnect.

Delivery semantics are at-least-once. Handling is idempotent: a redelivered job whose record is
already terminal is acknowledged without being processed again.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from nats.errors import TimeoutError as NatsTimeoutError
from opentelemetry import propagate
from opentelemetry.trace import SpanKind
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError

from eye_blink import __version__
from eye_blink.api.app import build_service, install_error_handlers
from eye_blink.api.middleware import ObservabilityMiddleware
from eye_blink.config import Settings, get_settings
from eye_blink.errors import AppError
from eye_blink.jobs import JobBus, JobMessage, JobRecord, JobStatus
from eye_blink.logging_config import configure_logging, get_logger
from eye_blink.metrics import BUILD_INFO, JOB_E2E_DURATION, JOBS_PROCESSED, WORKER_IN_FLIGHT
from eye_blink.schemas import HealthResponse
from eye_blink.service import BlinkService
from eye_blink.storage import ObjectNotFoundError, ObjectStore
from eye_blink.telemetry import get_tracer, setup_tracing

if TYPE_CHECKING:
    from nats.aio.msg import Msg

log = get_logger(__name__)


def _is_permanent(exc: Exception) -> bool:
    """Errors caused by the input itself (4xx, missing object) cannot succeed on retry: fail the job now.

    5xx ``AppError``s (``OverloadedError``, ``DependencyUnavailableError``) are infrastructure conditions
    and must be retried.
    """
    if isinstance(exc, ObjectNotFoundError):
        return True
    return isinstance(exc, AppError) and exc.status < 500


_FETCH_TIMEOUT_S = 1.0


class Worker:
    def __init__(self, settings: Settings, bus: JobBus, store: ObjectStore, service: BlinkService) -> None:
        self._s = settings
        self._bus = bus
        self._store = store
        self._service = service
        self._stopping = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._loop(i), name=f"worker-{i}") for i in range(self._s.worker_concurrency)
        ]

    @property
    def healthy(self) -> bool:
        return bool(self._tasks) and all(not t.done() for t in self._tasks) and self._bus.is_ready()

    async def stop(self) -> None:
        """Stop pulling new jobs and wait (bounded) for in-flight ones to finish."""
        self._stopping.set()
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=self._s.job_ack_wait_s)
            for task in pending:  # in-flight job exceeded the drain budget: JetStream will redeliver it
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _loop(self, index: int) -> None:
        sub = await self._bus.pull_subscription()
        log.info("worker_loop_started", index=index)
        while not self._stopping.is_set():
            try:
                msgs = await sub.fetch(batch=1, timeout=_FETCH_TIMEOUT_S)
            except NatsTimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("fetch_failed", index=index, error=repr(exc))
                await asyncio.sleep(1.0)  # back off while NATS reconnects
                continue
            for msg in msgs:
                await self._handle(msg)
        with contextlib.suppress(Exception):
            await sub.unsubscribe()
        log.info("worker_loop_stopped", index=index)

    async def _handle(self, msg: Msg) -> None:
        WORKER_IN_FLIGHT.inc()
        try:
            ctx = propagate.extract(dict(msg.headers or {}))
            with get_tracer().start_as_current_span("process_job", context=ctx, kind=SpanKind.CONSUMER):
                await self._process(msg)
        except Exception as exc:  # last-resort guard: never let one message kill the loop
            log.exception("unexpected_worker_error", error=repr(exc))
            with contextlib.suppress(Exception):
                await msg.nak(delay=self._s.job_retry_backoff_s)
        finally:
            WORKER_IN_FLIGHT.dec()

    async def _process(self, msg: Msg) -> None:
        attempt = msg.metadata.num_delivered
        try:
            job = JobMessage.model_validate_json(msg.data)
        except ValidationError:
            log.error("poison_message", attempt=attempt)
            JOBS_PROCESSED.labels(outcome="poison").inc()
            await msg.term()  # unparseable: cannot be retried into success
            return

        log.info("job_started", job_id=job.job_id, attempt=attempt)
        record = await self._bus.get_record(job.job_id)
        if record is None:  # KV entry expired before we ran; recreate so the outcome is observable
            record = JobRecord(
                job_id=job.job_id,
                owner=job.owner,
                status=JobStatus.QUEUED,
                created_at=job.created_at,
                updated_at=time.time(),
            )
        if record.status.terminal:
            log.info("job_already_terminal", job_id=job.job_id, status=record.status.value)
            await msg.ack()
            return

        record.status, record.attempts, record.updated_at = JobStatus.PROCESSING, attempt, time.time()
        await self._bus.put_record(record)

        try:
            data = await self._store.get(job.object_key)
            result = await self._service.analyze_video_bytes(data, source="worker")
        except Exception as exc:
            if _is_permanent(exc):
                await self._finish(msg, record, JobStatus.FAILED, error=_public_error(exc), outcome="failed_permanent")
                await self._cleanup(job)
                return
            if attempt >= self._s.job_max_deliver:
                log.error("job_retries_exhausted", job_id=job.job_id, attempt=attempt, error=repr(exc))
                await self._finish(
                    msg, record, JobStatus.FAILED, error="processing failed after retries", outcome="failed_exhausted"
                )
                await self._cleanup(job)
            else:
                log.warning("job_retry", job_id=job.job_id, attempt=attempt, error=repr(exc))
                JOBS_PROCESSED.labels(outcome="retried").inc()
                await msg.nak(delay=self._s.job_retry_backoff_s * attempt)
            return

        record.result = result.model_dump(mode="json")
        await self._finish(msg, record, JobStatus.SUCCEEDED, outcome="succeeded")
        await self._cleanup(job)

    async def _finish(
        self, msg: Msg, record: JobRecord, status: JobStatus, *, outcome: str, error: str | None = None
    ) -> None:
        record.status, record.error, record.updated_at = status, error, time.time()
        await self._bus.put_record(record)  # persist the outcome *before* acknowledging the message
        await msg.ack()
        JOBS_PROCESSED.labels(outcome=outcome).inc()
        JOB_E2E_DURATION.observe(max(0.0, record.updated_at - record.created_at))
        log.info("job_finished", job_id=record.job_id, status=status.value, outcome=outcome)

    async def _cleanup(self, job: JobMessage) -> None:
        if self._s.delete_input_after_processing:
            with contextlib.suppress(Exception):  # objects also expire via bucket lifecycle rules
                await self._store.delete(job.object_key)


def _public_error(exc: Exception) -> str:
    if isinstance(exc, AppError):
        return exc.detail
    if isinstance(exc, ObjectNotFoundError):
        return "input image not found (it may have expired)"
    return "processing failed"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        tracer_provider = setup_tracing(settings)
        service = await anyio.to_thread.run_sync(build_service, settings)
        BUILD_INFO.labels(
            version=__version__, model_sha256=settings.model_sha256[:12], signal=settings.signal.value
        ).set(1)
        bus, store = JobBus(settings), ObjectStore(settings)
        await bus.connect()
        worker = Worker(settings, bus, store, service)
        worker.start()
        app.state.worker = worker
        log.info("worker_started", version=__version__, concurrency=settings.worker_concurrency)
        try:
            yield
        finally:
            await worker.stop()
            await bus.close()
            await anyio.to_thread.run_sync(service.close)
            if tracer_provider is not None:
                await asyncio.to_thread(tracer_provider.shutdown)
            log.info("worker_stopped")

    app = FastAPI(
        title="Eye Blink Detection Worker",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(ObservabilityMiddleware)
    install_error_handlers(app)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__)

    @app.get("/readyz", response_model=HealthResponse, responses={503: {"model": HealthResponse}})
    async def readyz(request: Request) -> Response:
        ok = bool(request.app.state.worker.healthy)
        body = HealthResponse(status="ready" if ok else "not_ready", checks={"worker": ok}, version=__version__)
        return JSONResponse(body.model_dump(), status_code=200 if ok else 503)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
