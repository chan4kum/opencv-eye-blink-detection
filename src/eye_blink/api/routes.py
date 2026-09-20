"""HTTP and WebSocket routes."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketState

from eye_blink import __version__
from eye_blink.api.security import CurrentPrincipal, authenticate_websocket
from eye_blink.api.state import AppState, get_state
from eye_blink.errors import (
    AppError,
    AsyncDisabledError,
    NotFoundError,
    PayloadTooLargeError,
    UnauthorizedError,
    UnsupportedMediaTypeError,
)
from eye_blink.jobs import JobMessage, JobStatus, is_valid_job_id, new_job_id
from eye_blink.metrics import (
    IMAGES_REJECTED,
    JOBS_SUBMITTED,
    STREAM_FRAMES_DROPPED,
    STREAM_SESSIONS,
    STREAM_SESSIONS_TOTAL,
)
from eye_blink.schemas import (
    EyeStateResponse,
    HealthResponse,
    JobAccepted,
    JobResponse,
    ModelInfo,
    ProblemDetails,
)
from eye_blink.video import sniff_container

State = Annotated[AppState, Depends(get_state)]

IMAGE_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/bmp", "application/octet-stream"})
VIDEO_CONTENT_TYPES = frozenset(
    {"video/mp4", "video/quicktime", "video/webm", "video/x-matroska", "video/x-msvideo", "application/octet-stream"}
)

_PROBLEMS: dict[int | str, dict[str, object]] = {
    401: {"model": ProblemDetails, "description": "Missing or invalid API key"},
    413: {"model": ProblemDetails, "description": "Payload too large"},
    415: {"model": ProblemDetails, "description": "Unsupported media type"},
    422: {"model": ProblemDetails, "description": "Not a valid image/video, or over the configured limits"},
    503: {"model": ProblemDetails, "description": "At capacity or dependency unavailable"},
}

ops = APIRouter(tags=["operations"])
v1 = APIRouter(prefix="/v1", tags=["blink detection"], responses=_PROBLEMS)


async def read_body(request: Request, limit: int, allowed: frozenset[str], what: str) -> bytes:
    """Read the raw request body, enforcing a hard byte limit while streaming."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in allowed:
        IMAGES_REJECTED.labels(reason="unsupported-media-type").inc()
        raise UnsupportedMediaTypeError(
            f"Content-Type {content_type or '(none)'!r} not accepted; send the raw {what} bytes with one of: "
            + ", ".join(sorted(allowed))
        )
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        IMAGES_REJECTED.labels(reason="payload-too-large").inc()
        raise PayloadTooLargeError(f"body exceeds the {limit}-byte limit")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            IMAGES_REJECTED.labels(reason="payload-too-large").inc()
            raise PayloadTooLargeError(f"body exceeds the {limit}-byte limit")
    return bytes(body)


# --------------------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------------------
@ops.get("/healthz", response_model=HealthResponse, summary="Liveness probe")
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@ops.get(
    "/readyz", response_model=HealthResponse, responses={503: {"model": HealthResponse}}, summary="Readiness probe"
)
async def readyz(state: State) -> Response:
    checks = {"model": True}
    if state.bus is not None:
        checks["nats"] = state.bus.is_ready()
        checks["object_storage"] = await state.storage_healthy()
    ready = all(checks.values()) and not state.shutting_down
    body = HealthResponse(status="ready" if ready else "not_ready", checks=checks, version=__version__)
    return JSONResponse(body.model_dump(), status_code=200 if ready else 503)


# --------------------------------------------------------------------------------------
# single-frame eye state
# --------------------------------------------------------------------------------------
@v1.get("/model", response_model=ModelInfo, summary="Loaded model metadata")
async def model_info(state: State, _: CurrentPrincipal) -> ModelInfo:
    return state.service.model


