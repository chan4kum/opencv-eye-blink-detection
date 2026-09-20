from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, ClassVar

import cv2
import httpx
import numpy as np
import pytest

from eye_blink.api.app import create_app
from eye_blink.config import Settings
from eye_blink.landmarker import EyeState

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "models" / "face_landmarker.task"
DATA = Path(__file__).resolve().parent / "data"

# Real MediaPipe cannot initialise in some macOS sandboxes; those tests run in Linux (Docker / CI).
REAL_MEDIAPIPE = os.environ.get("EB_TEST_REAL_MEDIAPIPE") == "1"
requires_mediapipe = pytest.mark.skipif(not REAL_MEDIAPIPE, reason="set EB_TEST_REAL_MEDIAPIPE=1 (Linux/Docker)")


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"environment": "test", "log_json": False, "log_level": "WARNING", "model_path": MODEL}
    return Settings(**{**base, **overrides})


# ---------------------------------------------------------------------------------------------
# Deterministic fake landmarker: a frame's pixels encode the eye closure, so tests control the signal.
#   red channel   = closure * 255
#   green channel = face present (255) / absent (0)
# ---------------------------------------------------------------------------------------------
def make_frame(closure: float | None, size: int = 96) -> np.ndarray:
    """BGR frame encoding ``closure`` (``None`` = no face)."""
    img = np.zeros((size, size, 3), np.uint8)
    if closure is not None:
        img[:, :, 1] = 255
        img[:, :, 2] = round(closure * 255)
    return img


def encode_frame(closure: float | None, fmt: str = ".png") -> bytes:
    ok, buf = cv2.imencode(fmt, make_frame(closure))
    assert ok
    return bytes(buf.tobytes())


def make_video(path: Path, closures: list[float | None], fps: float = 30, codec: str = "mp4v") -> Path:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*codec), fps, (96, 96))
    assert writer.isOpened()
    for c in closures:
        writer.write(make_frame(c))
    writer.release()
    return path


def blink_track(fps: int, seconds: float, blinks: list[tuple[float, float]]) -> list[float | None]:
    """Closure track (0/1) with rectangular blinks given as (start_s, length_s)."""
    n = int(fps * seconds)
    return [1.0 if any(s <= i / fps < s + ln for s, ln in blinks) else 0.0 for i in range(n)]


class FakeLandmarker:
    instances: ClassVar[list[FakeLandmarker]] = []

    def __init__(self, *_: Any, video: bool = False, **__: Any) -> None:
        self.video = video
        self.closed = False
        self.last_ts = -1
        self.calls = 0
        FakeLandmarker.instances.append(self)

    def analyze(self, image_rgb: np.ndarray, timestamp_ms: int | None = None) -> EyeState | None:
        if self.video:
            assert timestamp_ms is not None and timestamp_ms > self.last_ts, "timestamps must strictly increase"
            self.last_ts = timestamp_ms
        self.calls += 1
        px = image_rgb[0, 0]
        if px[1] < 128:
            return None
        c = float(px[0]) / 255.0
        return EyeState(blink_left=c, blink_right=c, ear_left=0.30 - 0.2 * c, ear_right=0.30 - 0.2 * c)

    def close(self) -> None:
        self.closed = True


class FakePool:
    def __init__(self, _model_path: Path, size: int) -> None:
        self._lms = [FakeLandmarker(video=False) for _ in range(size)]
        self.closed = False

    @contextmanager
    def acquire(self, timeout: float) -> Iterator[FakeLandmarker]:
        yield self._lms[0]

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_mediapipe(monkeypatch: pytest.MonkeyPatch) -> type[FakeLandmarker]:
    FakeLandmarker.instances = []
    monkeypatch.setattr("eye_blink.service.EyeLandmarker", FakeLandmarker)
    monkeypatch.setattr("eye_blink.video.EyeLandmarker", FakeLandmarker)
    monkeypatch.setattr("eye_blink.cli.EyeLandmarker", FakeLandmarker)
    monkeypatch.setattr("eye_blink.service.LandmarkerPool", FakePool)
    return FakeLandmarker


@pytest.fixture
def make_client(fake_mediapipe: type[FakeLandmarker]) -> Callable[..., Any]:
    """Factory returning an async context manager yielding an httpx client bound to a fresh app (fake model)."""

    @asynccontextmanager
    async def _make(**overrides: Any) -> AsyncIterator[httpx.AsyncClient]:
        app = create_app(make_settings(**overrides))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            client.app = app  # type: ignore[attr-defined]
            yield client

    return _make


@pytest.fixture
async def client(make_client: Callable[..., Any]) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client() as c:
        yield c
