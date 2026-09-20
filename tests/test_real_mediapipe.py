"""Tests against the real MediaPipe Face Landmarker. Linux only (``EB_TEST_REAL_MEDIAPIPE=1``).

Ground truth comes from *synthetic* blink videos (a public-domain portrait whose eyelids are moved on a
known schedule, see ``scripts/make_fixture_video.py``). They validate the pipeline and thresholds end to
end; they do not measure accuracy on real people.
"""

from __future__ import annotations

import json
import threading
import time

import cv2
import numpy as np
import pytest
from starlette.testclient import TestClient

from eye_blink.api.app import create_app
from eye_blink.landmarker import EyeLandmarker, LandmarkerPool, bgr_to_rgb
from eye_blink.video import analyze_video
from tests.conftest import DATA, MODEL, make_settings, requires_mediapipe

pytestmark = requires_mediapipe


def load_rgb(name: str) -> np.ndarray:
    img = cv2.imread(str(DATA / name))
    assert img is not None
    return bgr_to_rgb(img)


@pytest.fixture(scope="module")
def image_landmarker() -> EyeLandmarker:
    lm = EyeLandmarker(MODEL, video=False)
    yield lm  # type: ignore[misc]
    lm.close()


def test_real_face_with_open_eyes(image_landmarker: EyeLandmarker) -> None:
    state = image_landmarker.analyze(load_rgb("astronaut.jpg"))
    assert state is not None
    assert state.closure < 0.2, "open eyes must read as open"
    assert 0.24 < state.ear_left < 0.40 and 0.24 < state.ear_right < 0.40
    assert state.closure_from_ear < 0.15


def test_no_face_returns_none(image_landmarker: EyeLandmarker) -> None:
    rng = np.random.default_rng(0)
    assert image_landmarker.analyze(rng.integers(0, 255, (240, 320, 3), dtype=np.uint8)) is None
    assert image_landmarker.analyze(np.zeros((64, 64, 3), np.uint8)) is None


def test_wrong_array_shape_is_rejected(image_landmarker: EyeLandmarker) -> None:
    with pytest.raises(ValueError, match="HxWx3 uint8"):
        image_landmarker.analyze(np.zeros((10, 10), np.uint8))


def test_video_mode_requires_increasing_timestamps() -> None:
    lm = EyeLandmarker(MODEL, video=True)
    try:
        img = load_rgb("astronaut.jpg")
        lm.analyze(img, 100)
        with pytest.raises(ValueError, match="strictly increase"):
            lm.analyze(img, 100)
        with pytest.raises(ValueError, match="requires timestamp_ms"):
            lm.analyze(img, None)
    finally:
        lm.close()


def truth(name: str) -> dict[str, object]:
    return json.loads((DATA / f"{name}.json").read_text())  # type: ignore[no-any-return]


def test_synthetic_video_blinks_match_ground_truth() -> None:
    """EAR signal: all 4 scheduled blinks found, none invented, timing within a tolerance."""
    res = analyze_video(DATA / "blinks_synthetic.mp4", make_settings(signal="ear"))
    scheduled = truth("blinks_synthetic")["blinks"]
    assert res.blink_count == len(scheduled) == 4 and res.long_closure_count == 0
    for event, sched in zip(res.events, scheduled, strict=True):  # type: ignore[arg-type]
        assert abs(event.start_ms / 1000 - sched["start_s"]) < 0.25
        assert 80 <= event.duration_ms <= 300
        assert event.peak_closure > 0.8
    assert res.face_found_ratio == 1.0 and res.frames_analyzed == 300


def test_synthetic_video_without_blinks_has_no_events() -> None:
    res = analyze_video(DATA / "no_blinks_synthetic.mp4", make_settings(signal="ear"))
    assert res.blink_count == 0 and res.long_closure_count == 0 and res.events == []


def test_synthetic_long_closure_is_not_counted_as_a_blink() -> None:
    res = analyze_video(DATA / "long_closure_synthetic.mp4", make_settings(signal="ear"))
    assert res.blink_count == 0 and res.long_closure_count == 1 and res.events[0].duration_ms > 700


def test_blendshape_signal_responds_to_closure_even_though_synthetic_eyes_stay_below_threshold() -> None:
    """The learned blendshape rises clearly on closed eyes; absolute values on synthetic eyes are lower than on real ones."""
    lm = EyeLandmarker(MODEL, video=True)
    try:
        cap = cv2.VideoCapture(str(DATA / "blinks_synthetic.mp4"))
        values = []
        i = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            state = lm.analyze(bgr_to_rgb(frame), int(i * 1000 / 30))
            assert state is not None
            values.append(state.closure)
            i += 1
    finally:
        lm.close()
    assert max(values) > 0.3, "blendshape must clearly rise on closed eyes"
    assert np.percentile(values, 50) < 0.15  # mostly open


def test_landmarker_pool_is_safe_under_concurrency() -> None:
    pool = LandmarkerPool(MODEL, size=2)
    img = load_rgb("astronaut.jpg")
    results: list[bool] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for _ in range(5):
                with pool.acquire(timeout=30) as lm:
                    results.append(lm.analyze(img) is not None)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    pool.close()
    assert not errors and len(results) == 40 and all(results)


def test_eye_state_endpoint_with_real_model() -> None:
    with TestClient(create_app(make_settings())) as c:
        ok, buf = cv2.imencode(".jpg", cv2.imread(str(DATA / "astronaut.jpg")))
        assert ok
        body = c.post("/v1/eye-state", content=buf.tobytes(), headers={"content-type": "image/jpeg"}).json()
    assert body["face_found"] is True and body["eyes_closed"] is False and body["closure"] < 0.2


def test_live_stream_with_real_model_detects_the_scheduled_blink() -> None:
    """Replays part of the synthetic video over the WebSocket at ~15 fps: expect exactly one blink (the one at 2.0 s)."""
    cap = cv2.VideoCapture(str(DATA / "blinks_synthetic.mp4"))
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    window = frames[45:100:2]  # t = 1.5 .. 3.3 s, every other frame (15 fps)
    settings = make_settings(signal="ear", stream_max_fps=30)
    blinks = []
    with TestClient(create_app(settings)) as c, c.websocket_connect("/v1/stream") as ws:
        for frame in window:
            ok, buf = cv2.imencode(".jpg", frame)
            ws.send_bytes(buf.tobytes())
            time.sleep(1 / 15)
        ws.send_text("ping")
        while True:
            m = ws.receive()
            if m.get("text") == "pong":
                break
            msg = json.loads(m["text"])
            if msg["type"] == "blink":
                blinks.append(msg)
    assert len(blinks) == 1 and 80 <= blinks[0]["duration_ms"] <= 350
