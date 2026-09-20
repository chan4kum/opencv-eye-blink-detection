# Runbook

Each alert in `deploy/prometheus/alerts.yml` links to a section here. Metric names are `eb_*` (see ARCHITECTURE.md).

## Quick triage

```bash
kubectl get pods -l app.kubernetes.io/name=eye-blink
kubectl logs deploy/<release>-eye-blink-api --tail=100 | jq -c 'select(.level!="info")'
kubectl logs deploy/<release>-eye-blink-worker --tail=100 | jq -c .
curl -s localhost:8000/readyz            # which dependency is failing
```

Every response carries `X-Request-ID`; logs and traces are keyed by it (`request_id`, `trace_id`).

## High error rate
*Alert `EyeBlinkHighErrorRate`: more than 2% of requests are 5xx for 10 min.*

1. Split by status/route: `sum by (route,status) (rate(eb_http_requests_total{status=~"5.."}[5m]))`.
2. `503` with `overloaded` = capacity (see Load shedding). `503` with `dependency-unavailable` = NATS or S3 (see below).
3. `500` = bug: find the `unhandled_exception` log line with the request ID; open a bug with the trace.
4. Recently deployed? `helm rollback <release>`.

## High latency
*Alert `EyeBlinkHighLatency`: p95 of `/v1/eye-state` above 500 ms for 10 min.*

1. Compare `eb_inference_duration_seconds` to `eb_http_request_duration_seconds`. If inference is fast but requests slow, the time is
   queueing (see Load shedding) or large uploads.
2. Very large images cost more (decode + down-scale). Check `image.width/height` span attributes.
3. CPU throttling? The chart sets no CPU limit on purpose; check node contention.

## Load shedding
*Alert `EyeBlinkLoadShedding`: requests are answered 503 `overloaded` (`eb_inference_rejected_total`).*

* Scale out: raise API `maxReplicas`, or lower the HPA CPU target.
* Or raise `config.maxConcurrentInference` together with the CPU request (one slot is roughly one core).
* Clients should honour `Retry-After` and use the async API for bursts.

## Jobs failing
*Alert `EyeBlinkJobsFailing`: `eb_jobs_processed_total{outcome="failed_exhausted"|"poison"}` is increasing.*

`failed_permanent` is normal (bad user input). `failed_exhausted` means an infrastructure fault persisted through all retries:

1. Worker logs: `job_retry` / `job_retries_exhausted` include the error.
2. Object storage reachable? `kubectl exec` a worker and run `python -c "from eye_blink.storage import ObjectStore; ..."`, or check `/readyz` on the API.
3. NATS KV writable? `nats kv status eb_jobs` (nats-box).
4. `poison` messages: something other than this API published to the subject. Inspect with `nats stream view EB_JOBS`.

## Queue backlog
*Alert `EyeBlinkQueueBacklog`: more than 500 pending messages for 10 min.*

1. Are workers running and ready? `kubectl get pods -l app.kubernetes.io/component=worker`.
2. Enable KEDA (`worker.keda.enabled=true`) or raise `worker.autoscaling.maxReplicas`.
3. `nats consumer info EB_JOBS fd-workers`: high `Ack Pending` with no progress means stuck jobs; they are redelivered after `job_ack_wait_s`.
4. Do NOT purge the stream unless you accept losing those jobs (their inputs stay in the bucket until lifecycle expiry).

## Stream at capacity
*Alert `EyeBlinkStreamAtCapacity`: `eb_stream_sessions_closed_total{reason="at_capacity"}` is increasing.*

Clients receive close code `1013` ("server at capacity, retry later"); a well-behaved client backs off and reconnects.

1. `sum(eb_stream_active_sessions)` vs `replicas * config.streamMaxSessions`: are all replicas full?
2. Scale out the API (HPA max, or lower the CPU target). Sessions are not sticky, so new replicas take new connections immediately.
3. Only raise `config.streamMaxSessions` together with the CPU request: about one core sustains roughly 16 concurrent 30 fps sessions on
   modern hardware (measured), but size for your frame size and CPU, and watch `server_ms` in the `frame` messages.
4. Check for stuck sessions: `eb_stream_sessions_closed_total{reason}` should show `client_closed` and `idle_timeout`. A gauge that never falls
   suggests clients that stay connected but idle; lower `config.streamIdleTimeoutS`.

## Stream frames dropped
*Alert `EyeBlinkStreamFramesDropped`: over 20% of live frames are dropped as `stale`.*

`stale` means analysis is behind the client, so the server keeps only the newest frame (latency stays bounded; frames are lost).
`rate_limited` means the client exceeds `config.streamMaxFps` on a sustained basis.

1. `histogram_quantile(0.95, ...)` of the `server_ms` field is not a metric; sample it from a client, or compare `eb_inference_duration_seconds`.
2. CPU-bound? Check pod CPU and node contention. Scale out or reduce the client frame rate (15 fps is enough for blinks of 100 ms or longer).
3. Large frames cost more (decode + landmarks). Ask clients to downscale to about 640x480.

## Video jobs failing with `video-limit-exceeded`
Input problem, not an outage (`failed_permanent`). The client-visible error says which limit (pixels, duration, frame count, time budget).
Raise `config.maxVideo*` only if the workers have the CPU and `/tmp` space (`emptyDir` is 512 Mi).

## Instance down
Check `kubectl describe pod` (OOMKilled? raise the memory limit; probes failing?) and the startup log line `started`.
If the pod exits at start with `model checksum mismatch`, the image is corrupted or the model was replaced: rebuild from a clean checkout.
If startup hangs, the API waits for NATS/JetStream at start; check the NATS pods first.

## Common operations

| Task | How |
|---|---|
| Rotate an API key | `eye-blink keygen`; add the new digest to `auth.apiKeyHashes`, roll clients, remove the old digest |
| Change thresholds | `config.scoreThreshold` etc. in values, `helm upgrade` (pods roll via config checksum) |
| Roll back | `helm rollback <release> <revision>` |
| Drain a worker | `kubectl delete pod` is safe: SIGTERM stops pulling, finishes in-flight jobs; anything unfinished is redelivered |
| Verify a model file | `eye-blink verify-model --model path.onnx` |
