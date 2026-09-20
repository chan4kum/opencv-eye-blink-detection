"""Generate the Grafana dashboard JSON (kept as code so panels stay reviewable)."""

import json
import sys

DS = {"type": "prometheus", "uid": "prometheus"}


def panel(
    pid: int,
    title: str,
    exprs: list[tuple[str, str]],
    x: int,
    y: int,
    unit: str = "short",
    w: int = 12,
    h: int = 8,
    desc: str = "",
) -> dict:
    return {
        "id": pid,
        "type": "timeseries",
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 8}}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [
            {"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": legend}
            for i, (e, legend) in enumerate(exprs)
        ],
    }


def q(p: float, metric: str, by: str = "", extra: str = "") -> str:
    return f"histogram_quantile({p}, sum by (le{by}) (rate({metric}_bucket{extra}[$__rate_interval])))"


panels = [
    panel(
        1,
        "Request rate by route",
        [
            (
                'sum by (route) (rate(eb_http_requests_total{route!~"/healthz|/readyz|/metrics"}[$__rate_interval]))',
                "{{route}}",
            )
        ],
        0,
        0,
        "reqps",
    ),
    panel(
        2,
        "Error ratio (5xx)",
        [
            (
                'sum(rate(eb_http_requests_total{status=~"5.."}[$__rate_interval])) / clamp_min(sum(rate(eb_http_requests_total[$__rate_interval])), 1e-9)',
                "5xx ratio",
            )
        ],
        12,
        0,
        "percentunit",
    ),
    panel(
        3,
        "Latency: /v1/eye-state",
        [
            (q(0.5, "eb_http_request_duration_seconds", extra='{route="/v1/eye-state"}'), "p50"),
            (q(0.95, "eb_http_request_duration_seconds", extra='{route="/v1/eye-state"}'), "p95"),
            (q(0.99, "eb_http_request_duration_seconds", extra='{route="/v1/eye-state"}'), "p99"),
        ],
        0,
        8,
        "s",
    ),
    panel(
        4,
        "Model inference latency",
        [
            (q(0.5, "eb_inference_duration_seconds", ",source"), "p50 {{source}}"),
            (q(0.95, "eb_inference_duration_seconds", ",source"), "p95 {{source}}"),
        ],
        12,
        8,
        "s",
    ),
    panel(
        5,
        "In-flight HTTP requests / worker jobs",
        [
            ("sum(eb_http_requests_in_flight)", "http in flight"),
            ("sum(eb_worker_jobs_in_flight)", "worker jobs in flight"),
        ],
        0,
        16,
    ),
    panel(
        6,
        "Load shedding & rejected inputs",
        [
            ("sum by (reason) (rate(eb_inference_rejected_total[$__rate_interval]))", "shed: {{reason}}"),
            ("sum by (reason) (rate(eb_images_rejected_total[$__rate_interval]))", "rejected: {{reason}}"),
        ],
        12,
        16,
        "ops",
    ),
    panel(
        7,
        "Async jobs by outcome",
        [
            ("sum by (outcome) (rate(eb_jobs_processed_total[$__rate_interval]))", "{{outcome}}"),
            ("sum(rate(eb_jobs_submitted_total[$__rate_interval]))", "submitted"),
        ],
        0,
        24,
        "ops",
    ),
    panel(
        8,
        "Job end-to-end latency (submit to done)",
        [(q(0.5, "eb_job_end_to_end_seconds"), "p50"), (q(0.95, "eb_job_end_to_end_seconds"), "p95")],
        12,
        24,
        "s",
    ),
    panel(
        9,
        "Queue depth (pending jobs)",
        [
            ('max(nats_consumer_num_pending{consumer_name="eb-workers"})', "pending"),
            ('max(nats_consumer_num_ack_pending{consumer_name="eb-workers"})', "ack pending"),
        ],
        0,
        32,
        desc="Requires the NATS Prometheus exporter.",
    ),
    panel(
        10,
        "Blinks detected (rate)",
        [("sum by (source, kind) (rate(eb_blinks_detected_total[$__rate_interval]))", "{{source}} {{kind}}")],
        12,
        32,
        "ops",
    ),
    panel(
        11,
        "Live stream sessions",
        [
            ("sum(eb_stream_active_sessions)", "active"),
            ("sum by (reason) (rate(eb_stream_sessions_closed_total[$__rate_interval]))", "closed: {{reason}}"),
        ],
        0,
        40,
    ),
    panel(
        12,
        "Live frames dropped by reason",
        [("sum by (reason) (rate(eb_stream_frames_dropped_total[$__rate_interval]))", "{{reason}}")],
        12,
        40,
        "ops",
        desc="stale = analysis is behind the client; rate_limited = client above EB_STREAM_MAX_FPS.",
    ),
    panel(
        13,
        "Video analysis time (p50 / p95)",
        [(q(0.5, "eb_video_analysis_seconds"), "p50"), (q(0.95, "eb_video_analysis_seconds"), "p95")],
        0,
        48,
        "s",
    ),
    panel(
        14,
        "Frames analysed: face found vs not",
        [("sum by (source, face) (rate(eb_frames_analyzed_total[$__rate_interval]))", "{{source}} face={{face}}")],
        12,
        48,
        "ops",
    ),
]
dash = {
    "uid": "eye-blink",
    "title": "Eye Blink Detection Service",
    "schemaVersion": 39,
    "version": 1,
    "editable": True,
    "refresh": "10s",
    "time": {"from": "now-1h", "to": "now"},
    "tags": ["eye-blink"],
    "panels": panels,
}
json.dump(dash, sys.stdout, indent=2)
print()
