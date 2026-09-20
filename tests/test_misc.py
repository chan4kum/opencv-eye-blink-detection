from __future__ import annotations

import asyncio
import uuid

import pytest

from eye_blink.api.state import AppState
from eye_blink.jobs import JobStatus, is_valid_job_id, new_job_id
from eye_blink.telemetry import setup_tracing, tracing_enabled
from tests.conftest import make_settings


def test_job_ids_are_uuid4_and_validated() -> None:
    jid = new_job_id()
    assert is_valid_job_id(jid) and uuid.UUID(jid).version == 4
    for bad in ("", "abc", "../etc/passwd", jid.upper() + "x", jid.replace("-", "")):
        assert not is_valid_job_id(bad)


def test_status_terminality() -> None:
    assert JobStatus.SUCCEEDED.terminal and JobStatus.FAILED.terminal
    assert not JobStatus.QUEUED.terminal and not JobStatus.PROCESSING.terminal


def test_tracing_is_off_without_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    assert not tracing_enabled() and setup_tracing(make_settings()) is None


def test_tracing_provider_is_created_when_endpoint_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    provider = setup_tracing(make_settings())
    assert provider is not None
    provider.shutdown()


class _Store:
    def __init__(self) -> None:
        self.calls = 0
        self.ok = True

    async def check(self) -> None:
        self.calls += 1
        if not self.ok:
            raise RuntimeError("down")


async def test_storage_health_is_cached_and_recovers() -> None:
    store = _Store()
    state = AppState(settings=make_settings(), service=None, store=store)  # type: ignore[arg-type]
    assert await state.storage_healthy() and await state.storage_healthy()
    assert store.calls == 1  # second call served from the 5s cache
    state._storage_ok_until = 0
    store.ok = False
    assert not await state.storage_healthy()
    store.ok = True
    await asyncio.sleep(0)
    assert await state.storage_healthy()


async def test_storage_health_without_store_is_healthy() -> None:
    assert await AppState(settings=make_settings(), service=None).storage_healthy()  # type: ignore[arg-type]
