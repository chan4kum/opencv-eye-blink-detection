# Model card: MediaPipe Face Landmarker for blink detection

| | |
|---|---|
| **Model** | MediaPipe Face Landmarker bundle (`face_landmarker.task`, float16, v1): BlazeFace short-range detector + FaceMesh-V2 (478 landmarks) + Blendshape V2 (52 scores) |
| **Source / license** | Google AI Edge. **Apache License 2.0**, as stated in the three linked model cards (BlazeFace Short Range, Face Mesh V2, Blendshape V2). See `models/README.md` for provenance and the pinned SHA-256 |
| **Task in this service** | Per-frame eye closure (from the `eyeBlinkLeft/Right` blendshapes, or landmark geometry) feeding a blink state machine |
| **Speed** | About 8 to 12 ms per frame on one CPU thread in this service (Apple M4 Pro, containerised). Not benchmarked elsewhere |
| **Intended use** | Blink counting/rate for UX research, accessibility tooling, driver/operator *attention aids*, liveness heuristics as one weak signal among several |
| **Out of scope** | Medical diagnosis, decisions with legal or similarly significant effect on a person, covert monitoring, employment or exam surveillance, safety-critical control without independent safeguards |

## What has and has not been validated

* **Validated (automated):** the blink state machine (26 tests across frame rates 10 to 60 fps, flicker, long closures, lost faces);
  the full pipeline on **synthetic** videos with known ground truth; the live WebSocket path on the same videos; resource limits and failure handling.
* **Synthetic ground truth, and its limits.** The test videos are a public-domain portrait whose eyelids are moved on a known schedule
  (`scripts/make_fixture_video.py`). With the landmark-geometry signal (`EB_SIGNAL=ear`) all 4 scheduled blinks are found with correct
  timing and no false positives, and a 2.5 s closure is classified as a long closure, not a blink. With the **blendshape** signal, the
  synthetic eyes only reach about 0.42 (below the 0.5 default threshold): the learned blendshape model does not treat painted eyelids as
  fully closed. This shows the blendshape *responds* to closure (0.04 open to 0.42 closed), but the **default thresholds (0.5 / 0.3) have
  not been validated against real blinks** and were chosen from typical blendshape ranges, not measured on a labelled dataset.
* **Not validated:** accuracy (precision/recall, timing error) on real people; performance across skin tones, ages, eyewear, ethnic eyelid
  morphology, lighting, camera quality, head pose, or frame rates below 10 fps.

**Before relying on this for anything that matters, evaluate it on labelled real-world blink data representative of your users and tune
`EB_BLINK_*` and `EB_SIGNAL` accordingly.** A public benchmark such as the Eyeblink8 or RT-BENE datasets is a reasonable starting point
(not run here; check each dataset's license and consent terms).

## Known limitations

* **Frame rate.** A blink lasts roughly 100 to 400 ms. Below about 10 fps blinks can fall between frames; the detector accepts single-frame
  closures at low fps but timing resolution is one frame interval.
* **Glasses, glare, heavy makeup, ptosis, or partially occluded eyes** change both signals. Squinting and bright light produce
  partial closures that may or may not cross the thresholds.
* **Looking down** lowers the geometric (EAR) signal; use the blendshape signal (default) unless you have a reason not to.
* **One face per frame** (`num_faces=1`): the most prominent face is analysed.
* **Video timing** uses the container's frame rate. Variable-frame-rate files are analysed as constant-rate.
* **Live timing** uses server receive time; network jitter widens durations by the jitter.
* **Blink rate** depends on task and is not a proxy for fatigue on its own. A long closure is reported separately; it is not a diagnosis.
* MediaPipe reports temporal "jitter" in landmarks under extreme conditions (per the Face Mesh model card); the state machine's hysteresis
  absorbs small flicker but not large systematic error.

## Privacy and responsible use

* Faces, eye state and blink patterns are personal data and can reveal sensitive information (fatigue, neurological or ophthalmic
  conditions, medication effects). In many jurisdictions face-derived data can be **biometric data** with heightened protection (GDPR
  Art. 9, BIPA, and others). You are responsible for a lawful basis, notice and consent, and retention limits.
* The service minimises retention: live frames are never stored, uploaded videos are deleted after processing, results contain
  timestamps and scores but no pixels, and everything expires by TTL. It never writes images to logs.
* Do not use this software to monitor people without their knowledge or in contexts prohibited by law or policy (for example
  workplace productivity scoring or exam proctoring without a lawful, proportionate basis and consent).
* Fairness has **not been audited** for this pipeline. The upstream Face Mesh card describes fairness evaluation across geographic
  subregions for landmarks, but not for blink detection built on top of them.
