"""Environment-driven configuration (12-factor). Every setting is prefixed with ``EB_``."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AnyUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from eye_blink.blink import BlinkConfig
from eye_blink.landmarker import SignalMode

# SHA-256 of the pinned MediaPipe Face Landmarker bundle (float16, v1; Apache-2.0).
DEFAULT_MODEL_SHA256 = "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"

ALLOWED_IMAGE_FORMATS = ("JPEG", "PNG", "WEBP", "BMP")


class Settings(BaseSettings):
    """Validated runtime configuration. Invalid values fail fast at start-up."""

    model_config = SettingsConfigDict(env_prefix="EB_", extra="ignore", frozen=True)

    # --- general -----------------------------------------------------------------
    environment: Literal["dev", "test", "prod"] = "dev"
    service_name: str = "eye-blink"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True
    docs_enabled: bool = True  # serve /docs, /redoc and /openapi.json

    # --- model / inference -------------------------------------------------------
    model_path: Path = Path("models/face_landmarker.task")
    model_sha256: str = DEFAULT_MODEL_SHA256
    min_detection_confidence: float = Field(0.5, ge=0.0, le=1.0)
    signal: SignalMode = SignalMode.BLENDSHAPE

    # --- blink detection (see eye_blink.blink) --------------------------------------
    blink_close_threshold: float = Field(0.5, gt=0.0, le=1.0)
    blink_open_threshold: float = Field(0.3, ge=0.0, lt=1.0)
    blink_min_duration_ms: float = Field(50.0, ge=0.0)
    blink_max_duration_ms: float = Field(700.0, gt=0.0)
    blink_max_face_gap_ms: float = Field(400.0, ge=0.0)

    # --- request limits (defence against oversized / decompression-bomb inputs) ----
    max_upload_bytes: int = Field(10 * 1024 * 1024, ge=1024)  # still images
    max_image_pixels: int = Field(16_000_000, ge=1024)
    max_video_bytes: int = Field(50 * 1024 * 1024, ge=1024)
    max_video_seconds: float = Field(120.0, gt=0)
    max_video_pixels: int = Field(1920 * 1080, ge=1024)  # per frame
    max_video_frames: int = Field(7_200, ge=1)  # hard cap on frames actually decoded, regardless of headers
    video_timeout_s: float = Field(300.0, gt=0)  # wall-clock budget for analysing one video
    video_analysis_fps: float = Field(30.0, gt=0, le=120)  # frames above this rate are skipped
    max_concurrent_inference: int = Field(4, ge=1, le=256)
    inference_queue_timeout_s: float = Field(2.0, gt=0)

    # --- live stream (WebSocket) limits ---------------------------------------------
    stream_max_sessions: int = Field(16, ge=1, le=1024)
    stream_max_frame_bytes: int = Field(1024 * 1024, ge=1024)
    stream_max_fps: float = Field(30.0, gt=0, le=120)
    stream_idle_timeout_s: float = Field(30.0, gt=0)
    stream_max_duration_s: float = Field(900.0, gt=0)

    # --- security ----------------------------------------------------------------
    # Comma-separated SHA-256 hex digests of accepted API keys (never store raw keys).
    api_key_hashes: Annotated[frozenset[str], NoDecode] = frozenset()
    auth_disabled: bool = False
    cors_allow_origins: Annotated[tuple[str, ...], NoDecode] = ()

    # --- async pipeline (NATS JetStream + S3-compatible object storage) ----------
    async_enabled: bool = False
    nats_url: str = "nats://localhost:4222"
    nats_provision: bool = True  # create stream/KV/consumer if missing
    nats_replicas: int = Field(1, ge=1, le=5)  # use 3 against a 3-node NATS cluster
    nats_stream: str = "EB_JOBS"
    nats_subject: str = "eb.jobs.analyze"
    nats_consumer: str = "eb-workers"
    nats_kv_bucket: str = "eb_jobs"
    job_ttl_s: int = Field(3600, ge=60)
    job_max_deliver: int = Field(4, ge=1, le=20)
    job_ack_wait_s: int = Field(30, ge=5)
    job_retry_backoff_s: float = Field(2.0, ge=0)
    worker_concurrency: int = Field(4, ge=1, le=256)

    s3_bucket: str = "eye-blink"
    s3_endpoint_url: AnyUrl | None = None  # unset = AWS S3; set for MinIO/SeaweedFS/Ceph/etc.
    s3_region: str = "us-east-1"
    s3_access_key_id: SecretStr | None = None  # unset = default credential chain (IRSA, env, ...)
    s3_secret_access_key: SecretStr | None = None
    s3_force_path_style: bool = True
    s3_server_side_encryption: Literal["AES256", "aws:kms"] | None = None
    delete_input_after_processing: bool = True  # privacy by default: do not retain uploaded images

    @field_validator("api_key_hashes", mode="before")
    @classmethod
    def _split_hashes(cls, v: object) -> object:
        if isinstance(v, str):
            return frozenset(h.strip().lower() for h in v.split(",") if h.strip())
        return v

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return tuple(o.strip() for o in v.split(",") if o.strip())
        return v

    @property
    def blink_config(self) -> BlinkConfig:
        return BlinkConfig(
            close_threshold=self.blink_close_threshold,
            open_threshold=self.blink_open_threshold,
            min_duration_ms=self.blink_min_duration_ms,
            max_duration_ms=self.blink_max_duration_ms,
            max_face_gap_ms=self.blink_max_face_gap_ms,
        )

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        self.blink_config  # noqa: B018 - construct once to validate threshold relationships at start-up
        for h in self.api_key_hashes:
            if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
                raise ValueError("EB_API_KEY_HASHES must contain 64-char lowercase SHA-256 hex digests")
        if self.environment == "prod" and not self.api_key_hashes and not self.auth_disabled:
            raise ValueError(
                "EB_ENVIRONMENT=prod requires EB_API_KEY_HASHES (or an explicit EB_AUTH_DISABLED=true "
                "when authentication is enforced upstream, e.g. by a service mesh or gateway)"
            )
        if (self.s3_access_key_id is None) != (self.s3_secret_access_key is None):
            raise ValueError("EB_S3_ACCESS_KEY_ID and EB_S3_SECRET_ACCESS_KEY must be set together")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def hash_api_key(key: str) -> str:
    """SHA-256 hex digest used to store API keys."""
    return hashlib.sha256(key.encode()).hexdigest()
