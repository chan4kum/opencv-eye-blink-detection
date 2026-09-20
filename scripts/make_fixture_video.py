"""Generate synthetic blink videos with KNOWN ground truth from a still photo (public domain NASA portrait).

A real face is detected with MediaPipe, then the upper eyelids are moved down frame by frame following
a raised-cosine closure profile and the uncovered area is filled with sampled skin colour. This gives
deterministic blink timings for tests. It is *synthetic*: it validates the pipeline and thresholds, not
real-world accuracy on people.

Run inside the Linux test image:  docker run --rm -v ... eyeblink-test:dev  (see Makefile: make fixtures)
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import cv2
import numpy as np

from eye_blink.landmarker import EyeLandmarker

# Eye contour rings (MediaPipe 478-point mesh)
UPPER = {"right": [246, 161, 160, 159, 158, 157, 173], "left": [398, 384, 385, 386, 387, 388, 466]}
LOWER = {"right": [7, 163, 144, 145, 153, 154, 155], "left": [382, 381, 380, 374, 373, 390, 249]}
CORNERS = {"right": (33, 133), "left": (362, 263)}


def landmarks_px(lm: EyeLandmarker, rgb: np.ndarray) -> np.ndarray:
    mp = lm._mp
    res = lm._landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
    h, w = rgb.shape[:2]
    return np.array([[p.x * w, p.y * h] for p in res.face_landmarks[0]])


def render(base_bgr: np.ndarray, pts: np.ndarray, closure: float) -> np.ndarray:
    out = base_bgr.copy()
    if closure <= 0.01:
        return out
    mask = np.zeros(out.shape[:2], np.uint8)
    skin_samples = []
    for side in ("right", "left"):
        up = pts[UPPER[side]]
        lo = pts[LOWER[side]]
        lid = up + closure * (lo - up) * 1.0  # move the upper lid toward the lower lid
        poly = np.vstack([up, lid[::-1]]).astype(np.int32)
        cv2.fillPoly(mask, [poly], 255)
        # sample skin just above the eyebrow line of this eye
        cx, cy = up.mean(axis=0)
        y0 = int(cy - 0.9 * (lo[:, 1].mean() - up[:, 1].mean()) - 12)
        skin_samples.append(base_bgr[max(0, y0 - 4) : y0 + 4, int(cx) - 6 : int(cx) + 6].reshape(-1, 3))
    skin = np.median(np.vstack(skin_samples), axis=0)
    mask = cv2.GaussianBlur(mask, (5, 5), 0)
    alpha = (mask.astype(np.float32) / 255.0)[..., None]
    fill = np.empty_like(out, dtype=np.float32)
    fill[:] = skin
    out = (out * (1 - alpha) + fill * alpha).astype(np.uint8)
    # A closed eye shows a dark, slightly downward-curved lash line where the lids meet.
    lash = np.zeros(out.shape[:2], np.float32)
    for side in ("right", "left"):
        up = pts[UPPER[side]]
        lo = pts[LOWER[side]]
        lid = up + closure * (lo - up)
        curve = lid.copy()
        curve[:, 1] += 1.5 * np.sin(np.linspace(0, np.pi, len(curve))) * closure  # sag in the middle
        cv2.polylines(lash, [curve.astype(np.int32)], False, 1.0, thickness=max(2, round(2 * closure + 1)))
    lash = cv2.GaussianBlur(lash, (3, 3), 0)[..., None] * min(1.0, closure * 1.4)
    dark = np.array([35, 30, 40], np.float32)
    return (out * (1 - lash) + dark * lash).astype(np.uint8)


def profile(t: float, blinks: list[tuple[float, float]]) -> float:
    v = 0.0
    for start, length in blinks:
        if start <= t <= start + length:
            v = max(v, 0.5 * (1 - math.cos(2 * math.pi * (t - start) / length)))
    return v


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--photo", type=Path, default=Path("tests/data/astronaut.jpg"))
    ap.add_argument("--model", type=Path, default=Path("models/face_landmarker.task"))
    ap.add_argument("--out", type=Path, default=Path("tests/data"))
    args = ap.parse_args()

    base = cv2.imread(str(args.photo))
    lm = EyeLandmarker(args.model, video=False)
    pts = landmarks_px(lm, cv2.cvtColor(base, cv2.COLOR_BGR2RGB))

    specs = {
        "blinks_synthetic": {
            "fps": 30,
            "seconds": 10.0,
            "blinks": [(2.0, 0.30), (4.5, 0.30), (5.1, 0.30), (8.0, 0.25)],
        },
        "no_blinks_synthetic": {"fps": 30, "seconds": 5.0, "blinks": []},
        "long_closure_synthetic": {"fps": 30, "seconds": 6.0, "blinks": [(1.0, 2.5)]},
    }
    for name, spec in specs.items():
        fps, seconds = spec["fps"], spec["seconds"]
        h, w = base.shape[:2]
        path = args.out / f"{name}.mp4"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        for i in range(int(fps * seconds)):
            writer.write(render(base, pts, profile(i / fps, spec["blinks"])))
        writer.release()
        truth = {"fps": fps, "seconds": seconds, "blinks": [{"start_s": s, "length_s": ln} for s, ln in spec["blinks"]]}
        (args.out / f"{name}.json").write_text(json.dumps(truth, indent=2))
        print(name, path.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
