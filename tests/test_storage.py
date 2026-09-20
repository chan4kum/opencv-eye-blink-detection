from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from eye_blink.errors import DependencyUnavailableError, PayloadTooLargeError
from eye_blink.storage import ObjectNotFoundError, ObjectStore
from tests.conftest import make_settings

BUCKET = "unit-bucket"


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        yield


def store(**kw: object) -> ObjectStore:
    return ObjectStore(make_settings(s3_bucket=BUCKET, s3_region="us-east-1", **kw))


async def test_ensure_bucket_creates_bucket_and_lifecycle_rule(aws: None) -> None:
    s = store()
    assert await s.ensure_bucket(expire_days=2) == []
    rules = boto3.client("s3", region_name="us-east-1").get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]
    assert rules[0]["Filter"]["Prefix"] == "inputs/" and rules[0]["Expiration"]["Days"] == 2
    assert await s.ensure_bucket() == []  # idempotent


async def test_ensure_bucket_in_non_default_region(aws: None) -> None:
    s = ObjectStore(make_settings(s3_bucket=BUCKET, s3_region="eu-west-1"))
    await s.ensure_bucket(region="eu-west-1")
    loc = boto3.client("s3", region_name="eu-west-1").get_bucket_location(Bucket=BUCKET)["LocationConstraint"]
    assert loc == "eu-west-1"


async def test_roundtrip_and_delete(aws: None) -> None:
    s = store()
    await s.ensure_bucket()
    await s.put("inputs/a", b"hello", "image/jpeg")
    assert await s.get("inputs/a") == b"hello"
    await s.delete("inputs/a")
    with pytest.raises(ObjectNotFoundError):
        await s.get("inputs/a")


async def test_size_limit_enforced_on_read(aws: None) -> None:
    s = store(max_upload_bytes=2048, max_video_bytes=2048)
    await s.ensure_bucket()
    await s.put("inputs/big", b"x" * 4096)
    with pytest.raises(PayloadTooLargeError):
        await s.get("inputs/big")


async def test_server_side_encryption_header_is_sent(aws: None) -> None:
    s = store(s3_server_side_encryption="AES256")
    await s.ensure_bucket()
    await s.put("inputs/enc", b"data")
    head = boto3.client("s3", region_name="us-east-1").head_object(Bucket=BUCKET, Key="inputs/enc")
    assert head["ServerSideEncryption"] == "AES256"


async def test_missing_bucket_maps_to_dependency_unavailable(aws: None) -> None:
    s = store()
    with pytest.raises(DependencyUnavailableError):
        await s.check()
    with pytest.raises(DependencyUnavailableError):
        await s.put("inputs/x", b"1")


async def test_explicit_credentials_and_endpoint_are_used(aws: None) -> None:
    s = ObjectStore(make_settings(s3_endpoint_url="http://127.0.0.1:1", s3_access_key_id="a", s3_secret_access_key="b"))
    assert s._client.meta.endpoint_url == "http://127.0.0.1:1/"
