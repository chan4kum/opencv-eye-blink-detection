from __future__ import annotations

import struct
from pathlib import Path

import pytest

from eye_blink.errors import InvalidVideoError, UnsupportedMediaTypeError, VideoLimitError
from eye_blink.video import analyze_video, sniff_container, temporary_video_file
from tests.conftest import FakeLandmarker, blink_track, make_settings, make_video


def test_sniff_container_by_magic_bytes() -> None:
    assert sniff_container(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8) == "mp4"
    assert sniff_container(b"\x1a\x45\xdf\xa3" + b"\x00" * 12) == "webm"
    assert sniff_container(b"RIFF" + struct.pack("<I", 100) + b"AVI LIST" + b"\x00" * 4) == "avi"


@pytest.mark.parametrize(
    "data", [b"", b"GIF89a" + b"\x00" * 20, b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, b"not a video at all!!"]
)
def test_sniff_rejects_other_formats(data: bytes) -> None:
    with pytest.raises(UnsupportedMediaTypeError):
        sniff_container(data)


def test_temp_file_is_private_and_removed() -> None:
    with temporary_video_file(b"abc", "mp4") as path:
        assert path.read_bytes() == b"abc" and (path.stat().st_mode & 0o777) == 0o600
    assert not path.exists()


def test_temp_file_removed_even_when_body_raises() -> None:
    with pytest.raises(RuntimeError), temporary_video_file(b"abc", "mp4") as path:
        raise RuntimeError("boom")
    assert not path.exists()


def test_counts_blinks_in_a_video(tmp_path: Path, fake_mediapipe: type[FakeLandmarker]) -> None:
    video = make_video(tmp_path / "v.mp4", blink_track(30, 10.0, [(2.0, 0.25), (5.0, 0.25), (5.6, 0.25), (8.0, 0.25)]))
    res = analyze_video(video, make_settings())
    assert res.blink_count == 4 and res.long_closure_count == 0
    assert [round(e.start_ms / 1000) for e in res.events] == [2, 5, 6, 8]
    assert res.frames_decoded == 300 and res.frames_analyzed == 300 and res.face_found_ratio == 1.0
    assert res.blinks_per_minute == pytest.approx(4 / (10.0 / 60), rel=0.05)
    assert all(lm.closed for lm in fake_mediapipe.instances), "landmarker must be closed after analysis"


def test_long_closure_is_reported_separately(tmp_path: Path, fake_mediapipe: type[FakeLandmarker]) -> None:
    res = analyze_video(make_video(tmp_path / "v.mp4", blink_track(30, 6.0, [(1.0, 2.0)])), make_settings())
    assert res.blink_count == 0 and res.long_closure_count == 1


def test_no_face_video(tmp_path: Path, fake_mediapipe: type[FakeLandmarker]) -> None:
    res = analyze_video(make_video(tmp_path / "v.mp4", [None] * 60), make_settings())
    assert res.blink_count == 0 and res.face_found_ratio == 0.0 and res.blinks_per_minute is None


def test_blink_rate_is_null_with_too_little_face_time(tmp_path: Path, fake_mediapipe: type[FakeLandmarker]) -> None:
    res = analyze_video(make_video(tmp_path / "v.mp4", blink_track(30, 3.0, [(1.0, 0.25)])), make_settings())
    assert res.blink_count == 1 and res.blinks_per_minute is None  # < 5 s of face data


def test_frame_rate_cap_skips_frames(tmp_path: Path, fake_mediapipe: type[FakeLandmarker]) -> None:
    video = make_video(tmp_path / "v.mp4", blink_track(30, 4.0, [(1.0, 0.4)]))
    res = analyze_video(video, make_settings(video_analysis_fps=10))
    assert res.frames_decoded == 120 and 38 <= res.frames_analyzed <= 42 and res.blink_count == 1


def test_signal_mode_ear_uses_landmark_geometry(tmp_path: Path, fake_mediapipe: type[FakeLandmarker]) -> None:
    video = make_video(tmp_path / "v.mp4", blink_track(30, 4.0, [(1.0, 0.3)]))
    res = analyze_video(video, make_settings(signal="ear"))
    assert res.blink_count == 1


def test_video_dimension_limit(tmp_path: Path, fake_mediapipe: type[FakeLandmarker]) -> None:
    video = make_video(tmp_path / "v.mp4", [0.0] * 30)
    with pytest.raises(VideoLimitError, match="pixels"):
        analyze_video(video, make_settings(max_video_pixels=1024))


def test_video_duration_limit_from_header(tmp_path: Path, fake_mediapipe: type[FakeLandmarker]) -> None:
    video = make_video(tmp_path / "v.mp4", [0.0] * 300)  # 10 s
    with pytest.raises(VideoLimitError, match="long"):
        analyze_video(video, make_settings(max_video_seconds=5))


def test_frame_cap_is_enforced_even_if_header_is_trusted(tmp_path: Path, fake_mediapipe: type[FakeLandmarker]) -> None:
    video = make_video(tmp_path / "v.mp4", [0.0] * 90)
    with pytest.raises(VideoLimitError, match="more than 50 frames"):
        analyze_video(video, make_settings(max_video_frames=50))


def test_wall_clock_budget(
    tmp_path: Path, fake_mediapipe: type[FakeLandmarker], monkeypatch: pytest.MonkeyPatch
) -> None:
    video = make_video(tmp_path / "v.mp4", [0.0] * 90)
    ticks = iter(range(0, 100_000, 400))  # every check advances the clock 400 s
    monkeypatch.setattr("eye_blink.video.time.monotonic", lambda: float(next(ticks)))
    with pytest.raises(VideoLimitError, match="time budget"):
        analyze_video(video, make_settings(video_timeout_s=300))


def test_corrupt_video_is_rejected(tmp_path: Path, fake_mediapipe: type[FakeLandmarker]) -> None:
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200)
    with pytest.raises(InvalidVideoError):
        analyze_video(bad, make_settings())


def test_truncated_video_still_fails_safely_or_analyses_what_exists(
    tmp_path: Path, fake_mediapipe: type[FakeLandmarker]
) -> None:
    good = make_video(tmp_path / "v.mp4", blink_track(30, 4.0, [(1.0, 0.3)]))
    cut = tmp_path / "cut.mp4"
    cut.write_bytes(good.read_bytes()[: good.stat().st_size // 4])
    try:
        res = analyze_video(cut, make_settings())
        assert res.frames_decoded >= 0
    except InvalidVideoError:
        pass


def test_landmarker_closed_when_analysis_fails(tmp_path: Path, fake_mediapipe: type[FakeLandmarker]) -> None:
    video = make_video(tmp_path / "v.mp4", [0.0] * 90)
    with pytest.raises(VideoLimitError):
        analyze_video(video, make_settings(max_video_frames=10))
    assert fake_mediapipe.instances and all(lm.closed for lm in fake_mediapipe.instances)
