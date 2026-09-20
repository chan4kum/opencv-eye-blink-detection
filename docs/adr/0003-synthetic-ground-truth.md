# ADR 0003: Synthetic blink videos as the ground truth for pipeline tests

**Status:** accepted, with explicit limits

**Context.** End-to-end tests need videos with *known* blink times. Licensed, consented, labelled blink footage is not freely available for
redistribution in a public repository, and recording people raises consent and privacy issues.

**Decision.** Generate videos from a public-domain NASA portrait by moving the eyelids on a known schedule (real face detected with MediaPipe,
eyelid region repainted, dark lash line drawn where the lids meet).

**Consequences.**
- (+) Deterministic, redistributable, and precise: the tests can assert blink counts, timing and the long-closure distinction.
- (+) They exercised real problems (frame dropping in the live stream, the model's response to synthetic eyes, timestamp handling).
- (-) **Not real blinks.** The learned blendshape signal does not treat painted eyelids as fully closed (peaks near 0.42), so pipeline tests use the
  landmark-geometry signal. These tests validate plumbing and thresholds' *mechanics*, not accuracy on people.
- (-) The default blendshape thresholds are unvalidated on real data (stated in the model card). Evaluation on a labelled real dataset is the
  recommended next step before any consequential use.