@v1.post(
    "/eye-state",
    response_model=EyeStateResponse,
    summary="Eye state of one image (open/closed)",
    description="Send the raw image bytes. A blink needs a time series: use `/v1/jobs` (video) or `/v1/stream` (live).",
)
async def eye_state(request: Request, state: State, _: CurrentPrincipal) -> EyeStateResponse:
    data = await read_body(request, state.settings.max_upload_bytes, IMAGE_CONTENT_TYPES, "image")
    image, eye, elapsed_ms = await state.service.eye_state(data, source="api")
    return EyeStateResponse.build(
        request_id=request.state.request_id,
        image=image,
        state=eye,
        signal=state.settings.signal,
        close_threshold=state.settings.blink_close_threshold,
        inference_ms=round(elapsed_ms, 3),
        model=state.service.model,
    )


# --------------------------------------------------------------------------------------
# asynchronous video jobs
# --------------------------------------------------------------------------------------
@v1.post(
    "/jobs",
    status_code=202,
    response_model=JobAccepted,
    summary="Submit a video for asynchronous blink analysis",
    description="Send the raw video bytes (MP4/MOV, WebM/Matroska or AVI). The container is detected from the bytes.",
    responses={501: {"model": ProblemDetails, "description": "Async pipeline disabled"}},
)
async def submit_job(request: Request, state: State, principal: CurrentPrincipal) -> JobAccepted:
    if state.bus is None or state.store is None:
        raise AsyncDisabledError
    data = await read_body(request, state.settings.max_video_bytes, VIDEO_CONTENT_TYPES, "video")
    sniff_container(data)  # cheap up-front validation; full limits are enforced by the worker

    job_id = new_job_id()
    key = f"inputs/{job_id}"
    await state.store.put(key, data)
    try:
        await state.bus.submit(JobMessage(job_id=job_id, owner=principal.id, object_key=key, created_at=time.time()))
    except Exception:
        await asyncio.shield(_best_effort_delete(state, key))  # do not orphan the upload
        raise
    JOBS_SUBMITTED.inc()
    return JobAccepted(job_id=job_id, status=JobStatus.QUEUED, status_url=f"/v1/jobs/{job_id}")


@v1.get("/jobs/{job_id}", response_model=JobResponse, summary="Get the status/result of a job")
async def get_job(job_id: str, state: State, principal: CurrentPrincipal) -> JobResponse:
    if state.bus is None:
        raise AsyncDisabledError
    # Owner mismatch is reported as 404 so job IDs of other tenants are not disclosed.
    record = await state.bus.get_record(job_id) if is_valid_job_id(job_id) else None
    if record is None or record.owner != principal.id:
        raise NotFoundError("no such job (it may have expired)")
    return JobResponse.model_validate(record.model_dump())


async def _best_effort_delete(state: AppState, key: str) -> None:
    if state.store is None:
        return
    with contextlib.suppress(Exception):  # cleanup must never mask the original error
        await state.store.delete(key)


# --------------------------------------------------------------------------------------
# live stream
# --------------------------------------------------------------------------------------
# Close codes: 1008 policy violation (auth), 1009 message too big, 1013 try again later, 1000 normal.
@v1.websocket("/stream")
async def stream(websocket: WebSocket) -> None:
    """Live blink detection.

    Client -> server: **binary** messages, each one JPEG/PNG/WebP/BMP frame. Text ``ping`` -> ``pong``.
    Server -> client: JSON ``frame`` messages (per analysed frame) and ``blink`` / ``long_closure`` messages.
    Timing uses server receive time, so keep the frame cadence steady. If frames arrive faster than they
    can be analysed, older frames are dropped so latency stays bounded.
    """
    state: AppState = websocket.app.state.eb
    settings = state.settings
    try:
        principal, subprotocol = authenticate_websocket(websocket)
    except UnauthorizedError:
        STREAM_SESSIONS_TOTAL.labels(reason="unauthorized").inc()
        await websocket.close(code=1008)
        return
    if state.shutting_down or state.active_streams >= settings.stream_max_sessions:
        STREAM_SESSIONS_TOTAL.labels(reason="at_capacity").inc()
        # Accept, then close with 1013 ("try again later"): refusing the handshake would surface as HTTP 403,
        # which clients cannot tell apart from an authentication failure.
        await websocket.accept(subprotocol=subprotocol)
        await websocket.close(code=1013, reason="server at capacity, retry later")
        return

    state.active_streams += 1
    STREAM_SESSIONS.inc()
    reason = "client_closed"
    session = None
    try:
        await websocket.accept(subprotocol=subprotocol)
        session = await anyio.to_thread.run_sync(state.service.new_stream_session)
        reason = await _run_stream(websocket, state, session)
    except WebSocketDisconnect:
        reason = "client_closed"
    except Exception:
        reason = "error"
        raise
    finally:
        state.active_streams -= 1
        STREAM_SESSIONS.dec()
        STREAM_SESSIONS_TOTAL.labels(reason=reason).inc()
        # Shielded: if this task is being cancelled (abrupt disconnect, server shutdown) the cleanup must
        # still run, otherwise the native landmarker would leak.
        with anyio.CancelScope(shield=True):
            if session is not None:
                await anyio.to_thread.run_sync(session.close)
            if websocket.application_state is WebSocketState.CONNECTED:
                with contextlib.suppress(Exception):
                    await websocket.close(code=1000 if reason != "too_big" else 1009)
    del principal  # authenticated; streams are not persisted so there is no ownership to record


