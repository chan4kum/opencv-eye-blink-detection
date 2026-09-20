# Deployment guide

The service is cloud-agnostic: it needs a Kubernetes cluster (or just Docker), a NATS JetStream server and an
S3-compatible bucket. Nothing in the image or chart is specific to one provider.

## 1. Local (Docker Compose)

```bash
docker compose --profile observability up -d --build --wait
```

| What | Where |
|---|---|
| API + OpenAPI docs | http://localhost:8000 (`/docs`) |
| Grafana (dashboard "Eye Blink Detection Service") | http://localhost:3000 |
| Prometheus | http://localhost:9090 |
| Jaeger (traces: set `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318` in `.env`) | http://localhost:16686 |

Host ports are overridable (`.env.example`). Everything runs read-only, non-root, with dropped capabilities.
Auth is off by default locally; set `EB_API_KEY_HASHES` to enable it.

## 2. Kubernetes (Helm)

Prerequisites: Kubernetes >= 1.28, a StorageClass (for NATS' JetStream volume), an ingress controller if you expose the API.

```bash
# generate a key; keep the key, put only the digest in the cluster
uv run eye-blink keygen

helm dependency build deploy/helm/eye-blink
helm install fd deploy/helm/eye-blink \
  --namespace eye-blink --create-namespace \
  --set auth.apiKeyHashes=<digest> \
  --set storage.endpointUrl=<s3-endpoint> --set storage.existingSecret=<secret> \
  --set api.ingress.enabled=true --set api.ingress.hosts[0].host=blinks.example.com
helm test fd -n eye-blink
```

Published chart and image (after a release tag): `oci://ghcr.io/chan4kum/charts/eye-blink`,
`ghcr.io/chan4kum/opencv-eye-blink`. Verify the signature before deploying:

```bash
cosign verify ghcr.io/chan4kum/opencv-eye-blink@sha256:<digest> \
  --certificate-identity-regexp 'https://github.com/chan4kum/opencv-eye-blink/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

### Production checklist

- [ ] `image.digest` pinned (not a mutable tag)
- [ ] `auth.apiKeyHashes` (or `existingSecret`) set; `auth.disabled=false`
- [ ] NATS as a **3-node cluster** with persistence: `nats.config.cluster.enabled=true`, `async.replicas=3`; or an external managed NATS (`nats.enabled=false`, `async.natsUrl=...`) with authentication and TLS
- [ ] Object bucket provisioned by IaC, encryption at rest on, lifecycle rule expiring `inputs/` after 1 day (or `storage.ensureBucket=true`)
- [ ] Rate limiting, `proxy-body-size: 50m` (videos) and long WebSocket timeouts (`proxy-read-timeout` / `proxy-send-timeout` >= `streamMaxDurationS`) configured on the ingress (see `values.yaml` annotations)
- [ ] `config.streamMaxSessions` sized to the pod CPU: each live session holds one MediaPipe instance and roughly one core at 30 fps
- [ ] `networkPolicy.enabled=true` with `apiIngressFrom` / `metricsFrom` set for your ingress and monitoring namespaces
- [ ] `serviceMonitor`, `prometheusRule`, `grafanaDashboard` enabled (needs prometheus-operator)
- [ ] `otel.endpoint` pointing at your collector
- [ ] Autoscaling tuned: API on CPU; workers on queue lag with KEDA (`worker.keda.enabled=true`)
- [ ] PodDisruptionBudgets fit your replica counts

## 3. AWS (optional)

The same chart runs unchanged on EKS; only the backing services differ. **This repository does not create any AWS
resources.** If you provision them, this is the mapping, and remember to delete anything you stop using:

| Need | AWS option | Chart settings |
|---|---|---|
| Kubernetes | EKS | none |
| Object storage | S3 bucket (SSE-S3 or SSE-KMS, lifecycle rule on `inputs/`, block public access) | `storage.endpointUrl=""`, `storage.forcePathStyle=false`, `storage.region`, `storage.serverSideEncryption=aws:kms` |
| Credentials | **IRSA / EKS Pod Identity**, no static keys | `serviceAccount.annotations."eks.amazonaws.com/role-arn"=arn:aws:iam::<acct>:role/<role>`, leave `storage.existingSecret` empty |
| Queue | NATS in-cluster (StatefulSet, gp3 volumes) | `nats.*` |
| Ingress / TLS | AWS Load Balancer Controller + ACM | `api.ingress.className=alb` + annotations |
| Registry | ECR (mirror the GHCR image) or GHCR directly | `image.repository` |

Minimum S3 IAM policy for the workload role (bucket `eye-blink`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"], "Resource": "arn:aws:s3:::eye-blink/inputs/*"},
    {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": "arn:aws:s3:::eye-blink"}
  ]
}
```

(`readyz` uses `HeadBucket`, which requires `s3:ListBucket`. Add `s3:CreateBucket` and `s3:PutLifecycleConfiguration` only if you enable `storage.ensureBucket`.)

## 4. Upgrades and rollbacks

* `helm upgrade` performs a zero-downtime rolling update (`maxUnavailable: 0`, readiness gates, `preStop` drain). Config changes roll pods through a checksum annotation.
* Workers finish in-flight jobs on SIGTERM. Unfinished jobs are redelivered, and handling is idempotent.
* `helm rollback <release> <revision>` reverts chart and image together.
* JetStream stream/KV settings are reconciled on start (`EB_NATS_PROVISION=true`). Set it to `false` and manage them yourself if you prefer.
