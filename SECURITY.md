# Security policy

## Reporting a vulnerability

Please **do not open a public issue**. Report privately through GitHub:
**Security tab -> "Report a vulnerability"** on this repository
(https://github.com/chan4kum/opencv-eye-blink-detection/security/advisories/new).

Include affected version, reproduction steps and impact. You will get an acknowledgement within 5 business days.
Only the latest release receives security fixes.

## Security design (summary)

| Concern | Mitigation |
|---|---|
| Untrusted images | Format allow-list, header-only pixel budget *before* decode, body size cap enforced while streaming, bounded inference concurrency with load shedding |
| Untrusted video (FFmpeg decode) | Container allow-list from magic bytes, header limits, **runtime frame/pixel/time budgets that do not trust the header**, private `0600` temp file always removed, non-root read-only container |
| Live stream (WebSocket) | Auth at the handshake (bearer header or subprotocol, never the URL), frame-size and rate limits, per-replica session cap, idle and maximum-duration timeouts, latest-frame-wins backpressure, cancellation-safe cleanup |
| Authentication | Bearer API keys; only SHA-256 digests are configured; constant-time comparison; production refuses to start without keys unless explicitly opted out |
| Tenant isolation | Async jobs are bound to the creating key; other keys get `404` |
| Supply chain | Model pinned by SHA-256 and verified at start; dependencies locked (`uv.lock`); Dependabot; Trivy image scan, CodeQL and gitleaks in CI; images signed with cosign (keyless) with SBOM and provenance |
| Container | Non-root (UID 10001), read-only root filesystem, all capabilities dropped, `no-new-privileges`, seccomp `RuntimeDefault`, no secrets in the image |
| Data minimisation | Live frames never stored; uploaded videos deleted after processing plus bucket lifecycle expiry; job records expire by TTL; no pixels in logs |
| Error handling | RFC 9457 problem responses that never include stack traces or internal paths; a request ID is returned for correlation |
| Kubernetes | NetworkPolicies (default-deny ingress/egress with explicit allows), PodDisruptionBudgets, no service-account token mounted |

## Operator responsibilities

* **Rate limiting** is not implemented in the application (a per-process limiter is not meaningful when scaled out).
  Enforce it at the ingress / API gateway (see the ingress annotation example in `values.yaml`).
* **TLS** is terminated at the ingress or service mesh; the service speaks plain HTTP inside the cluster.
* **WebSocket ingress:** configure long read/send timeouts and an origin policy at the ingress; browsers send `Origin` and the service applies `EB_CORS_ALLOW_ORIGINS` only to HTTP, so restrict WebSocket origins at the gateway if browsers connect directly.
* **`/metrics`** is unauthenticated by convention. Do not expose it through the public ingress (the chart does not).
* Restrict who can read the NATS KV bucket and the object bucket; use NATS authentication/TLS in production
  (the bundled chart is configured without auth for local use).
* Rotate API keys by adding the new digest, migrating clients, then removing the old digest.