async def _run_stream(websocket: WebSocket, state: AppState, session: object) -> str:  # noqa: PLR0915 - one cohesive pump
    """Reader keeps only the newest frame; the processor analyses it. Returns the close reason."""
    from eye_blink.service import StreamSession  # noqa: PLC0415

    assert isinstance(session, StreamSession)
    settings = state.settings
    # Token bucket: sustained rate <= stream_max_fps, but tolerant of network jitter and small bursts.
    burst = max(2.0, settings.stream_max_fps / 5.0)
    deadline = time.monotonic() + settings.stream_max_duration_s
    latest: list[tuple[bytes, float]] = []  # single-slot mailbox: (frame, monotonic receive time)
    have_frame = asyncio.Event()
    outcome: list[str] = []

    async def reader() -> None:
        tokens = burst
        last_refill = time.monotonic()
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=settings.stream_idle_timeout_s)
            except TimeoutError:
                outcome.append("idle_timeout")
                have_frame.set()
                return
            if message["type"] == "websocket.disconnect":
                outcome.append("client_closed")
                have_frame.set()
                return
            if (text := message.get("text")) is not None:
                if text.strip() == "ping":
                    await websocket.send_text("pong")
                continue
            data = message.get("bytes")
            if data is None:
                continue
            if len(data) > settings.stream_max_frame_bytes:
                STREAM_FRAMES_DROPPED.labels(reason="too_big").inc()
                outcome.append("too_big")
                have_frame.set()
                return
            now = time.monotonic()
            tokens = min(burst, tokens + (now - last_refill) * settings.stream_max_fps)
            last_refill = now
            if tokens < 1.0:
                STREAM_FRAMES_DROPPED.labels(reason="rate_limited").inc()
                continue
            tokens -= 1.0
            if latest:
                STREAM_FRAMES_DROPPED.labels(reason="stale").inc()  # processor is behind: keep the newest
            latest[:] = [(data, now)]
            have_frame.set()

    reader_task = asyncio.create_task(reader())
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "max_duration"
            try:
                await asyncio.wait_for(have_frame.wait(), timeout=remaining)
            except TimeoutError:
                return "max_duration"
            have_frame.clear()
            if outcome and not latest:
                return outcome[0]
            if not latest:
                continue
            frame, received_at = latest.pop()
            try:
                ts, face, closure, events = await anyio.to_thread.run_sync(session.process, frame)
            except AppError as exc:
                await websocket.send_json({"type": "error", "code": exc.code, "detail": exc.detail})
                continue
            await websocket.send_json(
                {
                    "type": "frame",
                    "t_ms": ts,
                    "face_found": face,
                    "closure": None if closure is None else round(closure, 3),
                    "blinks": session.blink_count,
                    "server_ms": round((time.monotonic() - received_at) * 1000.0, 1),
                }
            )
            for e in events:
                await websocket.send_json(
                    {
                        "type": e.kind.value,
                        "start_ms": round(e.start_ms, 1),
                        "end_ms": round(e.end_ms, 1),
                        "duration_ms": round(e.duration_ms, 1),
                        "blinks": session.blink_count,
                    }
                )
            if outcome and not latest:
                return outcome[0]
    finally:
        reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader_task
