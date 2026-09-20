"""Blink detection as a pure, deterministic state machine over an eye-closure signal.

Input: one ``closure`` value per frame in [0, 1] (0 = eyes fully open, 1 = fully closed) with its
timestamp, or ``None`` when no face is visible. The detector is model-agnostic and has no I/O,
so it is exhaustively unit-testable.

A blink is a closure that (1) rises above ``close_threshold``, (2) falls back below
``open_threshold`` (hysteresis prevents flicker from splitting or duplicating events) and
(3) lasts between ``min_duration_ms`` and ``max_duration_ms``. Longer closures are reported
separately as *long closures* (drowsiness-relevant), not as blinks.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class BlinkConfig:
    close_threshold: float = 0.55
    open_threshold: float = 0.35
    min_duration_ms: float = 50.0
    max_duration_ms: float = 700.0
    # If the face disappears for longer than this while an eye is closed, the closure is discarded:
    # we cannot tell whether the eyes reopened.
    max_face_gap_ms: float = 400.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.open_threshold < self.close_threshold <= 1.0:
            raise ValueError("require 0 <= open_threshold < close_threshold <= 1")
        if not 0.0 <= self.min_duration_ms < self.max_duration_ms:
            raise ValueError("require 0 <= min_duration_ms < max_duration_ms")
        if self.max_face_gap_ms < 0.0:
            raise ValueError("max_face_gap_ms must be >= 0")


class EventKind(StrEnum):
    BLINK = "blink"
    LONG_CLOSURE = "long_closure"


@dataclass(frozen=True, slots=True)
class BlinkEvent:
    kind: EventKind
    start_ms: float
    end_ms: float
    peak_closure: float

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms


class BlinkDetector:
    """Feed frames in strictly increasing time order with :meth:`update`; call :meth:`finish` at the end."""

    def __init__(self, config: BlinkConfig | None = None) -> None:
        self.config = config or BlinkConfig()
        self._closed_since: float | None = None
        self._peak = 0.0
        self._last_seen_ms: float | None = None  # last frame time with a face
        self._last_t: float | None = None
        self.blink_count = 0
        self.long_closure_count = 0
        self.discarded_count = 0

    def update(self, t_ms: float, closure: float | None) -> list[BlinkEvent]:
        if self._last_t is not None and t_ms <= self._last_t:
            raise ValueError(f"timestamps must strictly increase (got {t_ms} after {self._last_t})")
        self._last_t = t_ms
        cfg = self.config

        if closure is None:
            if (
                self._closed_since is not None
                and self._last_seen_ms is not None
                and t_ms - self._last_seen_ms > cfg.max_face_gap_ms
            ):
                self._reset()
                self.discarded_count += 1
            return []
        if not 0.0 <= closure <= 1.0:
            raise ValueError(f"closure must be within [0, 1], got {closure}")
        self._last_seen_ms = t_ms

        if self._closed_since is None:
            if closure >= cfg.close_threshold:
                self._closed_since = t_ms
                self._peak = closure
            return []

        self._peak = max(self._peak, closure)
        if closure <= cfg.open_threshold:
            event = self._emit(end_ms=t_ms)
            self._reset()
            return [event] if event else []
        return []

    def finish(self) -> list[BlinkEvent]:
        """Close out the stream. A closure still open at the end is discarded (its end is unknown)."""
        if self._closed_since is not None:
            self.discarded_count += 1
            self._reset()
        return []

    def _emit(self, end_ms: float) -> BlinkEvent | None:
        assert self._closed_since is not None
        start = self._closed_since
        duration = end_ms - start
        cfg = self.config
        if duration < cfg.min_duration_ms:
            self.discarded_count += 1
            return None
        if duration > cfg.max_duration_ms:
            self.long_closure_count += 1
            return BlinkEvent(EventKind.LONG_CLOSURE, start, end_ms, self._peak)
        self.blink_count += 1
        return BlinkEvent(EventKind.BLINK, start, end_ms, self._peak)

    def _reset(self) -> None:
        self._closed_since = None
        self._peak = 0.0
