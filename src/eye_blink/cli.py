"""Command-line interface: local analysis, live webcam counter, API-key generation, model verification."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

import anyio
import cv2

from eye_blink.blink import BlinkDetector
from eye_blink.config import DEFAULT_MODEL_SHA256, Settings, hash_api_key
from eye_blink.errors import AppError
from eye_blink.imaging import validate_and_decode
from eye_blink.landmarker import EyeLandmarker, ModelIntegrityError, bgr_to_rgb, sha256_file, verify_model
from eye_blink.storage import ObjectStore
from eye_blink.video import analyze_video, event_kinds

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray


def _settings() -> Settings:
    return Settings(environment="dev")


def _cmd_analyze(args: argparse.Namespace) -> int:
    settings = _settings()
    verify_model(settings.model_path, settings.model_sha256)
    try:
        result = analyze_video(args.video, settings)
    except OSError as exc:
        print(f"error: cannot read {args.video}: {exc}", file=sys.stderr)
        return 2
    except AppError as exc:
        print(f"error: {exc.detail}", file=sys.stderr)
        return 2
    payload = {
        "video": {
            "width": result.info.width,
            "height": result.info.height,
            "fps": result.info.fps,
            "frames_analyzed": result.frames_analyzed,
        },
        "face_found_ratio": round(result.face_found_ratio, 4),
        "blink_count": result.blink_count,
        "long_closure_count": result.long_closure_count,
        "blinks_per_minute": result.blinks_per_minute,
        "events": [
            {
                "kind": e.kind.value,
                "start_ms": round(e.start_ms, 1),
                "duration_ms": round(e.duration_ms, 1),
                "peak_closure": round(e.peak_closure, 3),
            }
            for e in result.events
        ],
        "counts": event_kinds(result.events),
    }
    print(json.dumps(payload, indent=2 if sys.stdout.isatty() else None))
    return 0


def _cmd_eye_state(args: argparse.Namespace) -> int:
    settings = _settings()
    verify_model(settings.model_path, settings.model_sha256)
    try:
        image = validate_and_decode(args.image.read_bytes(), max_pixels=settings.max_image_pixels)
    except OSError as exc:
        print(f"error: cannot read {args.image}: {exc}", file=sys.stderr)
        return 2
    except AppError as exc:
        print(f"error: {exc.detail}", file=sys.stderr)
        return 2
    landmarker = EyeLandmarker(settings.model_path, video=False)
    try:
        state = landmarker.analyze(bgr_to_rgb(image))
    finally:
        landmarker.close()
    if state is None:
        print(json.dumps({"face_found": False}))
        return 0
    closure = state.closure_for(settings.signal)
    print(
        json.dumps(
            {
                "face_found": True,
                "closure": round(closure, 3),
                "eyes_closed": closure >= settings.blink_close_threshold,
                "blink_score_left": round(state.blink_left, 3),
                "blink_score_right": round(state.blink_right, 3),
                "ear_left": round(state.ear_left, 3),
                "ear_right": round(state.ear_right, 3),
            }
        )
    )
    return 0


def _cmd_webcam(args: argparse.Namespace) -> int:
    settings = _settings()
    verify_model(settings.model_path, settings.model_sha256)
    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print(f"error: cannot open camera {args.camera}", file=sys.stderr)
        return 2
    landmarker = EyeLandmarker(settings.model_path, video=True)
    detector = BlinkDetector(settings.blink_config)
    started = time.monotonic()
    last_ts = -1
    try:
        while True:
            ok, raw = cap.read()
            if not ok:
                break
            frame = cast("NDArray[np.uint8]", raw)
            ts = max(int((time.monotonic() - started) * 1000), last_ts + 1)
            last_ts = ts
            state = landmarker.analyze(bgr_to_rgb(frame), ts)
            closure = None if state is None else state.closure_for(settings.signal)
            detector.update(float(ts), closure)
            label = "no face" if closure is None else f"closure {closure:.2f}"
            cv2.putText(
                frame,
                f"Blinks: {detector.blink_count}  ({label})",
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 0, 255),
                2,
            )
            cv2.imshow("eye-blink (q to quit)", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    except cv2.error as exc:
        print(
            "error: this OpenCV build has no GUI support (headless). For the webcam demo run:\n"
            "  uv pip uninstall opencv-python-headless && uv pip install opencv-python\n"
            f"({exc})",
            file=sys.stderr,
        )
        return 2
    finally:
        cap.release()
        landmarker.close()
        cv2.destroyAllWindows()
    return 0


def _cmd_keygen(_: argparse.Namespace) -> int:
    key = secrets.token_urlsafe(32)
    print(f"API key (give to the client, shown once): {key}")
    print(f"EB_API_KEY_HASHES entry (put on the server):  {hash_api_key(key)}")
    return 0


def _cmd_init_storage(args: argparse.Namespace) -> int:
    settings = _settings()
    store = ObjectStore(settings)
    try:
        notes = anyio.run(lambda: store.ensure_bucket(expire_days=args.expire_days, region=settings.s3_region))
    except AppError as exc:
        print(f"error: {exc.detail}", file=sys.stderr)
        return 1
    for note in notes:
        print(f"warning: {note}", file=sys.stderr)
    print(f"bucket {settings.s3_bucket!r} ready (inputs/ expire after {args.expire_days} day(s))")
    return 0


def _cmd_verify_model(args: argparse.Namespace) -> int:
    path = args.model or _settings().model_path
    try:
        actual = sha256_file(path)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    ok = actual == DEFAULT_MODEL_SHA256
    print(f"{path}: sha256={actual} {'OK' if ok else 'MISMATCH (expected ' + DEFAULT_MODEL_SHA256 + ')'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eye-blink", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    a = sub.add_parser("analyze", help="detect blinks in a video file and print JSON")
    a.add_argument("video", type=Path)
    a.set_defaults(func=_cmd_analyze)

    e = sub.add_parser("eye-state", help="report whether the eyes are open/closed in one image")
    e.add_argument("image", type=Path)
    e.set_defaults(func=_cmd_eye_state)

    w = sub.add_parser("webcam", help="live webcam blink counter (requires GUI-enabled OpenCV)")
    w.add_argument("--camera", type=int, default=0)
    w.set_defaults(func=_cmd_webcam)

    sub.add_parser("keygen", help="generate an API key and its SHA-256 hash").set_defaults(func=_cmd_keygen)

    ini = sub.add_parser("init-storage", help="create the S3 bucket and its input-expiry lifecycle rule")
    ini.add_argument("--expire-days", type=int, default=1)
    ini.set_defaults(func=_cmd_init_storage)

    ver = sub.add_parser("verify-model", help="check the model file against the pinned checksum")
    ver.add_argument("--model", type=Path)
    ver.set_defaults(func=_cmd_verify_model)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ModelIntegrityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
