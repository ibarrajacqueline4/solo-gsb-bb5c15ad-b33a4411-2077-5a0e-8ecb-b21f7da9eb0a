"""运输交接连续性检查（纯逻辑，不依赖 Web/DB 框架）。

调用方给出**按运输顺序排列**的批次时间轴，本模块逐对比较
“前一批次 end_time → 后一批次 start_time”：

- ``interval_seconds`` = 后一批 start_time − 前一批 end_time（有符号）；
- 正值为**缺口**（``gap_seconds``），负值为**重叠**（``overlap_seconds``），
  ``0`` 表示两批次首尾相接；
- 缺口**严格大于**允许交接间隔，或交接任一侧的末段/首段为 ``missing``
  片段时，该交接标记 ``handoff_issue``；
- ``route_compliant`` 当且仅当链上所有交接均无问题。

重叠本身不视为问题（同一时间窗内双方都有观测），只做报告；批次自身的
温度合规性也不在本检查范围内——本检查只回答“交接是否连续”。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

# 交接问题原因（固定顺序：先缺口超限，再侧接缺测段）。
GAP_EXCEEDS_THRESHOLD = "gap_exceeds_threshold"
MISSING_BOUNDARY_SEGMENT = "missing_boundary_segment"


@dataclass(frozen=True)
class BatchTimeline:
    """单批次在交接检查中所需的已审计时间轴边界信息。"""

    id: str
    external_ref: str
    start_time: datetime
    end_time: datetime
    first_segment_type: str
    first_segment_missing: bool
    last_segment_type: str
    last_segment_missing: bool


@dataclass(frozen=True)
class HandoffVerdict:
    """相邻两批次（from → to）的一次交接判定。"""

    index: int
    from_timeline: BatchTimeline
    to_timeline: BatchTimeline
    interval_seconds: float
    gap_seconds: float
    overlap_seconds: float
    handoff_issue: bool
    issue_reasons: tuple[str, ...]


def evaluate_handoffs(
    timelines: Sequence[BatchTimeline],
    allowed_gap_seconds: float,
) -> list[HandoffVerdict]:
    """按给定运输顺序逐对评估相邻批次的交接连续性。

    调用方保证 ``timelines`` 至少有两项且互不重复；缺口判定使用未舍入的
    原始秒差做**严格大于**比较（等于阈值不算问题），输出字段保留三位小数。
    """

    verdicts: list[HandoffVerdict] = []
    for index, (left, right) in enumerate(zip(timelines, timelines[1:])):
        interval_raw = (
            right.start_time - left.end_time
        ).total_seconds()
        interval_seconds = round(interval_raw, 3)
        if interval_raw > 0:
            gap_seconds, overlap_seconds = interval_seconds, 0.0
        elif interval_raw < 0:
            gap_seconds, overlap_seconds = 0.0, round(-interval_raw, 3)
        else:
            gap_seconds, overlap_seconds = 0.0, 0.0

        issue_reasons: list[str] = []
        if interval_raw > allowed_gap_seconds:
            issue_reasons.append(GAP_EXCEEDS_THRESHOLD)
        if left.last_segment_missing or right.first_segment_missing:
            issue_reasons.append(MISSING_BOUNDARY_SEGMENT)

        verdicts.append(
            HandoffVerdict(
                index=index,
                from_timeline=left,
                to_timeline=right,
                interval_seconds=interval_seconds,
                gap_seconds=gap_seconds,
                overlap_seconds=overlap_seconds,
                handoff_issue=bool(issue_reasons),
                issue_reasons=tuple(issue_reasons),
            )
        )
    return verdicts


def route_is_compliant(verdicts: Sequence[HandoffVerdict]) -> bool:
    """整条路线合规当且仅当没有任何交接问题。"""

    return all(not verdict.handoff_issue for verdict in verdicts)
