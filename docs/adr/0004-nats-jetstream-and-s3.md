# ADR 0004: NATS JetStream for queueing and job state, S3 API for image payloads

**Status:** accepted

**Context.** We need a cloud-agnostic, open-source, horizontally scalable asynchronous pipeline. Options: Redis (streams/Celery),
Kafka, RabbitMQ, NATS JetStream, cloud-native queues (SQS/PubSub, not portable).

**Decision.** Use NATS JetStream: a work-queue stream with a shared durable pull consumer, plus a JetStream KV bucket for job
state/results. Images go to an S3-compatible object store, never through the bus.

**Why.** Apache-2.0, a single small binary, built-in persistence/replication, at-least-once delivery with ack-wait, max-deliver
and back-off, KV with TTL (no separate database), header propagation for tracing, and KEDA has a native scaler. Kafka is heavier
than this workload needs; Redis' recent license changes and weaker delivery semantics made it a poorer default. The S3 API is the
de-facto portable object interface (AWS S3, MinIO, SeaweedFS, Ceph, R2).

**Consequences.**
- (+) One dependency for queue + state; API stays stateless; workers scale on consumer lag.
- (+) Storage backend is swappable through configuration only (`EB_S3_ENDPOINT_URL`).
- (-) JetStream KV is not a general database. Results are ephemeral (TTL) by design; persistent results belong in the caller's store.
- (-) Exactly-once is not provided; handlers are idempotent instead.
- SeaweedFS (Apache-2.0) is used for local/CI S3; MinIO's community edition licensing/distribution has been in flux.
