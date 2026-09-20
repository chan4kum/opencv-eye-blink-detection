"""Application errors, rendered as RFC 9457 ``application/problem+json``."""

from __future__ import annotations


class AppError(Exception):
    """An error that maps to a specific, safe-to-expose HTTP response."""

    status: int = 500
    code: str = "internal-error"
    title: str = "Internal server error"

    def __init__(self, detail: str | None = None, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.headers = headers or {}


class InvalidImageError(AppError):
    status, code, title = 422, "invalid-image", "The uploaded data is not a valid image"


class UnsupportedMediaTypeError(AppError):
    status, code, title = 415, "unsupported-media-type", "Unsupported image type"


class PayloadTooLargeError(AppError):
    status, code, title = 413, "payload-too-large", "Payload too large"


class UnauthorizedError(AppError):
    status, code, title = 401, "unauthorized", "Authentication required"


class NotFoundError(AppError):
    status, code, title = 404, "not-found", "Resource not found"


class OverloadedError(AppError):
    status, code, title = 503, "overloaded", "Service is at capacity"


class AsyncDisabledError(AppError):
    status, code, title = 501, "async-disabled", "The asynchronous job API is not enabled"


class DependencyUnavailableError(AppError):
    status, code, title = 503, "dependency-unavailable", "A required backing service is unavailable"


class InvalidVideoError(AppError):
    status, code, title = 422, "invalid-video", "The uploaded data is not a valid video"


class VideoLimitError(AppError):
    status, code, title = 422, "video-limit-exceeded", "The video exceeds the configured limits"
