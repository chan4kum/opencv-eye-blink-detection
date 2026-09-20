from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from eye_blink.config import hash_api_key
from tests.conftest import FakeLandmarker, blink_track, encode_frame, make_video

IMG = {"content-type": "image/png"}
VID = {"content-type": "video/mp4"}


async def test_health_ready_and_model(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json()["status"] == "ok"
    ready = await client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["checks"] == {"model": True}
    model = (await client.get("/v1/model")).json()
    assert model["name"].startswith("mediapipe") and model["signal"] == "blendshape"


async def test_eye_state_open_and_closed(client: httpx.AsyncClient) -> None:
    open_ = (await client.post("/v1/eye-state", content=encode_frame(0.0), headers=IMG)).json()
    assert open_["face_found"] and open_["eyes_closed"] is False and open_["closure"] == pytest.approx(0.0, abs=0.01)
    closed = (await client.post("/v1/eye-state", content=encode_frame(1.0), headers=IMG)).json()
    assert closed["eyes_closed"] is True and closed["closure"] == pytest.approx(1.0, abs=0.01)
    assert closed["blink_score_left"] == pytest.approx(1.0, abs=0.01) and closed["ear_left"] < 0.12
    assert closed["request_id"] and closed["inference_ms"] >= 0 and closed["image"] == {"width": 96, "height": 96}


async def test_eye_state_no_face(client: httpx.AsyncClient) -> None:
    body = (await client.post("/v1/eye-state", content=encode_frame(None), headers=IMG)).json()
    assert body["face_found"] is False and body["closure"] is None and body["eyes_closed"] is None


async def test_eye_state_respects_configured_threshold(make_client: Callable[..., Any]) -> None:
    async with make_client(blink_close_threshold=0.9, blink_open_threshold=0.5) as c:
        r = await c.post("/v1/eye-state", content=encode_frame(0.7), headers=IMG)
    assert r.json()["eyes_closed"] is False


@pytest.mark.parametrize(("payload", "status"), [(b"", 422), (b"garbage", 422)])
async def test_eye_state_invalid_image(client: httpx.AsyncClient, payload: bytes, status: int) -> None:
    r = await client.post("/v1/eye-state", content=payload, headers=IMG)
    assert r.status_code == status and r.headers["content-type"].startswith("application/problem+json")


async def test_wrong_content_type_is_415(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/eye-state", content=encode_frame(0.0), headers={"content-type": "text/plain"})
    assert r.status_code == 415


async def test_oversize_image_is_413(make_client: Callable[..., Any]) -> None:
    async with make_client(max_upload_bytes=2048) as c:
        r = await c.post("/v1/eye-state", content=b"x" * 5000, headers=IMG)
    assert r.status_code == 413


async def test_security_headers_request_id_and_metrics(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz", headers={"x-request-id": "req-abc-12345"})
    assert r.headers["x-request-id"] == "req-abc-12345" and r.headers["x-content-type-options"] == "nosniff"
    await client.post("/v1/eye-state", content=encode_frame(0.0), headers=IMG)
    text = (await client.get("/metrics")).text
    assert 'eb_http_requests_total{method="POST",route="/v1/eye-state",status="200"}' in text
    assert "eb_frames_analyzed_total" in text and "eb_build_info" in text


async def test_jobs_501_when_async_disabled(client: httpx.AsyncClient, tmp_path: Path) -> None:
    video = make_video(tmp_path / "v.mp4", blink_track(30, 2.0, []))
    assert (await client.post("/v1/jobs", content=video.read_bytes(), headers=VID)).status_code == 501
    assert (await client.get("/v1/jobs/3f0c7b0e-8f0d-4b7e-9f77-1c7f6d1b2a10")).status_code == 501


async def test_unknown_route_is_problem_json(client: httpx.AsyncClient) -> None:
    r = await client.get("/nope")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem+json")


class TestAuth:
    KEY = "s3cr3t-key"

    @pytest.fixture
    def secured(self, make_client: Callable[..., Any]) -> Any:
        return make_client(api_key_hashes=frozenset({hash_api_key(self.KEY)}))

    async def test_missing_and_wrong_key(self, secured: Any) -> None:
        async with secured as c:
            r1 = await c.post("/v1/eye-state", content=encode_frame(0.0), headers=IMG)
            r2 = await c.post("/v1/eye-state", content=encode_frame(0.0), headers={**IMG, "authorization": "Bearer no"})
        assert r1.status_code == r2.status_code == 401 and r1.headers["www-authenticate"] == "Bearer"

    async def test_valid_key_and_open_probes(self, secured: Any) -> None:
        async with secured as c:
            ok = await c.post(
                "/v1/eye-state", content=encode_frame(0.0), headers={**IMG, "authorization": f"Bearer {self.KEY}"}
            )
            assert ok.status_code == 200
            assert (await c.get("/healthz")).status_code == 200 and (await c.get("/metrics")).status_code == 200


async def test_capacity_shedding_returns_503(make_client: Callable[..., Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import queue

    def busy(self: Any, timeout: float) -> Any:
        raise queue.Empty

    async with make_client() as c:
        monkeypatch.setattr("tests.conftest.FakePool.acquire", busy)
        r = await c.post("/v1/eye-state", content=encode_frame(0.0), headers=IMG)
    assert r.status_code == 503 and r.headers["retry-after"] == "1"


async def test_unhandled_error_does_not_leak(make_client: Callable[..., Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: FakeLandmarker, *a: Any, **k: Any) -> Any:
        raise RuntimeError("secret internal path /etc/shadow")

    async with make_client() as c:
        monkeypatch.setattr(FakeLandmarker, "analyze", boom)
        transport = httpx.ASGITransport(app=c.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as safe:
            r = await safe.post("/v1/eye-state", content=encode_frame(0.0), headers=IMG)
    assert r.status_code == 500 and "shadow" not in r.text and r.json()["request_id"]


async def test_docs_toggle(make_client: Callable[..., Any]) -> None:
    async with make_client(docs_enabled=False) as c:
        assert (await c.get("/openapi.json")).status_code == 404
    async with make_client() as c:
        spec = (await c.get("/openapi.json")).json()
        assert "/v1/eye-state" in spec["paths"] and "/v1/jobs" in spec["paths"]


async def test_concurrent_requests_all_succeed(client: httpx.AsyncClient) -> None:
    rs = await asyncio.gather(
        *(client.post("/v1/eye-state", content=encode_frame(0.0), headers=IMG) for _ in range(20))
    )
    assert all(r.status_code == 200 for r in rs)
