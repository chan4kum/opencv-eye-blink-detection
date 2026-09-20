# ADR 0005: Rate limiting is delegated to the edge

**Status:** accepted

**Context.** A per-process limiter is inaccurate when scaled to N replicas; a distributed limiter needs another stateful dependency
and adds latency to every request.

**Decision.** The application enforces *capacity protection* (bounded concurrency with 503 + `Retry-After`, body and pixel limits)
but not per-client quotas. Per-client rate limiting is configured at the ingress / gateway (example annotations in `values.yaml`).

**Consequences.** Simpler, faster service; operators must configure the edge. Documented in `SECURITY.md`.
