# ADR 0002: Live blink detection over a WebSocket with latest-frame-wins backpressure

**Status:** accepted

**Context.** Real-time blink events need a persistent, low-latency, bidirectional channel. Alternatives: HTTP long-polling or SSE plus
per-frame POSTs (per-request overhead, no ordering), WebRTC (heavy: media servers, ICE/TURN), gRPC streaming (poor browser support).

**Decision.** A WebSocket endpoint. Binary messages carry one frame each; JSON messages carry results. The server timestamps frames on receipt.
A single-slot mailbox holds only the newest unprocessed frame.

**Why.** Blink timing needs stable frame timing, but *latency matters more than completeness*: analysing a 2-second-old frame is useless.
Dropping stale frames keeps latency bounded when analysis lags, and the token-bucket limiter protects capacity from over-eager clients.
Server timestamps mean clients cannot skew durations and need no clock sync.

**Consequences.**
- (+) Simple for any client (browsers, scripts), works through ordinary ingress with long timeouts, no media server.
- (+) Measured: 16 concurrent 30 fps sessions on one container kept median server-side latency at about 12 ms.
- (-) Per-frame JPEG over TCP is less efficient than video codecs; acceptable at webcam resolutions.
- (-) Session state is per connection (no resumption). A dropped connection starts a new session.
- (-) Each session pins a native landmarker in memory; capacity is bounded per replica (`EB_STREAM_MAX_SESSIONS`).
