# Model provenance

| | |
|---|---|
| File | `face_landmarker.task` (MediaPipe Face Landmarker bundle, float16, version 1) |
| Contents | FaceDetector (BlazeFace short-range, 192x192), FaceMesh-V2 (256x256, 478 landmarks), Blendshape V2 (52 blendshape scores) |
| Source | https://storage.googleapis.com/mediapipe-assets/ via `https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task` |
| Docs | https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker |
| SHA-256 | `64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff` |
| Size | 3,758,596 bytes |
| Retrieved | 2026-09-20 |
| License | Apache License 2.0, as stated in the three Google model cards linked from the docs page (BlazeFace Short Range, Face Mesh V2, Blendshape V2). See `LICENSE` (the MediaPipe repository license text). |

The service refuses to start if the file's SHA-256 differs from `EB_MODEL_SHA256` (default: the value above).
The URL is versioned (`float16/1`) but not content-addressed upstream, so the checksum is the source of truth.
Verify with `uv run eye-blink verify-model`.

To upgrade: replace the file, update the checksum here and in `src/eye_blink/config.py`, and re-run the test-suite.
