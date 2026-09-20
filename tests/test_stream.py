"""WebSocket live-stream protocol, driven with Starlette's TestClient against a fake landmarker."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient, WebSocketDisconnect

from eye_blink.api.app import create_app
from eye_blink.config import hash_api_key
from tests.conftest import FakeLandmarker, encode_frame, make_settings

KEY = "stream-key"


def client_for(**overrides: Any) -> TestClient:
    return TestClient(create_app(make_settings(**{"stream_max_fps": 120, **overrides})))


@pytest.fixture
def tc(fake_mediapipe: type[FakeLandmarker]) -> Iterator[TestClient]:
    with client_for() as c:
        yield c


def drain_until(ws: Any, predicate: Any, limit: int = 50) -> list[dict[str, Any]]:
    seen = []
    for _ in range(limit):
        msg = ws.receive_json()
        seen.append(msg)
        if predicate(msg):
            return seen
    raise AssertionError(f"predicate never matched; saw {seen[-3:]}")


def send_track(ws: Any, closures: list[float | None], step_s: float = 0.05) -> list[dict[str, Any]]:
    """Send frames at a steady cadence, reading each response (one per frame)."""
    out = []
    for c in closures:
        ws.send_bytes(encode_frame(c))
        out.append(ws.receive_json())
        time.sleep(step_s)
    return out


def test_blink_detected_end_to_end(tc: TestClient) -> None:
    """open, closed for ~180 ms, open -> exactly one `blink` message, sent after its closing frame."""
    with tc.websocket_connect("/v1/stream") as ws:
        for c in [0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0]:
            ws.send_bytes(encode_frame(c))
            time.sleep(0.05)
        msgs = drain_until(ws, lambda m: m["type"] == "blink")
        frames = [m for m in msgs if m["type"] == "frame"]
        blink = msgs[-1]
    assert len(frames) >= 6 and all(f["face_found"] for f in frames)
    assert all(f["server_ms"] >= 0 for f in frames)
    assert blink["blinks"] == 1 and 100 <= blink["duration_ms"] <= 400 and blink["end_ms"] > blink["start_ms"]
    assert any(f["closure"] is not None and f["closure"] > 0.9 for f in frames)


def test_no_face_frames_report_face_found_false(tc: TestClient) -> None:
    with tc.websocket_connect("/v1/stream") as ws:
        r = send_track(ws, [None, None])
    assert all(m["type"] == "frame" and m["face_found"] is False and m["closure"] is None for m in r)


def test_ping_pong(tc: TestClient) -> None:
    with tc.websocket_connect("/v1/stream") as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "pong"


def test_invalid_frame_yields_error_message_and_session_continues(fake_mediapipe: type[FakeLandmarker]) -> None:
    with client_for(stream_idle_timeout_s=3) as c, c.websocket_connect("/v1/stream") as ws:
        ws.send_bytes(b"this is not an image")
        err = ws.receive_json()
        assert err["type"] == "error" and err["code"] == "invalid-image"
        time.sleep(0.05)  # stay under the configured frame rate so the next frame is not rate-limited
        ws.send_bytes(encode_frame(0.0))
        assert ws.receive_json()["type"] == "frame"


def test_oversized_frame_closes_with_1009(fake_mediapipe: type[FakeLandmarker]) -> None:
    with client_for(stream_max_frame_bytes=2048) as c, c.websocket_connect("/v1/stream") as ws:
        ws.send_bytes(b"x" * 5000)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1009


def test_idle_timeout_closes_session(fake_mediapipe: type[FakeLandmarker]) -> None:
    with client_for(stream_idle_timeout_s=0.3) as c, c.websocket_connect("/v1/stream") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1000


def test_max_duration_closes_session(fake_mediapipe: type[FakeLandmarker]) -> None:
    with client_for(stream_max_duration_s=0.4) as c, c.websocket_connect("/v1/stream") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1000


def test_capacity_limit_closes_with_1013_after_accepting(fake_mediapipe: type[FakeLandmarker]) -> None:
    """At capacity the socket is accepted then closed with 1013, so clients can distinguish it from an auth failure."""
    with client_for(stream_max_sessions=1) as c, c.websocket_connect("/v1/stream"):
        with c.websocket_connect("/v1/stream") as second, pytest.raises(WebSocketDisconnect) as exc:
            second.receive_json()
        assert exc.value.code == 1013 and "capacity" in exc.value.reason


def test_session_slot_and_landmarker_are_released_on_disconnect(fake_mediapipe: type[FakeLandmarker]) -> None:
    with client_for(stream_max_sessions=1) as c:
        with c.websocket_connect("/v1/stream") as ws:
            ws.send_bytes(encode_frame(0.0))
            ws.receive_json()
        for _ in range(50):  # server-side cleanup is asynchronous
            if all(lm.closed for lm in fake_mediapipe.instances if lm.video):
                break
            time.sleep(0.05)
        assert all(lm.closed for lm in fake_mediapipe.instances if lm.video)
        with c.websocket_connect("/v1/stream") as ws2:  # the single slot is free again
            ws2.send_text("ping")
            assert ws2.receive_text() == "pong"


class TestStreamAuth:
    @pytest.fixture
    def secured(self, fake_mediapipe: type[FakeLandmarker]) -> Iterator[TestClient]:
        with client_for(api_key_hashes=frozenset({hash_api_key(KEY)})) as c:
            yield c

    def test_no_credentials_rejected(self, secured: TestClient) -> None:
        with pytest.raises(WebSocketDisconnect) as exc, secured.websocket_connect("/v1/stream"):
            pass
        assert exc.value.code == 1008

    def test_wrong_key_rejected(self, secured: TestClient) -> None:
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            secured.websocket_connect("/v1/stream", headers={"Authorization": "Bearer wrong"}),
        ):
            pass
        assert exc.value.code == 1008

    def test_bearer_header_accepted(self, secured: TestClient) -> None:
        with secured.websocket_connect("/v1/stream", headers={"Authorization": f"Bearer {KEY}"}) as ws:
            ws.send_text("ping")
            assert ws.receive_text() == "pong"

    def test_browser_subprotocol_accepted(self, secured: TestClient) -> None:
        with secured.websocket_connect("/v1/stream", subprotocols=["bearer", KEY]) as ws:
            assert ws.accepted_subprotocol == "bearer"
            ws.send_text("ping")
            assert ws.receive_text() == "pong"

    def test_key_in_query_string_is_not_accepted(self, secured: TestClient) -> None:
        with pytest.raises(WebSocketDisconnect) as exc, secured.websocket_connect(f"/v1/stream?token={KEY}"):
            pass
        assert exc.value.code == 1008


def test_rate_limit_drops_excess_frames(fake_mediapipe: type[FakeLandmarker]) -> None:
    with client_for(stream_max_fps=5) as c, c.websocket_connect("/v1/stream") as ws:
        for _ in range(20):  # burst far above 5 fps
            ws.send_bytes(encode_frame(0.0))
        time.sleep(0.5)
        ws.send_text("ping")
        got = []
        while True:
            m = ws.receive()
            text = m.get("text")
            if text == "pong":
                break
            got.append(json.loads(text))
    frames = [g for g in got if g["type"] == "frame"]
    assert 1 <= len(frames) < 20


def test_jittery_client_at_the_nominal_rate_is_not_rate_limited(fake_mediapipe: type[FakeLandmarker]) -> None:
    """Regression: a strict minimum inter-frame gap dropped ~35% of frames from a real 30 fps client."""
    import random

    from eye_blink.metrics import STREAM_FRAMES_DROPPED

    counter = STREAM_FRAMES_DROPPED.labels(reason="rate_limited")
    before = counter._value.get()
    rng = random.Random(7)
    with client_for(stream_max_fps=30) as c, c.websocket_connect("/v1/stream") as ws:
        answered = 0
        for _ in range(60):
            ws.send_bytes(encode_frame(0.0))
            time.sleep(max(0.0, 1 / 30 + rng.uniform(-0.012, 0.012)))  # 30 fps with +-12 ms jitter
        ws.send_text("ping")
        while True:
            m = ws.receive()
            if m.get("text") == "pong":
                break
            answered += json.loads(m["text"])["type"] == "frame"
    assert counter._value.get() - before <= 2
    assert answered >= 55
