"""审计引擎单元测试：排序、滞回、缺测切片、峰值与合规判定。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import audit
from app.audit import (
    HIGH_EXCURSION,
    LOW_EXCURSION,
    MISSING,
    NORMAL,
    Reading,
)

BASE = datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc)


def r(offset_seconds: int, temperature: float, index: int = 0) -> Reading:
    return Reading(
        timestamp=BASE + timedelta(seconds=offset_seconds),
        temperature=temperature,
        source_index=index,
    )


def test_all_normal_single_segment_and_compliant() -> None:
    readings = [r(0, 4.0), r(60, 5.0), r(120, 3.0)]
    result = audit.analyze(
        readings,
        low_limit=2.0,
        high_limit=8.0,
        hysteresis=0.5,
        max_interval_seconds=900,
    )

    assert result.compliant is True
    assert len(result.segments) == 1
    segment = result.segments[0]
    assert segment.type == NORMAL
    assert segment.sample_count == 3
    assert segment.compliant is True
    assert segment.missing is False
    assert segment.peak_temperature is None
    assert segment.max_temperature == 5.0
    assert segment.min_temperature == 3.0
    assert segment.duration_seconds == 120
    assert result.missing_segment_count == 0


def test_hysteresis_prevents_jitter_from_splitting_event() -> None:
    # 允许区间 2~8，滞回 0.5：回到 7.6（>7.5）不解除高温事件。
    readings = [
        r(0, 7.0),
        r(60, 8.5),   # 进入高温
        r(120, 7.6),  # 高于解除线 7.5，仍为高温（抖动不切分）
        r(180, 9.0),
        r(240, 7.5),  # <= 7.5，解除
        r(300, 6.0),
    ]
    result = audit.analyze(
        readings,
        low_limit=2.0,
        high_limit=8.0,
        hysteresis=0.5,
        max_interval_seconds=900,
    )

    types = [s.type for s in result.segments]
    assert types == [NORMAL, HIGH_EXCURSION, NORMAL]
    excursion = result.segments[1]
    assert excursion.sample_count == 3
    assert excursion.peak_temperature == 9.0
    assert excursion.peak_at == BASE + timedelta(seconds=180)
    assert excursion.start_time == BASE + timedelta(seconds=60)
    assert excursion.end_time == BASE + timedelta(seconds=180)
    assert excursion.compliant is False
    assert result.high_excursion_count == 1
    assert result.compliant is False


def test_boundary_readings_are_normal_strict_comparison() -> None:
    readings = [r(0, 2.0), r(60, 8.0)]
    result = audit.analyze(
        readings,
        low_limit=2.0,
        high_limit=8.0,
        hysteresis=0.5,
        max_interval_seconds=900,
    )
    assert len(result.segments) == 1
    assert result.segments[0].type == NORMAL


def test_gap_over_max_interval_cuts_independent_missing_segment() -> None:
    readings = [
        r(0, 9.0),     # 高温
        r(1800, 9.0),  # 与前点相隔 1800s，严格大于 900s → 缺测切片
    ]
    result = audit.analyze(
        readings,
        low_limit=2.0,
        high_limit=8.0,
        hysteresis=0.5,
        max_interval_seconds=900,
    )

    types = [s.type for s in result.segments]
    assert types == [HIGH_EXCURSION, MISSING, HIGH_EXCURSION]

    missing = result.segments[1]
    assert missing.sample_count == 0
    assert missing.start_time == BASE
    assert missing.end_time == BASE + timedelta(seconds=1800)
    assert missing.duration_seconds == 1800
    assert missing.missing is True
    assert missing.compliant is False
    # 缺测两侧事件互不合并。
    assert result.high_excursion_count == 2
    assert result.missing_segment_count == 1
    assert result.compliant is False


def test_gap_equal_to_interval_is_not_missing() -> None:
    readings = [r(0, 5.0), r(900, 5.0)]
    result = audit.analyze(
        readings,
        low_limit=2.0,
        high_limit=8.0,
        hysteresis=0.5,
        max_interval_seconds=900,
    )
    assert len(result.segments) == 1
    assert result.segments[0].type == NORMAL


def test_low_excursion_peak_is_minimum_temperature() -> None:
    readings = [r(0, 1.0), r(60, 0.2), r(120, 1.5)]
    result = audit.analyze(
        readings,
        low_limit=2.0,
        high_limit=8.0,
        hysteresis=0.5,
        max_interval_seconds=900,
    )
    assert [s.type for s in result.segments] == [LOW_EXCURSION]
    segment = result.segments[0]
    assert segment.peak_temperature == 0.2
    assert segment.peak_at == BASE + timedelta(seconds=60)
    assert segment.min_temperature == 0.2
    assert segment.max_temperature == 1.5
    assert result.low_excursion_count == 1


def test_unsorted_readings_are_sorted_by_time() -> None:
    readings = [
        r(120, 3.0, index=2),
        r(0, 9.0, index=0),
        r(60, 9.5, index=1),
    ]
    result = audit.analyze(
        readings,
        low_limit=2.0,
        high_limit=8.0,
        hysteresis=0.5,
        max_interval_seconds=900,
    )
    assert result.segments[0].type == HIGH_EXCURSION
    assert result.segments[0].start_time == BASE
    assert result.segments[0].peak_temperature == 9.5


def test_single_reading_segment() -> None:
    result = audit.analyze(
        [r(0, 10.0)],
        low_limit=2.0,
        high_limit=8.0,
        hysteresis=0.5,
        max_interval_seconds=900,
    )
    assert len(result.segments) == 1
    assert result.segments[0].type == HIGH_EXCURSION
    assert result.segments[0].duration_seconds == 0.0


def test_timestamps_naive_are_accepted_by_engine() -> None:
    naive = datetime(2026, 9, 22, 8, 0, 0)
    result = audit.analyze(
        [Reading(naive, 9.0, 0)],
        low_limit=2.0,
        high_limit=8.0,
        hysteresis=0.5,
        max_interval_seconds=900,
    )
    assert result.segments[0].type == HIGH_EXCURSION


def test_gap_between_normal_runs_resets_state() -> None:
    # 第一段处于高温解除前；缺测后即使读数仍在解除线下也独立判定。
    readings = [
        r(0, 9.0),
        r(60, 7.5),  # 解除高温 → NORMAL
        r(2000, 5.0),  # 大缺测
        r(2060, 5.0),
    ]
    result = audit.analyze(
        readings,
        low_limit=2.0,
        high_limit=8.0,
        hysteresis=0.5,
        max_interval_seconds=900,
    )
    types = [s.type for s in result.segments]
    assert types == [HIGH_EXCURSION, NORMAL, MISSING, NORMAL]
    assert result.compliant is False
