"""S3-compatible object storage (AWS S3, MinIO, SeaweedFS, Ceph, Cloudflare R2, ...)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from eye_blink.config import Settings
from eye_blink.errors import DependencyUnavailableError, PayloadTooLargeError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


class ObjectNotFoundError(Exception):
    """The requested object does not exist (expired, deleted or never uploaded)."""


class ObjectStore:
    """Thin async facade over boto3 (blocking calls run in worker threads)."""

    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.s3_bucket
        self._sse = settings.s3_server_side_encryption
        self._max_bytes = max(settings.max_upload_bytes, settings.max_video_bytes)
        kwargs: dict[str, Any] = {}
        if settings.s3_endpoint_url is not None:
            kwargs["endpoint_url"] = str(settings.s3_endpoint_url)
        if settings.s3_access_key_id is not None and settings.s3_secret_access_key is not None:
            kwargs["aws_access_key_id"] = settings.s3_access_key_id.get_secret_value()
            kwargs["aws_secret_access_key"] = settings.s3_secret_access_key.get_secret_value()
        self._client: S3Client = boto3.client(
            "s3",
            region_name=settings.s3_region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if settings.s3_force_path_style else "auto"},
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=3,
                read_timeout=10,
                max_pool_connections=max(10, settings.worker_concurrency * 2),
            ),
            **kwargs,
        )

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        def _put() -> None:
            extra: dict[str, Any] = {"ServerSideEncryption": self._sse} if self._sse else {}
            self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type, **extra)

        await self._run(_put)

    async def get(self, key: str) -> bytes:
        def _get() -> bytes:
            try:
                resp = self._client.get_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
                    raise ObjectNotFoundError(key) from exc
                raise
            if resp["ContentLength"] > self._max_bytes:
                raise PayloadTooLargeError("stored object exceeds the configured size limit")
            body: bytes = resp["Body"].read(self._max_bytes + 1)
            if len(body) > self._max_bytes:
                raise PayloadTooLargeError("stored object exceeds the configured size limit")
            return body

        result: bytes = await self._run(_get)
        return result

    async def delete(self, key: str) -> None:
        await self._run(lambda: self._client.delete_object(Bucket=self._bucket, Key=key))

    async def ensure_bucket(self, *, expire_days: int = 1, region: str = "us-east-1") -> list[str]:
        """Create the bucket if missing and add an expiry rule for orphaned uploads.

        Returns human-readable notes about steps the backend did not support.
        """
        notes: list[str] = []

        def _ensure() -> None:
            try:
                self._client.head_bucket(Bucket=self._bucket)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchBucket", "NotFound"}:
                    raise
                if region == "us-east-1":
                    self._client.create_bucket(Bucket=self._bucket)
                else:
                    self._client.create_bucket(
                        Bucket=self._bucket,
                        CreateBucketConfiguration={"LocationConstraint": region},  # type: ignore[typeddict-item]
                    )
            try:
                self._client.put_bucket_lifecycle_configuration(
                    Bucket=self._bucket,
                    LifecycleConfiguration={
                        "Rules": [
                            {
                                "ID": "expire-uploaded-inputs",
                                "Status": "Enabled",
                                "Filter": {"Prefix": "inputs/"},
                                "Expiration": {"Days": expire_days},
                                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                            }
                        ]
                    },
                )
            except ClientError as exc:
                notes.append(f"lifecycle rule not applied: {exc.response.get('Error', {}).get('Code')}")

        await self._run(_ensure)
        return notes

    async def check(self) -> None:
        """Raise if the bucket is unreachable (used by readiness probes)."""
        await self._run(lambda: self._client.head_bucket(Bucket=self._bucket))

    @staticmethod
    async def _run(fn: Any) -> Any:
        try:
            return await anyio.to_thread.run_sync(fn)
        except (ObjectNotFoundError, PayloadTooLargeError):
            raise
        except (BotoCoreError, ClientError) as exc:
            raise DependencyUnavailableError("object storage request failed") from exc
