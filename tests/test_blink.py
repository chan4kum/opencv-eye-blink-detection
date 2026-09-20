from __future__ import annotations

import math

import pytest

from eye_blink.blink import BlinkConfig, BlinkDetector, BlinkEvent, EventKind


def signal(
    fps: float, duration_s: float, blinks: list[tuple[float, float]], base: float = 0.05
) -> list[tuple[float, float]]:
    """Synthetic closure signal: raised-cosine blinks given as (start_s, length_s)."""
    n = int(duration_s * fps)
    out = []
    for i in range(n):
        t = i / fps
        v = base
        for s, length in blinks:
            if s <= t <= s + length:
                v = max(v, 0.5 * (1 - math.cos(2 * math.pi * (t - s) / length)) if length > 0 else 1.0)
        out.append((t * 1000.0, min(1.0, v)))
    return out


def run(
    samples: list[tuple[float, float | None]], cfg: BlinkConfig | None = None
) -> tuple[BlinkDetector, list[BlinkEvent]]:
    det = BlinkDetector(cfg)
    events: list[BlinkEvent] = []
    for t, c in samples:
        events += det.update(t, c)
    events += det.finish()
    return det, events


@pytest.mark.parametrize("fps", [10, 15, 24, 30, 60])
def test_single_blink_detected_once_at_any_frame_rate(fps: int) -> None:
    det, ev = run(signal(fps, 3.0, [(1.0, 0.30)]))
    assert det.blink_count == 1 and len(ev) == 1 and ev[0].kind is EventKind.BLINK
    assert 1000 <= ev[0].start_ms <= 1300 and 100 <= ev[0].duration_ms <= 400
    assert ev[0].peak_closure > 0.7  # sampling at low fps can miss the true peak


def test_multiple_blinks_are_counted_separately() -> None:
    det, ev = run(signal(30, 10.0, [(1.0, 0.3), (3.0, 0.3), (3.6, 0.3), (8.0, 0.25)]))
    assert det.blink_count == 4
    assert [round(e.start_ms / 1000) for e in ev] == [1, 3, 4, 8]


def test_no_blinks_in_flat_or_noisy_open_eyes() -> None:
    noisy = [(i * 33.3, 0.05 + 0.2 * abs(math.sin(i))) for i in range(300)]  # peaks 0.25 < close threshold
    det, ev = run(noisy)
    assert det.blink_count == 0 and ev == []


def test_hysteresis_prevents_splitting_a_blink() -> None:
    # closes, dips to 0.45 (between thresholds) mid-blink, then reopens: still ONE blink
    values = [0.05, 0.9, 0.45, 0.9, 0.1, 0.05]
    det, ev = run([(i * 50.0, v) for i, v in enumerate(values)])
    assert det.blink_count == 1 and len(ev) == 1


def test_dip_below_open_threshold_ends_the_blink_and_next_rise_starts_another() -> None:
    values = [0.05, 0.9, 0.1, 0.9, 0.1, 0.05]
    det, _ = run([(i * 60.0, v) for i, v in enumerate(values)])
    assert det.blink_count == 2


def test_long_closure_is_not_a_blink() -> None:
    det, ev = run(signal(30, 5.0, [(1.0, 2.0)]))
    # a 2 s cosine closure is above the close threshold for >700 ms
    assert det.blink_count == 0 and det.long_closure_count == 1 and ev[0].kind is EventKind.LONG_CLOSURE


def test_single_frame_spike_rejected_at_30fps_but_accepted_at_10fps() -> None:
    def spike(fps: float) -> list[tuple[float, float]]:
        return [(i * 1000 / fps, 0.9 if i == 5 else 0.05) for i in range(12)]

    assert run(spike(30))[0].blink_count == 0  # 33 ms < 50 ms minimum
    assert run(spike(10))[0].blink_count == 1  # 100 ms


def test_face_lost_briefly_mid_blink_still_counts() -> None:
    samples: list[tuple[float, float | None]] = [
        (0, 0.05),
        (50, 0.9),
        (100, None),
        (150, None),
        (200, 0.1),
        (250, 0.05),
    ]
    det, _ = run(samples)
    assert det.blink_count == 1


def test_face_lost_for_long_discards_the_open_closure() -> None:
    samples: list[tuple[float, float | None]] = [(0, 0.05), (50, 0.9), (100, None), (1000, None), (1050, 0.05)]
    det, ev = run(samples)
    assert det.blink_count == 0 and ev == [] and det.discarded_count == 1


def test_closure_open_at_end_of_stream_is_discarded_not_counted() -> None:
    det, ev = run([(0, 0.05), (50, 0.9), (100, 0.9)])
    assert det.blink_count == 0 and det.long_closure_count == 0 and det.discarded_count == 1 and ev == []


def test_no_face_at_all() -> None:
    det, ev = run([(i * 33.0, None) for i in range(50)])
    assert det.blink_count == 0 and ev == []


def test_timestamps_must_increase() -> None:
    det = BlinkDetector()
    det.update(100, 0.1)
    for bad in (100, 50):
        with pytest.raises(ValueError, match="strictly increase"):
            det.update(bad, 0.1)


@pytest.mark.parametrize("bad", [-0.01, 1.01, float("nan")])
def test_closure_out_of_range_is_rejected(bad: float) -> None:
    with pytest.raises(ValueError, match=r"closure must be within"):
        BlinkDetector().update(0, bad)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"open_threshold": 0.6, "close_threshold": 0.5},
        {"open_threshold": 0.5, "close_threshold": 0.5},
        {"close_threshold": 1.5},
        {"min_duration_ms": 800.0},
        {"max_face_gap_ms": -1.0},
    ],
)
def test_config_validation(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=r"require|must be"):
        BlinkConfig(**kwargs)


def test_custom_thresholds_change_sensitivity() -> None:
    weak = [(i * 40.0, v) for i, v in enumerate([0.05, 0.45, 0.45, 0.05, 0.05])]
    assert run(weak)[0].blink_count == 0
    assert run(weak, BlinkConfig(close_threshold=0.4, open_threshold=0.2))[0].blink_count == 1


def test_event_duration_property() -> None:
    e = BlinkEvent(EventKind.BLINK, 100.0, 250.0, 0.8)
    assert e.duration_ms == 150.0
