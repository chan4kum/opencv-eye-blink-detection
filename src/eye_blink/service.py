"""Inference services shared by the HTTP API, the WebSocket stream and the queue workers."""

from __future__ import annotations

import asyncio
import queue
import time
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import numpy as np

from eye_blink.blink import BlinkDetector, BlinkEvent, EventKind
from eye_blink.errors import AppError, InvalidImageError, OverloadedError
from eye_blink.imaging import validate_and_decode
from eye_blink.landmarker import EyeLandmarker, EyeState, LandmarkerPool, bgr_to_rgb
from eye_blink.metrics import (
    BLINKS_DETECTED,
    FRAMES_ANALYZED,
    IMAGES_REJECTED,
    INFERENCE_DURATION,
    INFERENCE_REJECTED,
    VIDEO_ANALYSIS_DURATION,
)
from eye_blink.schemas import (
    BlinkOut,
    ImageInfo,
    ModelInfo,
    VideoAnalysisResult,
    VideoInfoOut,
)
from eye_blink.telemetry import get_tracer
from eye_blink.video import analyze_video, sniff_container, temporary_video_file

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from eye_blink.config import Settings

MODEL_NAME = "mediapipe-face-landmarker-v2"


class BlinkService:
    """Bounded-concurrency wrapper around the landmarker pool and the video analyser."""

    def __init__(self, settings: Settings, pool: LandmarkerPool | None = None) -> None:
        self._settings = settings
        self._pool = (
            pool if pool is not None else LandmarkerPool(settings.model_path, settings.max_concurrent_inference)
        )
        self._video_slots = asyncio.Semaphore(settings.max_concurrent_inference)
        self._model = ModelInfo(name=MODEL_NAME, sha256=settings.model_sha256, signal=settings.signal)

    @property
    def model(self) -> ModelInfo:
        return self._model

    def warmup(self) -> None:
        """One dummy inference so the first real request does not pay initialisation cost."""
        with self._pool.acquire(self._settings.inference_queue_timeout_s * 10) as lm:
            lm.analyze(np.zeros((256, 256, 3), dtype=np.uint8))

    def close(self) -> None:
        self._pool.close()

    # -- single image -----------------------------------------------------------------
    async def eye_state(self, data: bytes, *, source: str) -> tuple[ImageInfo, EyeState | None, float]:
        try:
            image, state, elapsed = await anyio.to_thread.run_sync(self._image_state, data)
        except AppError as exc:
            IMAGES_REJECTED.labels(reason=exc.code).inc()
            raise
        except queue.Empty:
            INFERENCE_REJECTED.labels(reason="queue_timeout").inc()
            raise OverloadedError("inference capacity exhausted, retry shortly", headers={"Retry-After": "1"}) from None
        INFERENCE_DURATION.labels(source=source).observe(elapsed)
        FRAMES_ANALYZED.labels(source=source, face=str(state is not None).lower()).inc()
        return ImageInfo(width=image.shape[1], height=image.shape[0]), state, elapsed * 1000.0

    def _image_state(self, data: bytes) -> tuple[NDArray[np.uint8], EyeState | None, float]:
        image = validate_and_decode(data, max_pixels=self._settings.max_image_pixels)
        with self._pool.acquire(self._settings.inference_queue_timeout_s) as lm:
            start = time.perf_counter()
            state = lm.analyze(bgr_to_rgb(image))
            return image, state, time.perf_counter() - start

    # -- video -----------------------------------------------------------------------
    async def analyze_video_bytes(self, data: bytes, *, source: str) -> VideoAnalysisResult:
        """Validate and analyse a video held in memory (used by the worker)."""
        try:
            await asyncio.wait_for(self._video_slots.acquire(), timeout=self._settings.inference_queue_timeout_s)
        except TimeoutError:
            INFERENCE_REJECTED.labels(reason="queue_timeout").inc()
            raise OverloadedError("analysis capacity exhausted, retry shortly", headers={"Retry-After": "1"}) from None
        try:
            with get_tracer().start_as_current_span("analyze_video") as span:
                started = time.perf_counter()
                try:
                    result = await anyio.to_thread.run_sync(self._analyze_video, data)
                except AppError as exc:
                    IMAGES_REJECTED.labels(reason=exc.code).inc()
                    raise
                span.set_attribute("video.blinks", result.blink_count)
                span.set_attribute("video.frames_analyzed", result.video.frames_analyzed)
        finally:
            self._video_slots.release()
        elapsed = time.perf_counter() - started
        VIDEO_ANALYSIS_DURATION.observe(elapsed)
        BLINKS_DETECTED.labels(source=source, kind=EventKind.BLINK.value).inc(result.blink_count)
        BLINKS_DETECTED.labels(source=source, kind=EventKind.LONG_CLOSURE.value).inc(result.long_closure_count)
        return result

    def _analyze_video(self, data: bytes) -> VideoAnalysisResult:
        container = sniff_container(data)
        started = time.perf_counter()
        with temporary_video_file(data, container) as path:
            analysis = analyze_video(Path(path), self._settings)
        info = analysis.info
        return VideoAnalysisResult(
            video=VideoInfoOut(
                width=info.width,
                height=info.height,
                fps=round(info.fps, 3),
                duration_s=round(analysis.analyzed_span_s, 3),
                frames_decoded=analysis.frames_decoded,
                frames_analyzed=analysis.frames_analyzed,
            ),
            face_found_ratio=round(analysis.face_found_ratio, 4),
            blink_count=analysis.blink_count,
            long_closure_count=analysis.long_closure_count,
            discarded_closures=analysis.discarded_count,
            blinks_per_minute=None if analysis.blinks_per_minute is None else round(analysis.blinks_per_minute, 2),
            events=[BlinkOut.from_event(e) for e in analysis.events],
            processing_ms=round((time.perf_counter() - started) * 1000.0, 1),
            model=self._model,
        )

    # -- live stream -----------------------------------------------------------------
    def new_stream_session(self) -> StreamSession:
        return StreamSession(self._settings)


class StreamSession:
    """One live WebSocket session: its own tracking landmarker and blink detector. Not thread-safe;
    call :meth:`process` from one worker thread at a time (the handler serialises calls)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._landmarker = EyeLandmarker(
            settings.model_path, video=True, min_detection_confidence=settings.min_detection_confidence
        )
        self._detector = BlinkDetector(settings.blink_config)
        self._started = time.monotonic()
        self._last_ts = -1

    @property
    def blink_count(self) -> int:
        return self._detector.blink_count

    def process(self, jpeg_or_png: bytes) -> tuple[int, bool, float | None, list[BlinkEvent]]:
        """Analyse one frame stamped with *server* receive time (client clocks are not trusted)."""
        image = validate_and_decode(jpeg_or_png, max_pixels=self._settings.max_image_pixels)
        if image is None:  # pragma: no cover - validate_and_decode raises instead
            raise InvalidImageError("undecodable frame")
        ts = max(int((time.monotonic() - self._started) * 1000.0), self._last_ts + 1)
        self._last_ts = ts
        state = self._landmarker.analyze(bgr_to_rgb(image), ts)
        closure = None if state is None else state.closure_for(self._settings.signal)
        events = self._detector.update(float(ts), closure)
        FRAMES_ANALYZED.labels(source="stream", face=str(state is not None).lower()).inc()
        for e in events:
            BLINKS_DETECTED.labels(source="stream", kind=e.kind.value).inc()
        return ts, state is not None, closure, events

    def close(self) -> None:
        self._landmarker.close()
