"""运输交接连续性检查接口（POST /handoffs/check）测试。

覆盖：
- 首尾相接 / 缺口（阈值内、等于阈值、超过阈值）/ 重叠及带符号间隔；
- 交接点两侧末/首段类型（含高/低温越限，不因此判交接问题）；
- 多批次链路逐对结果与 route_compliant 聚合；
- 引用不存在 → 404；不足两项、重复批次（含 ID 与 external_ref 指向同一
  批次）、非法间隔 → 422；
- 支持按批次 ID 定位、时区统一换算为 UTC；
- 接口只读：报告、seal/verify 与复核数据前后逐字节不变。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.schemas import HandoffCheckCreate


def _batch(ref: str, readings: list[tuple[str, float]], **overrides) -> dict:
    payload = {
        "external_ref": ref,
        "low_limit": 2.0,
        "high_limit": 8.0,
        "hysteresis": 0.5,
        "max_interval_seconds": 900,
        "readings": [
            {"timestamp": ts, "temperature": temp} for ts, temp in readings
        ],
    }
    payload.update(overrides)
    return payload


def _create(client: TestClient, payload: dict) -> dict:
    response = client.post("/batches", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _check(
    client: TestClient, refs: list[str], max_gap: float = 600.0
):
    return client.post(
        "/handoffs/check",
        json={"batch_refs": refs, "max_handoff_seconds": max_gap},
    )


def _single(client: TestClient, ref: str, ts: str, temp: float = 5.0) -> dict:
    return _create(
        client, _batch(ref, [(ts, temp)])
    )


def test_contiguous_and_gap_within_threshold_are_compliant(
    client: TestClient,
) -> None:
    _single(client, "b1", "2026-09-22T08:00:00Z")
    _single(client, "b2", "2026-09-22T08:05:00Z")

    # 首尾相接（同一时刻结束/开始）。
    _single(client, "b0", "2026-09-22T08:00:00Z")
    contiguous = _check(client, ["b0", "b1"], 600)
    assert contiguous.status_code == 200, contiguous.text
    body = contiguous.json()
    assert body["handoff_count"] == 1
    assert body["route_compliant"] is True
    item = body["handoffs"][0]
    assert item["relation"] == "contiguous"
    assert item["gap_seconds"] == 0
    assert item["overlap_seconds"] is None
    assert item["handoff_issue"] is False
    assert item["issue_reasons"] == []
    assert item["upstream_tail_type"] == "normal"
    assert item["downstream_head_type"] == "normal"

    # 300 秒缺口，阈值 600，合规。
    gapped = _check(client, ["b1", "b2"], 600)
    assert gapped.status_code == 200
    item = gapped.json()["handoffs"][0]
    assert item["relation"] == "gap"
    assert item["gap_seconds"] == 300
    assert item["handoff_issue"] is False
    assert gapped.json()["route_compliant"] is True


def test_gap_equal_to_threshold_is_allowed(client: TestClient) -> None:
    _single(client, "b1", "2026-09-22T08:00:00Z")
    _single(client, "b2", "2026-09-22T08:10:00Z")

    response = _check(client, ["b1", "b2"], 600)
    assert response.status_code == 200
    item = response.json()["handoffs"][0]
    assert item["gap_seconds"] == 600
    assert item["handoff_issue"] is False
    assert response.json()["route_compliant"] is True


def test_gap_exceeding_threshold_flags_handoff_issue(
    client: TestClient,
) -> None:
    _single(client, "b1", "2026-09-22T08:00:00Z")
    _single(client, "b2", "2026-09-22T09:00:00Z")

    response = _check(client, ["b1", "b2"], 600)
    assert response.status_code == 200
    body = response.json()
    item = body["handoffs"][0]
    assert item["relation"] == "gap"
    assert item["gap_seconds"] == 3600
    assert item["handoff_issue"] is True
    assert item["issue_reasons"] == ["gap_exceeds_threshold"]
    assert body["route_compliant"] is False
    assert item["upstream_end_time"] == "2026-09-22T08:00:00Z"
    assert item["downstream_start_time"] == "2026-09-22T09:00:00Z"


def test_overlap_is_reported_with_signed_gap(client: TestClient) -> None:
    # 上游批次正常观测跨度 07:50–08:10（放宽采样间隔上限避免内部缺测）。
    _create(
        client,
        _batch(
            "up",
            [
                ("2026-09-22T07:50:00Z", 5.0),
                ("2026-09-22T08:10:00Z", 5.0),
            ],
            max_interval_seconds=3600,
        ),
    )
    _single(client, "down", "2026-09-22T08:00:00Z")

    response = _check(client, ["up", "down"], 600)
    assert response.status_code == 200
    item = response.json()["handoffs"][0]
    assert item["relation"] == "overlap"
    assert item["gap_seconds"] == -600
    assert item["overlap_seconds"] == 600
    # 重叠只如实报告，不单独判交接问题。
    assert item["handoff_issue"] is False
    assert response.json()["route_compliant"] is True


def test_side_segment_types_are_reported(client: TestClient) -> None:
    # 上游：normal → high_excursion，末段为高温越限。
    _create(
        client,
        _batch(
            "up",
            [
                ("2026-09-22T08:00:00Z", 5.0),
                ("2026-09-22T08:05:00Z", 9.5),
            ],
        ),
    )
    # 下游：全程高温，首段即 high_excursion。
    _create(
        client,
        _batch(
            "down",
            [
                ("2026-09-22T08:10:00Z", 9.5),
                ("2026-09-22T08:15:00Z", 9.2),
            ],
        ),
    )

    response = _check(client, ["up", "down"], 600)
    item = response.json()["handoffs"][0]
    assert item["upstream_tail_type"] == "high_excursion"
    assert item["downstream_head_type"] == "high_excursion"
    # 越限不属于交接连续性问题（300 秒缺口也在阈值内）。
    assert item["handoff_issue"] is False
    assert response.json()["route_compliant"] is True


def test_three_batch_chain_aggregates_per_pair(client: TestClient) -> None:
    _single(client, "b1", "2026-09-22T08:00:00Z")
    _single(client, "b2", "2026-09-22T08:05:00Z")
    _single(client, "b3", "2026-09-22T09:30:00Z")

    response = _check(client, ["b1", "b2", "b3"], 600)
    assert response.status_code == 200
    body = response.json()
    assert body["handoff_count"] == 2
    first, second = body["handoffs"]
    assert (first["index"], first["upstream_ref"], first["downstream_ref"]) == (
        1,
        "b1",
        "b2",
    )
    assert first["gap_seconds"] == 300
    assert first["handoff_issue"] is False
    assert (
        second["index"],
        second["upstream_ref"],
        second["downstream_ref"],
    ) == (2, "b2", "b3")
    assert second["gap_seconds"] == 5100
    assert second["handoff_issue"] is True
    assert body["route_compliant"] is False
    assert body["batch_refs"] == ["b1", "b2", "b3"]
    assert body["max_handoff_seconds"] == 600


def test_unknown_batch_returns_404(client: TestClient) -> None:
    _single(client, "b1", "2026-09-22T08:00:00Z")

    missing_second = _check(client, ["b1", "ghost"], 600)
    assert missing_second.status_code == 404
    assert "ghost" in missing_second.json()["detail"]

    missing_first = _check(client, ["ghost", "b1"], 600)
    assert missing_first.status_code == 404


def test_fewer_than_two_refs_returns_422(client: TestClient) -> None:
    empty = client.post(
        "/handoffs/check",
        json={"batch_refs": [], "max_handoff_seconds": 600},
    )
    assert empty.status_code == 422

    single = client.post(
        "/handoffs/check",
        json={"batch_refs": ["b1"], "max_handoff_seconds": 600},
    )
    assert single.status_code == 422


def test_duplicate_refs_return_422(client: TestClient) -> None:
    report = _single(client, "b1", "2026-09-22T08:00:00Z")
    _single(client, "b2", "2026-09-22T08:05:00Z")

    repeated = _check(client, ["b1", "b1", "b2"], 600)
    assert repeated.status_code == 422

    # ID 与 external_ref 混用指向同一批次同样拒绝。
    aliased = _check(client, [report["id"], "b1", "b2"], 600)
    assert aliased.status_code == 422


def test_invalid_interval_returns_422(client: TestClient) -> None:
    _single(client, "b1", "2026-09-22T08:00:00Z")
    _single(client, "b2", "2026-09-22T08:05:00Z")

    assert _check(client, ["b1", "b2"], 0).status_code == 422
    assert _check(client, ["b1", "b2"], -10).status_code == 422


def test_non_finite_interval_rejected_by_model() -> None:
    with pytest.raises(ValidationError):
        HandoffCheckCreate(
            batch_refs=["b1", "b2"], max_handoff_seconds=float("nan")
        )


def test_check_by_batch_id(client: TestClient) -> None:
    r1 = _single(client, "b1", "2026-09-22T08:00:00Z")
    r2 = _single(client, "b2", "2026-09-22T08:05:00Z")

    response = _check(client, [r1["id"], r2["id"]], 600)
    assert response.status_code == 200
    item = response.json()["handoffs"][0]
    # 响应中的引用回显为请求原值（批次 ID）。
    assert item["upstream_ref"] == r1["id"]
    assert item["downstream_ref"] == r2["id"]


def test_timestamps_normalized_to_utc(client: TestClient) -> None:
    _single(client, "b1", "2026-09-22T16:00:00+08:00")
    _single(client, "b2", "2026-09-22T16:05:00+08:00")

    response = _check(client, ["b1", "b2"], 600)
    assert response.status_code == 200
    item = response.json()["handoffs"][0]
    assert item["upstream_end_time"] == "2026-09-22T08:00:00Z"
    assert item["downstream_start_time"] == "2026-09-22T08:05:00Z"
    assert item["gap_seconds"] == 300


def test_handoff_check_is_read_only(client: TestClient) -> None:
    _single(client, "b1", "2026-09-22T08:00:00Z")
    _single(client, "b2", "2026-09-22T09:00:00Z")

    # 先封存 b1 并完成一次复核通过。
    seal_before = client.post("/batches/b1/seal")
    assert seal_before.status_code == 200
    seal_before = seal_before.json()
    review = client.post(
        "/batches/b1/review",
        json={
            "reviewer": "alice",
            "decision": "approve",
            "request_id": "handoff-readonly-1",
        },
    )
    assert review.status_code == 201

    report_before = client.get("/batches/b1/report").json()
    verify_before = client.get("/batches/b1/verify").json()
    review_before = client.get("/batches/b1/review").json()
    assert verify_before["valid"] is True

    response = _check(client, ["b1", "b2"], 600)
    assert response.status_code == 200
    assert response.json()["route_compliant"] is False

    # 报告 / 封存 / 校验 / 复核数据均不被改动。
    assert client.get("/batches/b1/report").json() == report_before
    assert client.post("/batches/b1/seal").json() == seal_before
    verify_after = client.get("/batches/b1/verify").json()
    assert verify_after == verify_before
    assert verify_after["valid"] is True
    assert client.get("/batches/b1/review").json() == review_before
