"""MediaPipe Face Landmarker wrapper producing per-frame eye-closure signals.

MediaPipe is imported lazily so that modules that only need the pure blink logic (and their tests)
work on platforms where the MediaPipe native runtime is unavailable.
"""

from __future__ import annotations

import hashlib
import queue
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import cv2
import numpy as np
from numpy.typing import NDArray

# Landmark indices of the MediaPipe 478-point face mesh: (outer, upper1, upper2, inner, lower2, lower1)
RIGHT_EYE = (33, 160, 158, 133, 153, 144)
LEFT_EYE = (362, 385, 387, 263, 373, 380)

# Eye aspect ratio calibration used only for the fallback/diagnostic signal (see ``ear_to_closure``).
EAR_OPEN = 0.30
EAR_CLOSED = 0.10


class SignalMode(StrEnum):
    """Which per-frame eye-closure signal drives blink detection."""

    BLENDSHAPE = "blendshape"  # MediaPipe eyeBlink blendshapes: trained on real faces, robust to gaze/pose (default)
    EAR = "ear"  # landmark geometry only: model-agnostic, but drops when the subject looks down
    MAX = "max"  # the larger of the two: most sensitive, most false positives


class ModelIntegrityError(RuntimeError):
    """The model file is missing or does not match the pinned checksum."""


class LandmarkerUnavailableError(RuntimeError):
    """The MediaPipe runtime could not be initialised on this platform."""


@dataclass(frozen=True, slots=True)
class EyeState:
    """Eye state of the (single, most prominent) face in one frame."""

    blink_left: float  # MediaPipe eyeBlinkLeft blendshape, 0 open .. 1 closed
    blink_right: float
    ear_left: float  # eye aspect ratio from landmarks (diagnostic)
    ear_right: float

    @property
    def closure(self) -> float:
        """Primary signal: mean of both eyes' blink blendshapes."""
        return float(np.clip((self.blink_left + self.blink_right) / 2.0, 0.0, 1.0))

    def closure_for(self, mode: SignalMode) -> float:
        if mode is SignalMode.BLENDSHAPE:
            return self.closure
        if mode is SignalMode.EAR:
            return self.closure_from_ear
        return max(self.closure, self.closure_from_ear)

    @property
    def closure_from_ear(self) -> float:
        """Fallback signal derived from landmark geometry."""
        return ear_to_closure((self.ear_left + self.ear_right) / 2.0)


def bgr_to_rgb(image_bgr: NDArray[np.uint8]) -> NDArray[np.uint8]:
    return cast("NDArray[np.uint8]", cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB))


def ear_to_closure(ear: float) -> float:
    """Map an eye aspect ratio linearly onto [0, 1] closure between the calibrated open/closed values."""
    return float(np.clip((EAR_OPEN - ear) / (EAR_OPEN - EAR_CLOSED), 0.0, 1.0))


def eye_aspect_ratio(pts: NDArray[np.float64], idx: tuple[int, int, int, int, int, int]) -> float:
    """EAR = (|p2-p6| + |p3-p5|) / (2 |p1-p4|), computed on pixel-space coordinates."""
    p1, p2, p3, p4, p5, p6 = (pts[i] for i in idx)
    horizontal = float(np.linalg.norm(p1 - p4))
    if horizontal < 1e-6:
        return 0.0
    return float((np.linalg.norm(p2 - p6) + np.linalg.norm(p3 - p5)) / (2.0 * horizontal))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model(path: Path, expected_sha256: str | None) -> None:
    if not path.is_file():
        raise ModelIntegrityError(f"model file not found: {path}")
    if expected_sha256 is not None:
        actual = sha256_file(path)
        if actual != expected_sha256:
            raise ModelIntegrityError(f"model checksum mismatch for {path}: expected {expected_sha256}, got {actual}")


class EyeLandmarker:
    """One MediaPipe FaceLandmarker instance. **Not thread-safe**: use one per thread, or a pool."""

    def __init__(self, model_path: Path, *, video: bool, min_detection_confidence: float = 0.5) -> None:
        try:
            import mediapipe as mp  # noqa: PLC0415
            from mediapipe.tasks import python as mpp  # noqa: PLC0415
            from mediapipe.tasks.python import vision  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover - import failure is platform specific
            raise LandmarkerUnavailableError(f"MediaPipe could not be imported: {exc}") from exc
        self._mp: Any = mp
        self._video = video
        options = vision.FaceLandmarkerOptions(
            base_options=mpp.BaseOptions(model_asset_path=str(model_path), delegate=mpp.BaseOptions.Delegate.CPU),
            running_mode=vision.RunningMode.VIDEO if video else vision.RunningMode.IMAGE,
            num_faces=1,
            min_face_detection_confidence=min_detection_confidence,
            min_face_presence_confidence=min_detection_confidence,
            min_tracking_confidence=min_detection_confidence,
            output_face_blendshapes=True,
        )
        self._landmarker: Any = vision.FaceLandmarker.create_from_options(options)
        self._last_ts = -1

    def analyze(self, image_rgb: NDArray[np.uint8], timestamp_ms: int | None = None) -> EyeState | None:
        """Return the eye state, or ``None`` if no face was found.

        In video mode ``timestamp_ms`` must be strictly increasing across calls.
        """
        if image_rgb.ndim != 3 or image_rgb.shape[2] != 3 or image_rgb.dtype != np.uint8:
            raise ValueError("expected an HxWx3 uint8 RGB image")
        mp_image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=np.ascontiguousarray(image_rgb))
        if self._video:
            if timestamp_ms is None:
                raise ValueError("video mode requires timestamp_ms")
            if timestamp_ms <= self._last_ts:
                raise ValueError(f"timestamp_ms must strictly increase (got {timestamp_ms} after {self._last_ts})")
            self._last_ts = timestamp_ms
            result = self._landmarker.detect_for_video(mp_image, timestamp_ms)
        else:
            result = self._landmarker.detect(mp_image)
        if not result.face_landmarks:
            return None

        h, w = image_rgb.shape[:2]
        pts = np.array([[lm.x * w, lm.y * h] for lm in result.face_landmarks[0]], dtype=np.float64)
        scores = {b.category_name: float(b.score) for b in result.face_blendshapes[0]}
        return EyeState(
            blink_left=scores.get("eyeBlinkLeft", 0.0),
            blink_right=scores.get("eyeBlinkRight", 0.0),
            # MediaPipe's "left"/"right" blendshapes are from the subject's perspective; the landmark
            # index sets below follow the same convention (subject's right eye = RIGHT_EYE).
            ear_left=eye_aspect_ratio(pts, LEFT_EYE),
            ear_right=eye_aspect_ratio(pts, RIGHT_EYE),
        )

    def close(self) -> None:
        self._landmarker.close()


class LandmarkerPool:
    """Fixed-size pool of image-mode landmarkers for concurrent request handling."""

    def __init__(self, model_path: Path, size: int) -> None:
        self._all = [EyeLandmarker(model_path, video=False) for _ in range(size)]
        self._free: queue.Queue[EyeLandmarker] = queue.Queue()
        for lm in self._all:
            self._free.put(lm)
        self._closed = threading.Event()

    @contextmanager
    def acquire(self, timeout: float) -> Iterator[EyeLandmarker]:
        """Borrow a landmarker (blocking up to ``timeout`` s; raises ``queue.Empty`` on timeout)."""
        lm = self._free.get(timeout=timeout)
        try:
            yield lm
        finally:
            self._free.put(lm)

    def close(self) -> None:
        if not self._closed.is_set():
            self._closed.set()
            for lm in self._all:
                lm.close()
