"""Untrusted-video ingestion and analysis.

Defence in depth for decoding attacker-controlled media with FFmpeg (via OpenCV):

1. **Container allow-list** from magic bytes (MP4/MOV, WebM/Matroska, AVI); the client's Content-Type is ignored.
2. **Header limits** (resolution, duration) checked before any frame is decoded.
3. **Runtime limits** that do not trust the header: a hard cap on decoded frames and a wall-clock budget.
4. Decoding happens from a private temporary file that is always removed.
"""

from __future__ import annotations

import contextlib
import math
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import cv2

from eye_blink.blink import BlinkDetector, BlinkEvent, EventKind
from eye_blink.errors import InvalidVideoError, UnsupportedMediaTypeError, VideoLimitError
from eye_blink.landmarker import EyeLandmarker, bgr_to_rgb

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from eye_blink.config import Settings

MIN_FACE_TIME_FOR_RATE_S = 5.0


def sniff_container(data: bytes) -> str:
    """Return ``mp4`` / ``webm`` / ``avi`` from magic bytes, or raise ``UnsupportedMediaTypeError``."""
    head = data[:16]
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "avi"
    raise UnsupportedMediaTypeError("unsupported video container; allowed: MP4/MOV, WebM/Matroska, AVI")


@contextlib.contextmanager
def temporary_video_file(data: bytes, container: str) -> Iterator[Path]:
    """Write bytes to a private (0600) temp file and always remove it afterwards."""
    with tempfile.NamedTemporaryFile(suffix=f".{container}", delete=False) as fh:
        path = Path(fh.name)
        try:
            fh.write(data)
            fh.flush()
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class VideoInfo:
    width: int
    height: int
    fps: float
    frame_count: int  # from the header; may be 0/unknown
    duration_s: float


@dataclass(frozen=True, slots=True)
class VideoAnalysis:
    info: VideoInfo
    frames_decoded: int
    frames_analyzed: int
    frames_with_face: int
    analyzed_span_s: float
    events: list[BlinkEvent]
    blink_count: int
    long_closure_count: int
    discarded_count: int

    @property
    def face_found_ratio(self) -> float:
        return self.frames_with_face / self.frames_analyzed if self.frames_analyzed else 0.0

    @property
    def blinks_per_minute(self) -> float | None:
        """Blink rate over the time a face was visible; ``None`` when there is too little data to be meaningful."""
        if self.frames_analyzed == 0 or self.analyzed_span_s <= 0:
            return None
        face_time = self.analyzed_span_s * self.face_found_ratio
        if face_time < MIN_FACE_TIME_FOR_RATE_S:
            return None
        return self.blink_count / (face_time / 60.0)


def probe(cap: cv2.VideoCapture, settings: Settings) -> VideoInfo:
    if not cap.isOpened():
        raise InvalidVideoError("the video could not be opened or decoded")
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS))
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if width <= 0 or height <= 0:
        raise InvalidVideoError("the video has no readable frame dimensions")
    if not math.isfinite(fps) or fps < 1.0 or fps > 240.0:
        raise InvalidVideoError(f"unsupported or missing frame rate ({fps})")
    if width * height > settings.max_video_pixels:
        raise VideoLimitError(f"frames are {width}x{height}; the limit is {settings.max_video_pixels} pixels")
    duration = frames / fps if frames > 0 else 0.0
    if duration > settings.max_video_seconds:
        raise VideoLimitError(f"video is {duration:.1f}s long; the limit is {settings.max_video_seconds:.0f}s")
    return VideoInfo(width, height, fps, max(frames, 0), duration)


def analyze_video(path: Path, settings: Settings) -> VideoAnalysis:
    """Decode a video file and detect blinks. Blocking and CPU-bound: run in a worker thread."""
    cap = cv2.VideoCapture(str(path))
    landmarker: EyeLandmarker | None = None
    try:
        info = probe(cap, settings)
        landmarker = EyeLandmarker(
            settings.model_path, video=True, min_detection_confidence=settings.min_detection_confidence
        )
        detector = BlinkDetector(settings.blink_config)
        min_step_ms = 1000.0 / settings.video_analysis_fps
        deadline = time.monotonic() + settings.video_timeout_s

        events: list[BlinkEvent] = []
        decoded = analyzed = with_face = 0
        last_ts = -1
        last_analyzed_ms = -math.inf
        first_ms: float | None = None
        end_ms = 0.0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if decoded >= settings.max_video_frames:
                raise VideoLimitError(f"video has more than {settings.max_video_frames} frames")
            if time.monotonic() > deadline:
                raise VideoLimitError(f"analysis exceeded the {settings.video_timeout_s:.0f}s time budget")
            t_ms = decoded * 1000.0 / info.fps
            decoded += 1
            if t_ms - last_analyzed_ms < min_step_ms - 1e-6:
                continue  # frame-rate cap: skip frames above video_analysis_fps
            if frame.shape[0] * frame.shape[1] > settings.max_video_pixels:
                raise VideoLimitError("a frame exceeds the pixel limit")
            ts = max(int(t_ms), last_ts + 1)  # MediaPipe requires strictly increasing integer milliseconds
            last_ts = ts
            state = landmarker.analyze(bgr_to_rgb(cast("NDArray[np.uint8]", frame)), ts)
            closure = None if state is None else state.closure_for(settings.signal)
            analyzed += 1
            with_face += state is not None
            last_analyzed_ms = t_ms
            first_ms = t_ms if first_ms is None else first_ms
            end_ms = t_ms
            events += detector.update(float(ts), closure)
        events += detector.finish()
        if decoded == 0:
            raise InvalidVideoError("the video contains no decodable frames")
        span = (end_ms - (first_ms or 0.0)) / 1000.0 + 1.0 / info.fps
        return VideoAnalysis(
            info=info,
            frames_decoded=decoded,
            frames_analyzed=analyzed,
            frames_with_face=with_face,
            analyzed_span_s=span,
            events=events,
            blink_count=detector.blink_count,
            long_closure_count=detector.long_closure_count,
            discarded_count=detector.discarded_count,
        )
    finally:
        cap.release()
        if landmarker is not None:
            landmarker.close()


def event_kinds(events: list[BlinkEvent]) -> dict[str, int]:
    return {k.value: sum(1 for e in events if e.kind is k) for k in EventKind}
