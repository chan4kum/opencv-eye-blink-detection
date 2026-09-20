"""Asynchronous job pipeline on NATS JetStream.

* Stream ``EB_JOBS`` (work-queue retention) carries job messages; a single durable *pull* consumer
  is shared by every worker replica, so adding replicas scales throughput linearly.
* A JetStream key-value bucket stores job state and results, keeping the API stateless.
* Image bytes live in S3-compatible object storage, never in the message bus.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

import nats
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    KeyValueConfig,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.errors import BucketNotFoundError, KeyNotFoundError, NotFoundError
from nats.js.kv import KeyValue
from opentelemetry import propagate
from pydantic import BaseModel, Field

from eye_blink.config import Settings
from eye_blink.errors import DependencyUnavailableError
from eye_blink.logging_config import get_logger

log = get_logger(__name__)

KEY_PREFIX = "inputs/"


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED)


class JobRecord(BaseModel):
    job_id: str
    owner: str
    status: JobStatus
    created_at: float
    updated_at: float
    attempts: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None


class JobMessage(BaseModel):
    job_id: str
    owner: str
    object_key: str = Field(min_length=1)
    created_at: float


def new_job_id() -> str:
    return str(uuid.uuid4())


def is_valid_job_id(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


class JobBus:
    """Owns the NATS connection, JetStream stream, KV bucket and consumer."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._nc: NatsClient | None = None
        self._js: JetStreamContext | None = None
        self._kv: KeyValue | None = None

    # -- lifecycle -----------------------------------------------------------------
    async def connect(self) -> None:
        async def _error_cb(exc: Exception) -> None:
            log.warning("nats_error", error=repr(exc))

        async def _disconnected() -> None:
            log.warning("nats_disconnected")

        async def _reconnected() -> None:
            log.info("nats_reconnected")

        self._nc = await nats.connect(
            self._s.nats_url,
            name=self._s.service_name,
            connect_timeout=5,
            max_reconnect_attempts=-1,  # keep trying; readiness reports the outage
            reconnect_time_wait=1,
            error_cb=_error_cb,
            disconnected_cb=_disconnected,
            reconnected_cb=_reconnected,
        )
        self._js = self._nc.jetstream()
        if self._s.nats_provision:
            await self._provision()
        try:
            self._kv = await self._js.key_value(self._s.nats_kv_bucket)
        except BucketNotFoundError as exc:
            raise DependencyUnavailableError(
                f"KV bucket {self._s.nats_kv_bucket!r} does not exist (set EB_NATS_PROVISION=true or create it)"
            ) from exc
        log.info("nats_connected", url=self._s.nats_url, stream=self._s.nats_stream, kv=self._s.nats_kv_bucket)

    async def _provision(self) -> None:
        js = self.js
        stream_cfg = StreamConfig(
            name=self._s.nats_stream,
            subjects=[self._s.nats_subject],
            retention=RetentionPolicy.WORK_QUEUE,
            storage=StorageType.FILE,
            max_age=float(self._s.job_ttl_s),
            num_replicas=self._s.nats_replicas,
            duplicate_window=120.0,
        )
        try:
            await js.stream_info(self._s.nats_stream)
            await js.update_stream(stream_cfg)
        except NotFoundError:
            await js.add_stream(stream_cfg)
        try:
            await js.key_value(self._s.nats_kv_bucket)
        except BucketNotFoundError:
            await js.create_key_value(
                KeyValueConfig(
                    bucket=self._s.nats_kv_bucket,
                    ttl=float(self._s.job_ttl_s),
                    history=1,
                    replicas=self._s.nats_replicas,
                    storage=StorageType.FILE,
                )
            )

    async def close(self) -> None:
        """Flush pending publishes and disconnect.

        ``nc.drain()`` is deliberately not used: it waits up to 30 s for subscriptions to empty, which
        for pull consumers means every shutdown blocks for the full timeout and exceeds the pod's
        termination grace period. ``JetStream.publish`` is awaited until acknowledged, so a flush
        is all that is needed to avoid losing anything.
        """
        nc, self._nc, self._js, self._kv = self._nc, None, None, None
        if nc is not None and not nc.is_closed:
            with contextlib.suppress(Exception):
                await nc.flush(timeout=5)
            await nc.close()

    @property
    def js(self) -> JetStreamContext:
        if self._js is None:
            raise DependencyUnavailableError("NATS is not connected")
        return self._js

    @property
    def kv(self) -> KeyValue:
        if self._kv is None:
            raise DependencyUnavailableError("NATS is not connected")
        return self._kv

    def is_ready(self) -> bool:
        return self._nc is not None and self._nc.is_connected

    # -- producer side -------------------------------------------------------------
    async def submit(self, message: JobMessage) -> None:
        """Persist the queued record, then enqueue the work (in that order)."""
        record = JobRecord(
            job_id=message.job_id,
            owner=message.owner,
            status=JobStatus.QUEUED,
            created_at=message.created_at,
            updated_at=time.time(),
        )
        await self.put_record(record)
        headers: dict[str, str] = {"Nats-Msg-Id": message.job_id}
        propagate.inject(headers)  # W3C trace context flows API -> worker
        try:
            await self.js.publish(self._s.nats_subject, message.model_dump_json().encode(), headers=headers, timeout=5)
        except Exception as exc:
            raise DependencyUnavailableError("could not enqueue the job") from exc

    async def put_record(self, record: JobRecord) -> None:
        try:
            await self.kv.put(record.job_id, record.model_dump_json().encode())
        except Exception as exc:
            raise DependencyUnavailableError("could not persist job state") from exc

    async def get_record(self, job_id: str) -> JobRecord | None:
        try:
            entry = await self.kv.get(job_id)
        except (KeyNotFoundError, NotFoundError):
            return None
        except Exception as exc:
            raise DependencyUnavailableError("could not read job state") from exc
        if entry.value is None:
            return None
        return JobRecord.model_validate(json.loads(entry.value))

    # -- consumer side -------------------------------------------------------------
    async def pull_subscription(self) -> Any:
        """Return the shared durable pull subscription used by all worker replicas."""
        js = self.js
        cfg = ConsumerConfig(
            durable_name=self._s.nats_consumer,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=float(self._s.job_ack_wait_s),
            max_deliver=self._s.job_max_deliver,
            filter_subject=self._s.nats_subject,
            max_ack_pending=self._s.worker_concurrency * 8,
        )
        if self._s.nats_provision:
            try:
                await js.consumer_info(self._s.nats_stream, self._s.nats_consumer)
            except NotFoundError:
                await js.add_consumer(self._s.nats_stream, cfg)
        return await js.pull_subscribe(self._s.nats_subject, durable=self._s.nats_consumer, stream=self._s.nats_stream)


Handler = Callable[[JobMessage], Awaitable[None]]
