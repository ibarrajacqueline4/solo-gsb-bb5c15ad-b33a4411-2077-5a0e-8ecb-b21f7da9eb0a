"""运输交接连续性检查核心引擎（纯函数，不依赖 Web/DB 框架）。

一批货物在联运过程中会先后形成多个冷链批次（不同承运商 / 车厢 / 设备），
交接点要求上一批次结束与下一批次开始之间的时间间隔受控，且交接边界不能
落在缺测（missing）片段上，否则该段运输状态无法证明连续。

引擎输入为按**运输顺序**排列的批次时间轴摘要（批次起止时间与首 / 末片段
类型，均来自已审计并持久化的报告），逐对相邻批次计算：

- ``gap_seconds = downstream.start_time - upstream.end_time``：
  正数为交接缺口、0 为首尾相接、负数表示两段时间轴互相重叠；
- ``relation``：``gap`` / ``contiguous`` / ``overlap``；
- 两侧片段类型：上游批次的**末段**（upstream_tail_type）与下游批次的
  **首段**（downstream_head_type）。

任一相邻对满足以下任一条件即标记 ``handoff_issue``：

1. 存在缺口且**严格大于**允许交接间隔 ``max_handoff_seconds``
   （等于上限视为可接受，与采样缺测切片“严格超过才切分”的口径一致）；
2. 上游末段或下游首段为 ``missing`` —— 交接点紧邻缺测，
   连续性无法证明。

重叠只如实报告（``relation=overlap`` 且 gap_seconds 为负），按上述规则
不单独判为 issue；整条路线 ``route_compliant`` 当且仅当所有相邻对均无
交接问题。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .audit import MISSING

# 相邻批次时间轴关系。
CONTIGUOUS = "contiguous"
GAP = "gap"
OVERLAP = "overlap"

# handoff_issue 的具体原因（issue_reasons 取值）。
ISSUE_GAP_EXCEEDED = "gap_exceeds_threshold"
ISSUE_UPSTREAM_TAIL_MISSING = "upstream_tail_missing"
ISSUE_DOWNSTREAM_HEAD_MISSING = "downstream_head_missing"


@dataclass(frozen=True)
class BatchTimeline:
    """单个批次在交接检查中所需的已审计时间轴摘要。"""

    ref: str
    start_time: datetime
    end_time: datetime
    first_segment_type: str
    last_segment_type: str


@dataclass(frozen=True)
class Handoff:
    """一对相邻批次之间的交接检查结果。"""

    index: int
    upstream_ref: str
    downstream_ref: str
    upstream_end_time: datetime
    downstream_start_time: datetime
    gap_seconds: float
    relation: str
    upstream_tail_type: str
    downstream_head_type: str
    issue_reasons: tuple[str, ...]

    @property
    def handoff_issue(self) -> bool:
        return bool(self.issue_reasons)


@dataclass(frozen=True)
class HandoffReport:
    """整条运输路线的交接连续性检查结果。"""

    max_handoff_seconds: float
    handoffs: tuple[Handoff, ...]

    @property
    def route_compliant(self) -> bool:
        # 单批次路线无相邻对，调用方已在参数层禁止；此处天然判为合规。
        return all(not handoff.handoff_issue for handoff in self.handoffs)


def check_handoffs(
    timelines: list[BatchTimeline],
    *,
    max_handoff_seconds: float,
) -> HandoffReport:
    """按运输顺序逐对相邻批次执行交接连续性检查。

    参数由接口层保证：timelines 按运输顺序排列、每项非空、
    max_handoff_seconds > 0。
    """

    handoffs: list[Handoff] = []
    for index, (upstream, downstream) in enumerate(
        zip(timelines, timelines[1:]), start=1
    ):
        gap_seconds = round(
            (downstream.start_time - upstream.end_time).total_seconds(), 3
        )
        if gap_seconds > 0:
            relation = GAP
        elif gap_seconds < 0:
            relation = OVERLAP
        else:
            relation = CONTIGUOUS

        issue_reasons: list[str] = []
        if relation == GAP and gap_seconds > max_handoff_seconds:
            issue_reasons.append(ISSUE_GAP_EXCEEDED)
        if upstream.last_segment_type == MISSING:
            issue_reasons.append(ISSUE_UPSTREAM_TAIL_MISSING)
        if downstream.first_segment_type == MISSING:
            issue_reasons.append(ISSUE_DOWNSTREAM_HEAD_MISSING)

        handoffs.append(
            Handoff(
                index=index,
                upstream_ref=upstream.ref,
                downstream_ref=downstream.ref,
                upstream_end_time=upstream.end_time,
                downstream_start_time=downstream.start_time,
                gap_seconds=gap_seconds,
                relation=relation,
                upstream_tail_type=upstream.last_segment_type,
                downstream_head_type=downstream.first_segment_type,
                issue_reasons=tuple(issue_reasons),
            )
        )

    return HandoffReport(
        max_handoff_seconds=max_handoff_seconds,
        handoffs=tuple(handoffs),
    )
