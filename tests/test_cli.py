from __future__ import annotations

import json
from pathlib import Path

import pytest

from eye_blink.cli import main
from eye_blink.config import hash_api_key
from tests.conftest import MODEL, FakeLandmarker, blink_track, encode_frame, make_video


def test_analyze_prints_json(
    tmp_path: Path, fake_mediapipe: type[FakeLandmarker], capsys: pytest.CaptureFixture[str]
) -> None:
    video = make_video(tmp_path / "v.mp4", blink_track(30, 8.0, [(2.0, 0.25), (5.0, 0.25)]))
    assert main(["analyze", str(video)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["blink_count"] == 2 and out["counts"] == {"blink": 2, "long_closure": 0}
    assert out["video"]["frames_analyzed"] == 240


def test_analyze_errors(
    tmp_path: Path, fake_mediapipe: type[FakeLandmarker], capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 300)
    assert main(["analyze", str(bad)]) == 2
    assert "error:" in capsys.readouterr().err
    assert main(["analyze", str(tmp_path / "missing.mp4")]) == 2


def test_eye_state_json(
    tmp_path: Path, fake_mediapipe: type[FakeLandmarker], capsys: pytest.CaptureFixture[str]
) -> None:
    img = tmp_path / "closed.png"
    img.write_bytes(encode_frame(1.0))
    assert main(["eye-state", str(img)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["face_found"] and out["eyes_closed"] is True
    none = tmp_path / "none.png"
    none.write_bytes(encode_frame(None))
    assert main(["eye-state", str(none)]) == 0
    assert json.loads(capsys.readouterr().out) == {"face_found": False}


def test_keygen_hash_matches_key(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["keygen"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert hash_api_key(lines[0].split()[-1]) == lines[1].split()[-1]


def test_verify_model(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["verify-model", "--model", str(MODEL)]) == 0
    tampered = tmp_path / "m.task"
    tampered.write_bytes(MODEL.read_bytes() + b"\x00")
    assert main(["verify-model", "--model", str(tampered)]) == 1
    assert main(["verify-model", "--model", str(tmp_path / "missing")]) == 2


def test_tampered_model_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tampered = tmp_path / "m.task"
    tampered.write_bytes(MODEL.read_bytes() + b"\x00")
    monkeypatch.setenv("EB_MODEL_PATH", str(tampered))
    img = tmp_path / "i.png"
    img.write_bytes(encode_frame(0.0))
    assert main(["eye-state", str(img)]) == 3
    assert "checksum mismatch" in capsys.readouterr().err


class TestInitStorage:
    def test_creates_bucket_and_lifecycle(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import boto3
        from moto import mock_aws

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "t")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "t")
        monkeypatch.setenv("EB_S3_BUCKET", "cli-bucket")
        with mock_aws():
            assert main(["init-storage", "--expire-days", "3"]) == 0
            rules = boto3.client("s3", region_name="us-east-1").get_bucket_lifecycle_configuration(Bucket="cli-bucket")
            assert rules["Rules"][0]["Expiration"]["Days"] == 3

    def test_unreachable_storage_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("EB_S3_ENDPOINT_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("EB_S3_ACCESS_KEY_ID", "a")
        monkeypatch.setenv("EB_S3_SECRET_ACCESS_KEY", "b")
        assert main(["init-storage"]) == 1


class TestWebcam:
    """Fake camera + fake landmarker: no real device, no macOS permission prompt, no MediaPipe."""

    def test_camera_unavailable(
        self, fake_mediapipe: type[FakeLandmarker], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import cv2

        class Closed:
            def __init__(self, *_: object) -> None: ...
            def isOpened(self) -> bool:
                return False

            def release(self) -> None: ...

        monkeypatch.setattr(cv2, "VideoCapture", Closed)
        monkeypatch.setattr("eye_blink.cli.EyeLandmarker", FakeLandmarker)
        assert main(["webcam"]) == 2
        assert "cannot open camera" in capsys.readouterr().err

    def test_counts_blinks_and_exits_when_stream_ends(
        self, fake_mediapipe: type[FakeLandmarker], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cv2

        from tests.conftest import make_frame

        frames = [make_frame(c) for c in reversed([0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0])]
        drawn: list[str] = []
        clock = iter(x * 0.05 for x in range(100))

        class Cam:
            def __init__(self, *_: object) -> None: ...
            def isOpened(self) -> bool:
                return True

            def read(self) -> tuple[bool, object]:
                return (True, frames.pop()) if frames else (False, None)

            def release(self) -> None: ...

        monkeypatch.setattr(cv2, "VideoCapture", Cam)
        monkeypatch.setattr("eye_blink.cli.EyeLandmarker", FakeLandmarker)
        monkeypatch.setattr("eye_blink.cli.time.monotonic", lambda: next(clock))
        monkeypatch.setattr(cv2, "putText", lambda img, text, *a, **k: drawn.append(text))
        monkeypatch.setattr(cv2, "imshow", lambda *_: None)
        monkeypatch.setattr(cv2, "waitKey", lambda *_: 0)
        monkeypatch.setattr(cv2, "destroyAllWindows", lambda: None)
        assert main(["webcam"]) == 0
        assert drawn[-1].startswith("Blinks: 1")

    def test_headless_opencv_gives_actionable_error(
        self, fake_mediapipe: type[FakeLandmarker], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import cv2

        from tests.conftest import make_frame

        class Cam:
            def __init__(self, *_: object) -> None: ...
            def isOpened(self) -> bool:
                return True

            def read(self) -> tuple[bool, object]:
                return True, make_frame(0.0)

            def release(self) -> None: ...

        def no_gui(*_: object) -> None:
            raise cv2.error("The function is not implemented (imshow)")

        monkeypatch.setattr(cv2, "VideoCapture", Cam)
        monkeypatch.setattr("eye_blink.cli.EyeLandmarker", FakeLandmarker)
        monkeypatch.setattr(cv2, "imshow", no_gui)
        monkeypatch.setattr(cv2, "destroyAllWindows", lambda: None)
        assert main(["webcam"]) == 2
        assert "opencv-python-headless" in capsys.readouterr().err
