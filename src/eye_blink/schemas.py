"""Public API models (also the source of the generated OpenAPI document)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from eye_blink.blink import BlinkEvent
from eye_blink.jobs import JobStatus
from eye_blink.landmarker import EyeState, SignalMode


class ModelInfo(BaseModel):
    name: str
    sha256: str
    signal: SignalMode


class ImageInfo(BaseModel):
    width: int
    height: int


class EyeStateResponse(BaseModel):
    """Eye state of one frame (not a blink: a blink needs a time series)."""

    request_id: str
    image: ImageInfo
    face_found: bool
    closure: float | None = Field(None, ge=0, le=1, description="Signal used for blink detection (0 open .. 1 closed)")
    eyes_closed: bool | None = Field(None, description="closure above the close threshold")
    blink_score_left: float | None = None
    blink_score_right: float | None = None
    ear_left: float | None = None
    ear_right: float | None = None
    inference_ms: float
    model: ModelInfo

    @classmethod
    def build(
        cls,
        *,
        request_id: str,
        image: ImageInfo,
        state: EyeState | None,
        signal: SignalMode,
        close_threshold: float,
        inference_ms: float,
        model: ModelInfo,
    ) -> EyeStateResponse:
        if state is None:
            return cls(request_id=request_id, image=image, face_found=False, inference_ms=inference_ms, model=model)
        closure = state.closure_for(signal)
        return cls(
            request_id=request_id,
            image=image,
            face_found=True,
            closure=closure,
            eyes_closed=closure >= close_threshold,
            blink_score_left=state.blink_left,
            blink_score_right=state.blink_right,
            ear_left=state.ear_left,
            ear_right=state.ear_right,
            inference_ms=inference_ms,
            model=model,
        )


class BlinkOut(BaseModel):
    kind: Literal["blink", "long_closure"]
    start_ms: float
    end_ms: float
    duration_ms: float
    peak_closure: float

    @classmethod
    def from_event(cls, e: BlinkEvent) -> BlinkOut:
        return cls(
            kind=e.kind.value,
            start_ms=round(e.start_ms, 1),
            end_ms=round(e.end_ms, 1),
            duration_ms=round(e.duration_ms, 1),
            peak_closure=round(e.peak_closure, 3),
        )


class VideoInfoOut(BaseModel):
    width: int
    height: int
    fps: float
    duration_s: float
    frames_decoded: int
    frames_analyzed: int


class VideoAnalysisResult(BaseModel):
    video: VideoInfoOut
    face_found_ratio: float = Field(ge=0, le=1)
    blink_count: int
    long_closure_count: int
    discarded_closures: int = Field(description="Closures dropped as too short, or not observed to end")
    blinks_per_minute: float | None = Field(
        None, description="Over the time a face was visible; null when under 5 s of face data"
    )
    events: list[BlinkOut]
    processing_ms: float
    model: ModelInfo


class JobAccepted(BaseModel):
    job_id: str
    status: JobStatus
    status_url: str


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    created_at: float
    updated_at: float
    attempts: int
    result: VideoAnalysisResult | None = None
    error: str | None = None


class ProblemDetails(BaseModel):
    """RFC 9457 problem document."""

    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
    request_id: str | None = None
    errors: list[dict[str, object]] | None = None


class HealthResponse(BaseModel):
    status: str
    checks: dict[str, bool] = Field(default_factory=dict)
    version: str | None = None


class StreamFrameMessage(BaseModel):
    """Server -> client, one per analysed frame (documentation of the WebSocket protocol)."""

    type: Literal["frame"] = "frame"
    t_ms: int
    face_found: bool
    closure: float | None = None
    blinks: int
    server_ms: float = Field(description="Server-side time from frame receipt to this result (queueing + analysis)")


class StreamBlinkMessage(BaseModel):
    """Server -> client when a blink (or long closure) completes."""

    type: Literal["blink", "long_closure"]
    start_ms: float
    end_ms: float
    duration_ms: float
    blinks: int
