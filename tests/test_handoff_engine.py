"""交接连续性检查引擎（app/handoffs.py）单元测试。

覆盖：
- 缺口 / 首尾相接 / 重叠三种 relation 与带符号 gap_seconds；
- 缺口严格超过阈值才判问题（等于上限合规）；
- 上游末段 / 下游首段为 missing 时各自的问题原因，可叠加；
- route_compliant 的聚合、单批次无相邻对；
- 多批次链路逐对计算且互不影响。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import handoffs
from app.audit import HIGH_EXCURSION, MISSING, NORMAL


def _t(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _timeline(
    ref: str,
    *,
    start: datetime,
    end: datetime,
    first: str = NORMAL,
    last: str = NORMAL,
) -> handoffs.BatchTimeline:
    return handoffs.BatchTimeline(
        ref=ref,
        start_time=start,
        end_time=end,
        first_segment_type=first,
        last_segment_type=last,
    )


def test_contiguous_handoff_is_compliant() -> None:
    t = _t("2026-09-22T08:00:00")
    report = handoffs.check_handoffs(
        [
            _timeline("a", start=t, end=t + timedelta(hours=1)),
            _timeline(
                "b",
                start=t + timedelta(hours=1),
                end=t + timedelta(hours=2),
            ),
        ],
        max_handoff_seconds=600,
    )
    assert report.route_compliant is True
    (item,) = report.handoffs
    assert item.relation == handoffs.CONTIGUOUS
    assert item.gap_seconds == 0
    assert item.handoff_issue is False
    assert item.issue_reasons == ()
    assert item.upstream_tail_type == NORMAL
    assert item.downstream_head_type == NORMAL


def test_gap_within_threshold_is_compliant() -> None:
    t = _t("2026-09-22T08:00:00")
    report = handoffs.check_handoffs(
        [
            _timeline("a", start=t, end=t),
            _timeline("b", start=t + timedelta(seconds=300), end=t),
        ],
        max_handoff_seconds=600,
    )
    (item,) = report.handoffs
    assert item.relation == handoffs.GAP
    assert item.gap_seconds == 300
    assert item.handoff_issue is False
    assert report.route_compliant is True


def test_gap_equal_to_threshold_is_allowed() -> None:
    t = _t("2026-09-22T08:00:00")
    report = handoffs.check_handoffs(
        [
            _timeline("a", start=t, end=t),
            _timeline("b", start=t + timedelta(seconds=600), end=t),
        ],
        max_handoff_seconds=600,
    )
    # 严格超过才算问题：等于上限可接受。
    assert report.handoffs[0].handoff_issue is False
    assert report.route_compliant is True


def test_gap_exceeding_threshold_is_issue() -> None:
    t = _t("2026-09-22T08:00:00")
    report = handoffs.check_handoffs(
        [
            _timeline("a", start=t, end=t),
            _timeline("b", start=t + timedelta(seconds=601), end=t),
        ],
        max_handoff_seconds=600,
    )
    (item,) = report.handoffs
    assert item.relation == handoffs.GAP
    assert item.handoff_issue is True
    assert item.issue_reasons == (handoffs.ISSUE_GAP_EXCEEDED,)
    assert report.route_compliant is False


def test_overlap_is_reported_but_not_flagged_by_itself() -> None:
    t = _t("2026-09-22T08:00:00")
    report = handoffs.check_handoffs(
        [
            _timeline(
                "a", start=t - timedelta(hours=1), end=t + timedelta(minutes=10)
            ),
            _timeline("b", start=t, end=t + timedelta(hours=1)),
        ],
        max_handoff_seconds=600,
    )
    (item,) = report.handoffs
    assert item.relation == handoffs.OVERLAP
    assert item.gap_seconds == -600
    assert item.handoff_issue is False
    assert report.route_compliant is True


def test_upstream_tail_missing_is_issue_even_without_gap() -> None:
    t = _t("2026-09-22T08:00:00")
    report = handoffs.check_handoffs(
        [
            _timeline(
                "a", start=t, end=t, first=NORMAL, last=MISSING
            ),
            _timeline(
                "b",
                start=t + timedelta(seconds=60),
                end=t,
                first=NORMAL,
                last=NORMAL,
            ),
        ],
        max_handoff_seconds=600,
    )
    (item,) = report.handoffs
    assert item.handoff_issue is True
    assert item.upstream_tail_type == MISSING
    assert item.issue_reasons == (handoffs.ISSUE_UPSTREAM_TAIL_MISSING,)


def test_downstream_head_missing_is_issue() -> None:
    t = _t("2026-09-22T08:00:00")
    report = handoffs.check_handoffs(
        [
            _timeline("a", start=t, end=t),
            _timeline(
                "b",
                start=t + timedelta(seconds=60),
                end=t,
                first=MISSING,
                last=NORMAL,
            ),
        ],
        max_handoff_seconds=600,
    )
    (item,) = report.handoffs
    assert item.downstream_head_type == MISSING
    assert item.issue_reasons == (handoffs.ISSUE_DOWNSTREAM_HEAD_MISSING,)


def test_gap_and_missing_reasons_accumulate() -> None:
    t = _t("2026-09-22T08:00:00")
    report = handoffs.check_handoffs(
        [
            _timeline(
                "a", start=t, end=t, first=NORMAL, last=MISSING
            ),
            _timeline(
                "b",
                start=t + timedelta(seconds=900),
                end=t,
                first=MISSING,
                last=HIGH_EXCURSION,
            ),
        ],
        max_handoff_seconds=600,
    )
    (item,) = report.handoffs
    assert item.issue_reasons == (
        handoffs.ISSUE_GAP_EXCEEDED,
        handoffs.ISSUE_UPSTREAM_TAIL_MISSING,
        handoffs.ISSUE_DOWNSTREAM_HEAD_MISSING,
    )
    assert report.route_compliant is False


def test_three_batch_chain_aggregates_per_pair() -> None:
    t = _t("2026-09-22T08:00:00")
    report = handoffs.check_handoffs(
        [
            _timeline("a", start=t, end=t),
            _timeline("b", start=t + timedelta(seconds=30), end=t),
            _timeline("c", start=t + timedelta(seconds=9999), end=t),
        ],
        max_handoff_seconds=600,
    )
    assert len(report.handoffs) == 2
    first, second = report.handoffs
    assert first.index == 1
    assert (first.upstream_ref, first.downstream_ref) == ("a", "b")
    assert first.handoff_issue is False
    assert second.index == 2
    assert (second.upstream_ref, second.downstream_ref) == ("b", "c")
    assert second.handoff_issue is True
    assert report.route_compliant is False
