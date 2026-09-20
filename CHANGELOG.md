# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/), versioning: [SemVer](https://semver.org/).

## [Unreleased]

## [1.0.0]

### Changed
- Rebuilt from a Haar-cascade webcam script into a production-grade service.

### Added
- Pure blink state machine (hysteresis, long-closure classification, lost-face handling) with exhaustive tests.
- MediaPipe Face Landmarker wrapper with blendshape and landmark-geometry (EAR) signals, checksum-pinned model, instance pool.
- `POST /v1/eye-state`, `WS /v1/stream` (live, latest-frame-wins backpressure, token-bucket rate limit, session cap with close code 1013,
  per-frame `server_ms`), and asynchronous video jobs on NATS JetStream + S3.
- Hardened video ingestion: magic-byte container allow-list, header and runtime limits, temp-file hygiene.
- API-key auth (hashed), including WebSocket handshake auth (header or subprotocol, never URL).
- Prometheus metrics, OpenTelemetry tracing across API and worker, JSON logs, Grafana dashboard, alert rules including stream alerts.
- Docker image (non-root, read-only rootfs), Compose stack, Helm chart, CI/CD, documentation, model card, ADRs.
- Synthetic ground-truth video generator and fixtures.

### Fixed (found by this project's own tests and load runs)
- Worker classified every `AppError` as permanent, so an object-store outage would have failed jobs instead of retrying them.
  Classification is now by status: 4xx permanent, 5xx transient (regression tests included).
- WebSocket cleanup was cancelled together with the task on abrupt disconnects, leaking the native landmarker. Cleanup is now shielded.
- A strict minimum inter-frame gap dropped about a third of frames from a real 30 fps client; replaced by a token bucket.
- Capacity refusals surfaced as HTTP 403 (indistinguishable from an auth failure); now accept-then-close with 1013 and a reason.
