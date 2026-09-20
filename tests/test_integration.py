"""End-to-end tests against real NATS JetStream and an S3-compatible store.

Enabled by exporting:
    EB_TEST_NATS_URL=nats://127.0.0.1:4222
    EB_TEST_S3_ENDPOINT=http://127.0.0.1:8333
    EB_TEST_S3_ACCESS_KEY=...  EB_TEST_S3_SECRET_KEY=...
(``make up`` publishes both services on localhost.) Every test uses uniquely named
stream / KV bucket / S3 bucket and removes them afterwards.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from eye_blink.api.app import create_app
from eye_blink.config import Settings, hash_api_key
from eye_blink.jobs import JobBus, JobMessage
from eye_blink.service import BlinkService
from eye_blink.storage import ObjectStore
from eye_blink.worker import Worker
from tests.conftest import FakeLandmarker, blink_track, make_settings, make_video

NATS_URL = os.environ.get("EB_TEST_NATS_URL")
S3_ENDPOINT = os.environ.get("EB_TEST_S3_ENDPOINT")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (NATS_URL and S3_ENDPOINT), reason="EB_TEST_NATS_URL / EB_TEST_S3_ENDPOINT not set"),
]

VID = {"content-type": "video/mp4"}
KEY_A, KEY_B = "integration-key-a", "integration-key-b"


@dataclass
class Rig:
    settings: Settings
    client: httpx.AsyncClient
    store: ObjectStore
    start_worker: Callable[[], Any]


async def wait_terminal(
    client: httpx.AsyncClient, job_id: str, headers: dict[str, str] | None = None, timeout: float = 30
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await client.get(f"/v1/jobs/{job_id}", headers=headers)
        assert r.status_code == 200, r.text
        if r.json()["status"] in ("succeeded", "failed"):
            return dict(r.json())
        await asyncio.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


@pytest.fixture
async def rig(fake_mediapipe: type[FakeLandmarker]) -> AsyncIterator[Rig]:
    suffix = uuid.uuid4().hex[:8]
    settings = make_settings(
        async_enabled=True,
        nats_url=NATS_URL,
        nats_stream=f"FDT_{suffix}",
        nats_subject=f"ebt.{suffix}.detect",
        nats_consumer=f"w-{suffix}",
        nats_kv_bucket=f"ebt_{suffix}",
        s3_bucket=f"ebt-{suffix}",
        s3_endpoint_url=S3_ENDPOINT,
        s3_access_key_id=os.environ.get("EB_TEST_S3_ACCESS_KEY", "devaccesskey"),
        s3_secret_access_key=os.environ.get("EB_TEST_S3_SECRET_KEY", "devsecretkey-change-me"),
        worker_concurrency=3,
        job_retry_backoff_s=0.1,
        job_ack_wait_s=5,
        api_key_hashes=frozenset({hash_api_key(KEY_A), hash_api_key(KEY_B)}),
    )
    store = ObjectStore(settings)
    await store.ensure_bucket()
    app = create_app(settings)
    workers: list[tuple[Worker, JobBus]] = []

    async def start_worker() -> Worker:
        # Separate bus/store/inference instances: behaves like a separate process.
        bus, wstore = JobBus(settings), ObjectStore(settings)
        await bus.connect()
        worker = Worker(settings, bus, wstore, BlinkService(settings))
        worker.start()
        workers.append((worker, bus))
        return worker

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        try:
            yield Rig(settings, client, store, start_worker)
        finally:
            for worker, bus in workers:
                await worker.stop()
                await bus.close()
            bus = JobBus(settings)
            await bus.connect()
            with suppress(Exception):
                await bus.js.delete_stream(settings.nats_stream)
                await bus.js.delete_key_value(settings.nats_kv_bucket)
            await bus.close()
            raw = store._client
            with suppress(Exception):
                for obj in raw.list_objects_v2(Bucket=settings.s3_bucket).get("Contents", []):
                    raw.delete_object(Bucket=settings.s3_bucket, Key=obj["Key"])
                raw.delete_bucket(Bucket=settings.s3_bucket)


@pytest.fixture
def video_bytes(tmp_path: Path) -> bytes:
    return make_video(tmp_path / "v.mp4", blink_track(30, 8.0, [(2.0, 0.25), (5.0, 0.25)])).read_bytes()


AUTH_A = {"authorization": f"Bearer {KEY_A}"}
AUTH_B = {"authorization": f"Bearer {KEY_B}"}


async def test_readiness_reflects_backing_services(rig: Rig) -> None:
    r = await rig.client.get("/readyz")
    assert r.status_code == 200 and r.json()["checks"] == {"model": True, "nats": True, "object_storage": True}


async def test_job_lifecycle_and_input_deleted(rig: Rig, video_bytes: bytes) -> None:
    await rig.start_worker()
    r = await rig.client.post("/v1/jobs", content=video_bytes, headers={**VID, **AUTH_A})
    assert r.status_code == 202
    job = r.json()
    done = await wait_terminal(rig.client, job["job_id"], AUTH_A)
    assert done["status"] == "succeeded" and done["result"]["blink_count"] == 2 and done["attempts"] == 1
    with pytest.raises(Exception, match="NoSuchKey|Not Found|404"):
        rig.store._client.head_object(Bucket=rig.settings.s3_bucket, Key=f"inputs/{job['job_id']}")


async def test_api_and_worker_are_decoupled(rig: Rig, video_bytes: bytes) -> None:
    """Jobs submitted while no worker is running are queued, then processed once one appears."""
    r = await rig.client.post("/v1/jobs", content=video_bytes, headers={**VID, **AUTH_A})
    job_id = r.json()["job_id"]
    await asyncio.sleep(0.5)
    queued = (await rig.client.get(f"/v1/jobs/{job_id}", headers=AUTH_A)).json()
    assert queued["status"] == "queued" and queued["result"] is None
    await rig.start_worker()
    assert (await wait_terminal(rig.client, job_id, AUTH_A))["status"] == "succeeded"


async def test_corrupt_video_fails_permanently_after_one_attempt(rig: Rig) -> None:
    await rig.start_worker()
    corrupt = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 400  # passes the container sniff, fails decoding
    r = await rig.client.post("/v1/jobs", content=corrupt, headers={**VID, **AUTH_A})
    assert r.status_code == 202
    done = await wait_terminal(rig.client, r.json()["job_id"], AUTH_A)
    assert done["status"] == "failed" and done["attempts"] == 1 and done["error"]


async def test_missing_input_object_fails_with_clear_error(rig: Rig) -> None:
    await rig.start_worker()
    job_id = str(uuid.uuid4())
    bus = JobBus(rig.settings)
    await bus.connect()
    await bus.submit(
        JobMessage(job_id=job_id, owner=hash_api_key(KEY_A)[:16], object_key="inputs/ghost", created_at=time.time())
    )
    await bus.close()
    done = await wait_terminal(rig.client, job_id, AUTH_A)
    assert done["status"] == "failed" and "not found" in done["error"]


async def test_jobs_are_isolated_between_api_keys(rig: Rig, video_bytes: bytes) -> None:
    await rig.start_worker()
    job_id = (await rig.client.post("/v1/jobs", content=video_bytes, headers={**VID, **AUTH_A})).json()["job_id"]
    assert (await rig.client.get(f"/v1/jobs/{job_id}", headers=AUTH_B)).status_code == 404
    assert (await rig.client.get(f"/v1/jobs/{job_id}", headers=AUTH_A)).status_code == 200
    assert (await rig.client.get(f"/v1/jobs/{job_id}")).status_code == 401


async def test_unknown_and_malformed_job_ids_are_404(rig: Rig) -> None:
    for job_id in (str(uuid.uuid4()), "not-a-uuid", "../../etc/passwd"):
        assert (await rig.client.get(f"/v1/jobs/{job_id}", headers=AUTH_A)).status_code == 404


async def test_many_jobs_are_processed_by_multiple_workers(rig: Rig, video_bytes: bytes) -> None:
    await rig.start_worker()
    await rig.start_worker()  # two "replicas" sharing the durable consumer
    ids = []
    for _ in range(24):
        r = await rig.client.post("/v1/jobs", content=video_bytes, headers={**VID, **AUTH_A})
        ids.append(r.json()["job_id"])
    results = await asyncio.gather(*(wait_terminal(rig.client, i, AUTH_A) for i in ids))
    assert all(r["status"] == "succeeded" and r["result"]["blink_count"] == 2 for r in results)
    assert len(set(ids)) == 24


async def test_invalid_upload_is_rejected_before_queueing(rig: Rig) -> None:
    r = await rig.client.post("/v1/jobs", content=b"GIF89a" + b"\x00" * 64, headers={**VID, **AUTH_A})
    assert r.status_code == 415
    listing = rig.store._client.list_objects_v2(Bucket=rig.settings.s3_bucket)
    assert listing.get("KeyCount", 0) == 0  # nothing was uploaded to storage


async def test_worker_stops_promptly_when_idle(rig: Rig) -> None:
    worker = await rig.start_worker()
    await asyncio.sleep(0.3)
    started = time.monotonic()
    await worker.stop()
    assert time.monotonic() - started < 4


async def test_worker_app_serves_probes_and_metrics(rig: Rig) -> None:
    """The worker process (uvicorn app) starts its consumers, reports readiness and exposes metrics."""
    from eye_blink.worker import create_app as create_worker_app

    app = create_worker_app(rig.settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as c,
    ):
        assert (await c.get("/healthz")).status_code == 200
        ready = await c.get("/readyz")
        assert ready.status_code == 200 and ready.json()["checks"] == {"worker": True}
        assert "eb_worker_jobs_in_flight" in (await c.get("/metrics")).text


async def test_bus_shutdown_is_prompt_even_with_active_pull_subscription(rig: Rig) -> None:
    """Regression: nc.drain() blocked ~30 s on pull subscriptions, exceeding the pod grace period."""
    bus = JobBus(rig.settings)
    await bus.connect()
    await bus.pull_subscription()
    started = time.monotonic()
    await bus.close()
    assert time.monotonic() - started < 5
