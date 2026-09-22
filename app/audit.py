"""冷链温度合规审计核心引擎。

设计要点
========

1. 超温识别采用三态状态机（正常 / 高温越限 / 低温越限），并使用
   *滞回阈值（hysteresis）* 消除临界点附近的抖动：
   - 高温进入条件：temperature > high_limit
   - 高温解除条件：temperature <= high_limit - hysteresis
   - 低温进入条件：temperature < low_limit
   - 低温解除条件：temperature >= low_limit + hysteresis
   读数恰好落在允许边界上视为正常。

2. 相邻两次读数的时间差若严格大于 max_interval_seconds，则在两次读数之间
   切出一段独立的缺测片段（missing segment，起止为前后两个读数的时间戳）。
   缺测后状态机复位为 NORMAL，缺测两侧的越限事件互不合并。

3. 时间轴被完整切分为若干相邻片段：normal / high_excursion /
   low_excursion / missing。缺测片段与越限片段均判定为不合规，
   批次整体合规当且仅当所有片段均合规。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

# 片段类型常量
NORMAL = "normal"
HIGH_EXCURSION = "high_excursion"
LOW_EXCURSION = "low_excursion"
MISSING = "missing"


@dataclass(frozen=True)
class Reading:
    """单条温度读数（source_index 保留提交顺序，用于稳定排序）。"""

    timestamp: datetime
    temperature: float
    source_index: int


@dataclass
class Segment:
    """时间轴上的一个连续片段。"""

    type: str
    start_time: datetime
    end_time: datetime
    sample_count: int
    peak_temperature: float | None = None
    peak_at: datetime | None = None
    max_temperature: float | None = None
    min_temperature: float | None = None

    @property
    def missing(self) -> bool:
        return self.type == MISSING

    @property
    def compliant(self) -> bool:
        # 缺测无法证明合规，按不合规处理；越限显然不合规。
        return self.type == NORMAL

    @property
    def duration_seconds(self) -> float:
        return round((self.end_time - self.start_time).total_seconds(), 3)


@dataclass
class AuditResult:
    """整批次审计结果。"""

    segments: list[Segment]
    high_excursion_count: int
    low_excursion_count: int
    missing_segment_count: int

    @property
    def compliant(self) -> bool:
        return all(segment.compliant for segment in self.segments)


def _next_state(
    state: str,
    temperature: float,
    low_limit: float,
    high_limit: float,
    high_release: float,
    low_release: float,
) -> str:
    """滞回状态机：根据当前状态与读数计算新状态。"""

    # 先判断是否满足“解除越限”，解除后同一读数可立即反向进入另一越限态。
    if state == HIGH_EXCURSION and temperature <= high_release:
        state = NORMAL
    elif state == LOW_EXCURSION and temperature >= low_release:
        state = NORMAL

    if state == NORMAL:
        if temperature > high_limit:
            return HIGH_EXCURSION
        if temperature < low_limit:
            return LOW_EXCURSION
    return state


def _build_observed_segment(
    segment_type: str, group: Sequence[Reading]
) -> Segment:
    """根据一组连续读数构造有观测片段。"""

    temperatures = [reading.temperature for reading in group]
    max_temperature = max(temperatures)
    min_temperature = min(temperatures)

    peak_temperature: float | None = None
    peak_at: datetime | None = None
    if segment_type == HIGH_EXCURSION:
        # 峰值 = 最高温度；相同时取最早出现的位置。
        peak_temperature = max_temperature
        peak_at = next(
            reading.timestamp
            for reading in group
            if reading.temperature == peak_temperature
        )
    elif segment_type == LOW_EXCURSION:
        # 低温事件的“峰值”即最低温度（最严重偏离点）。
        peak_temperature = min_temperature
        peak_at = next(
            reading.timestamp
            for reading in group
            if reading.temperature == peak_temperature
        )

    return Segment(
        type=segment_type,
        start_time=group[0].timestamp,
        end_time=group[-1].timestamp,
        sample_count=len(group),
        peak_temperature=peak_temperature,
        peak_at=peak_at,
        max_temperature=max_temperature,
        min_temperature=min_temperature,
    )


def analyze(
    readings: Iterable[Reading],
    *,
    low_limit: float,
    high_limit: float,
    hysteresis: float,
    max_interval_seconds: float,
) -> AuditResult:
    """对一批读数执行合规审计。

    参数由接口层保证：readings 非空、low_limit < high_limit、
    hysteresis >= 0、max_interval_seconds > 0。
    """

    # 按时间排序；相同时间戳按提交顺序稳定排列，不去重。
    ordered = sorted(readings, key=lambda r: (r.timestamp, r.source_index))

    high_release = high_limit - hysteresis
    low_release = low_limit + hysteresis

    raw_segments: list[Segment] = []

    state = NORMAL
    group: list[Reading] = []
    previous_timestamp: datetime | None = None

    def flush_group() -> None:
        if group:
            raw_segments.append(_build_observed_segment(state, group))

    for reading in ordered:
        if previous_timestamp is not None:
            gap_seconds = (
                reading.timestamp - previous_timestamp
            ).total_seconds()
            gap_detected = gap_seconds > max_interval_seconds
        else:
            gap_detected = False

        if gap_detected:
            # 缺测切断当前片段，插入以两侧读数时间戳为界的缺测片段，
            # 并将状态机复位——缺测之后是一段独立的观测。
            flush_group()
            group = []
            raw_segments.append(
                Segment(
                    type=MISSING,
                    start_time=previous_timestamp,  # type: ignore[arg-type]
                    end_time=reading.timestamp,
                    sample_count=0,
                )
            )
            state = NORMAL

        new_state = _next_state(
            state,
            reading.temperature,
            low_limit,
            high_limit,
            high_release,
            low_release,
        )
        if new_state != state and group:
            # 越限状态切换（进入/解除）也是片段边界。
            flush_group()
            group = []

        state = new_state
        group.append(reading)
        previous_timestamp = reading.timestamp

    flush_group()

    # 按发射顺序统一编号。
    segments = [
        Segment(
            type=segment.type,
            start_time=segment.start_time,
            end_time=segment.end_time,
            sample_count=segment.sample_count,
            peak_temperature=segment.peak_temperature,
            peak_at=segment.peak_at,
            max_temperature=segment.max_temperature,
            min_temperature=segment.min_temperature,
        )
        for segment in raw_segments
    ]

    return AuditResult(
        segments=segments,
        high_excursion_count=sum(
            1 for s in segments if s.type == HIGH_EXCURSION
        ),
        low_excursion_count=sum(
            1 for s in segments if s.type == LOW_EXCURSION
        ),
        missing_segment_count=sum(1 for s in segments if s.type == MISSING),
    )
