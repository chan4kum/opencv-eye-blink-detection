# ADR 0001: MediaPipe Face Landmarker for eye closure (departing from the ONNX-only stack)

**Status:** accepted (user decision, 2026-09-20)

**Context.** The sibling `face-detection` service uses YuNet on ONNX Runtime. Blink detection needs eye *closure*, which YuNet's
five landmarks cannot express. Options: MediaPipe Face Landmarker (478 landmarks + blendshapes, Apache-2.0), or convert a permissively
licensed 68-point model to ONNX and compute the eye aspect ratio (EAR) only.

**Decision.** Use MediaPipe Face Landmarker. It provides learned blendshape scores for eye closure (robust to gaze and pose) *and*
landmarks for a geometric fallback, in one 3.7 MB model.

**Consequences.**
- (+) Accurate, fast (about 10 ms per frame), permissive license, two independent closure signals.
- (-) Introduces the `mediapipe` runtime (native, Linux/macOS/Windows), so this project is not ONNX-only.
- (-) `mediapipe` hard-depends on the GUI build of OpenCV, which needs X11 libraries and conflicts with `opencv-python-headless`. We
  exclude it with a `uv` override and depend on the headless build. The image also needs `libegl1` and `libgles2`.
- (-) MediaPipe's macOS build aborts in some sandboxes (Metal service unavailable) even with the CPU delegate. Development there uses
  the Linux test container; CI and production are Linux.
- (-) MediaPipe landmarkers are not documented as thread-safe: image requests use a fixed-size pool; each live session owns its own instance.
