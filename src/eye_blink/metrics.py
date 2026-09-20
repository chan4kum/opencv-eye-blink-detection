"""Prometheus metrics. One registry per process (scale out with pods, not worker processes)."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

HTTP_REQUESTS = Counter("eb_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_DURATION = Histogram(
    "eb_http_request_duration_seconds", "HTTP request latency", ["method", "route"], buckets=_LATENCY_BUCKETS
)
HTTP_IN_FLIGHT = Gauge("eb_http_requests_in_flight", "HTTP requests currently being served")

INFERENCE_DURATION = Histogram(
    "eb_inference_duration_seconds",
    "Model inference latency (pre-process + ONNX Runtime + post-process)",
    ["source"],
    buckets=_LATENCY_BUCKETS,
)
BLINKS_DETECTED = Counter("eb_blinks_detected_total", "Blinks detected", ["source", "kind"])
FRAMES_ANALYZED = Counter("eb_frames_analyzed_total", "Frames analysed", ["source", "face"])
VIDEO_ANALYSIS_DURATION = Histogram(
    "eb_video_analysis_seconds",
    "Wall-clock time to analyse one video",
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
STREAM_SESSIONS = Gauge("eb_stream_active_sessions", "Live WebSocket sessions")
STREAM_SESSIONS_TOTAL = Counter("eb_stream_sessions_closed_total", "WebSocket sessions by close reason", ["reason"])
STREAM_FRAMES_DROPPED = Counter("eb_stream_frames_dropped_total", "Frames dropped before analysis", ["reason"])
INFERENCE_REJECTED = Counter(
    "eb_inference_rejected_total", "Requests shed because inference capacity was exhausted", ["reason"]
)
IMAGES_REJECTED = Counter("eb_images_rejected_total", "Inputs rejected before inference", ["reason"])

JOBS_SUBMITTED = Counter("eb_jobs_submitted_total", "Async jobs accepted")
JOBS_PROCESSED = Counter("eb_jobs_processed_total", "Async jobs finished by workers", ["outcome"])
JOB_E2E_DURATION = Histogram(
    "eb_job_end_to_end_seconds",
    "Time from job submission to completion",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300),
)
WORKER_IN_FLIGHT = Gauge("eb_worker_jobs_in_flight", "Jobs currently being processed by this worker")

BUILD_INFO = Gauge("eb_build_info", "Build information", ["version", "model_sha256", "signal"])
