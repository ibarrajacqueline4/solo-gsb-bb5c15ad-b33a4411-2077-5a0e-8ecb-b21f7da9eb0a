"""运输交接连续性检查测试。

覆盖（API 层）：
- 连续路线：间隔/缺口/重叠/零间隔的秒数计算与两侧末/首段类型报告；
- 缺口严格大于阈值才标记问题（等于阈值不算），route_compliant / issue_count；
- 两侧相接片段为 missing 时标记 missing_boundary_segment（写时审计引擎不会在
  时间轴边缘产生 missing 片段，该路径通过直接构造持久化时间轴验证）；
- 时区分偏移归一化为 UTC（Z）；
- 引用不存在返回 404；不足两项 / 字符串重复 / ID 与 external_ref 指向同一
  批次返回 422；
- 检查全程只读：审计报告、seal/verify 与复核数据均不变；
- 省略 allowed_gap_seconds 时使用服务默认值。

另在本文件末尾覆盖纯逻辑（app.handoffs）：两侧同时 missing、原因排序、
三批次链与合规判定。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import handoffs
from tests.test_api import _payload


def _normal_payload(ref: str, times: list[str], temps: list[float] | None = None):
    if temps is None:
        temps = [5.0] * len(times)
    return _payload(
        external_ref=ref,
        readings=[
            {"timestamp": ts, "temperature": t} for ts, t in zip(times, temps)
        ],
    )


def _create(client: TestClient, payload: dict) -> str:
    response = client.post("/batches", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _check(client: TestClient, refs, allowed_gap_seconds=None):
    body: dict = {"batch_refs": refs}
    if allowed_gap_seconds is not None:
        body["allowed_gap_seconds"] = allowed_gap_seconds
    return client.post("/handoffs/check", json=body)


def _three_leg_chain(client: TestClient) -> None:
    """leg-1 08:00–08:20 normal；leg-2 08:25–08:45 高温后回正；leg-3 09:45 起。

    leg-1 → leg-2 缺口 300s；leg-2 首段 high_excursion、末段 normal；
    leg-2 → leg-3 缺口 3600s。
    """

    _create(
        client,
        _normal_payload(
            "leg-1",
            ["2026-09-22T08:00:00Z", "2026-09-22T08:10:00Z",
             "2026-09-22T08:20:00Z"],
        ),
    )
    _create(
        client,
        _normal_payload(
            "leg-2",
            ["2026-09-22T08:25:00Z", "2026-09-22T08:30:00Z",
             "2026-09-22T08:45:00Z"],
            temps=[9.2, 9.4, 5.0],
        ),
    )
    _create(
        client,
        _normal_payload(
            "leg-3",
            ["2026-09-22T09:45:00Z", "2026-09-22T09:55:00Z"],
        ),
    )


def test_continuous_chain_reports_intervals_and_boundary_segments(
    client: TestClient,
) -> None:
    _three_leg_chain(client)
    response = _check(client, ["leg-1", "leg-2", "leg-3"], 600)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["batch_refs"] == ["leg-1", "leg-2", "leg-3"]
    assert body["allowed_gap_seconds"] == 600.0
    assert body["handoff_count"] == 2
    assert body["issue_count"] == 1
    assert body["route_compliant"] is False

    first, second = body["handoffs"]
    assert first["index"] == 0
    assert first["from"]["external_ref"] == "leg-1"
    assert first["to"]["external_ref"] == "leg-2"
    assert first["from_end_time"] == "2026-09-22T08:20:00Z"
    assert first["to_start_time"] == "2026-09-22T08:25:00Z"
    assert first["interval_seconds"] == 300.0
    assert first["gap_seconds"] == 300.0
    assert first["overlap_seconds"] == 0.0
    assert first["handoff_issue"] is False
    assert first["issue_reasons"] == []

    # 两侧末/首段类型：leg-1 末段 normal；leg-2 首段 high_excursion。
    assert first["from"]["segment_type"] == "normal"
    assert first["from"]["segment_missing"] is False
    assert first["from"]["boundary_time"] == "2026-09-22T08:20:00Z"
    assert first["to"]["segment_type"] == "high_excursion"
    assert first["to"]["segment_missing"] is False
    assert first["to"]["boundary_time"] == "2026-09-22T08:25:00Z"

    # leg-2 末段为回落后的 normal。
    assert second["index"] == 1
    assert second["from"]["segment_type"] == "normal"
    assert second["to"]["segment_type"] == "normal"
    assert second["interval_seconds"] == 3600.0
    assert second["gap_seconds"] == 3600.0
    assert second["overlap_seconds"] == 0.0
    assert second["handoff_issue"] is True
    assert second["issue_reasons"] == ["gap_exceeds_threshold"]
    assert body["route_compliant"] is False


def test_route_compliant_when_all_gaps_within_threshold(
    client: TestClient,
) -> None:
    _create(
        client,
        _normal_payload("a", ["2026-09-22T08:00:00Z", "2026-09-22T08:20:00Z"]),
    )
    _create(
        client,
        _normal_payload("b", ["2026-09-22T08:25:00Z", "2026-09-22T08:40:00Z"]),
    )
    response = _check(client, ["a", "b"], 300)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["route_compliant"] is True
    assert body["issue_count"] == 0
    assert body["handoffs"][0]["handoff_issue"] is False


def test_gap_equal_to_threshold_is_not_an_issue(client: TestClient) -> None:
    _create(
        client,
        _normal_payload("a", ["2026-09-22T08:00:00Z", "2026-09-22T08:20:00Z"]),
    )
    _create(
        client,
        _normal_payload("b", ["2026-09-22T08:30:00Z", "2026-09-22T08:40:00Z"]),
    )
    # 缺口恰好 600s，阈值也是 600s：严格大于才算问题。
    response = _check(client, ["a", "b"], 600)
    assert response.status_code == 200
    assert response.json()["route_compliant"] is True

    # 阈值收窄 1 秒即超限。
    tighter = _check(client, ["a", "b"], 599)
    assert tighter.json()["route_compliant"] is False
    assert tighter.json()["handoffs"][0]["issue_reasons"] == [
        "gap_exceeds_threshold"
    ]


def test_zero_interval_is_contiguous(client: TestClient) -> None:
    _create(
        client,
        _normal_payload("a", ["2026-09-22T08:00:00Z", "2026-09-22T08:20:00Z"]),
    )
    _create(
        client,
        _normal_payload("b", ["2026-09-22T08:20:00Z", "2026-09-22T08:40:00Z"]),
    )
    response = _check(client, ["a", "b"], 60)
    assert response.status_code == 200
    handoff = response.json()["handoffs"][0]
    assert handoff["interval_seconds"] == 0.0
    assert handoff["gap_seconds"] == 0.0
    assert handoff["overlap_seconds"] == 0.0
    assert handoff["handoff_issue"] is False


def test_overlap_is_reported_but_not_an_issue(client: TestClient) -> None:
    _create(
        client,
        _normal_payload("a", ["2026-09-22T08:00:00Z", "2026-09-22T08:20:00Z"]),
    )
    _create(
        client,
        _normal_payload("b", ["2026-09-22T08:15:00Z", "2026-09-22T08:40:00Z"]),
    )
    response = _check(client, ["a", "b"], 60)
    assert response.status_code == 200
    body = response.json()
    handoff = body["handoffs"][0]
    assert handoff["interval_seconds"] == -300.0
    assert handoff["gap_seconds"] == 0.0
    assert handoff["overlap_seconds"] == 300.0
    assert handoff["handoff_issue"] is False
    assert body["route_compliant"] is True


def test_interval_seconds_rounded_to_milliseconds(client: TestClient) -> None:
    _create(
        client,
        _normal_payload("a", [
            "2026-09-22T08:00:00.000Z",
            "2026-09-22T08:20:00.000Z",
        ]),
    )
    _create(
        client,
        _normal_payload("b", [
            "2026-09-22T08:20:00.500Z",
            "2026-09-22T08:40:00.000Z",
        ]),
    )
    response = _check(client, ["a", "b"], 60)
    handoff = response.json()["handoffs"][0]
    assert handoff["interval_seconds"] == 0.5
    assert handoff["gap_seconds"] == 0.5
    assert handoff["overlap_seconds"] == 0.0


def test_offset_timestamps_normalized_to_utc_z(client: TestClient) -> None:
    # 16:20+08:00 == 08:20Z；16:30+08:00 == 08:30Z，缺口 600s。
    _create(
        client,
        _normal_payload("a", [
            "2026-09-22T16:00:00+08:00",
            "2026-09-22T16:20:00+08:00",
        ]),
    )
    _create(
        client,
        _normal_payload("b", [
            "2026-09-22T16:30:00+08:00",
            "2026-09-22T16:40:00+08:00",
        ]),
    )
    response = _check(client, ["a", "b"], 600)
    assert response.status_code == 200
    handoff = response.json()["handoffs"][0]
    assert handoff["from_end_time"] == "2026-09-22T08:20:00Z"
    assert handoff["to_start_time"] == "2026-09-22T08:30:00Z"
    assert handoff["from"]["boundary_time"].endswith("Z")
    assert handoff["to"]["boundary_time"].endswith("Z")


def test_missing_last_segment_on_from_side_flags_issue(
    client: TestClient,
) -> None:
    _create(
        client,
        _normal_payload("a", ["2026-09-22T08:00:00Z", "2026-09-22T08:20:00Z"]),
    )
    _create(
        client,
        _normal_payload("b", ["2026-09-22T08:25:00Z", "2026-09-22T08:40:00Z"]),
    )

    # 直接把 a 的末段构造为缺测段（引擎正常不会在边缘产出，这里验证读路径）。
    _set_boundary_segment_missing("a", side="last")

    response = _check(client, ["a", "b"], 600)
    assert response.status_code == 200
    handoff = response.json()["handoffs"][0]
    assert handoff["gap_seconds"] == 300.0  # 缺口未超阈值
    assert handoff["from"]["segment_type"] == "missing"
    assert handoff["from"]["segment_missing"] is True
    assert handoff["to"]["segment_missing"] is False
    assert handoff["handoff_issue"] is True
    assert handoff["issue_reasons"] == ["missing_boundary_segment"]
    assert response.json()["route_compliant"] is False


def test_missing_first_segment_on_to_side_flags_issue(
    client: TestClient,
) -> None:
    _create(
        client,
        _normal_payload("a", ["2026-09-22T08:00:00Z", "2026-09-22T08:20:00Z"]),
    )
    _create(
        client,
        _normal_payload("b", [
            "2026-09-22T08:25:00Z",
            "2026-09-22T08:30:00Z",
            "2026-09-22T08:40:00Z",
        ]),
    )
    _set_boundary_segment_missing("b", side="first")

    response = _check(client, ["a", "b"], 600)
    handoff = response.json()["handoffs"][0]
    assert handoff["to"]["segment_type"] == "missing"
    assert handoff["to"]["segment_missing"] is True
    assert handoff["from"]["segment_missing"] is False
    assert handoff["handoff_issue"] is True
    assert handoff["issue_reasons"] == ["missing_boundary_segment"]


def _set_boundary_segment_missing(ref: str, *, side: str) -> None:
    """绕过 API 将指定批次时间轴的首段或末段改成 missing 片段。"""

    import app.config as config

    connection = sqlite3.connect(config.DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        batch_id = connection.execute(
            "SELECT id FROM batches WHERE external_ref = ?", (ref,)
        ).fetchone()["id"]
        order = "idx ASC" if side == "first" else "idx DESC"
        idx = connection.execute(
            f"SELECT idx FROM segments WHERE batch_id = ? ORDER BY {order} LIMIT 1",
            (batch_id,),
        ).fetchone()["idx"]
        connection.execute(
            "UPDATE segments SET type = 'missing', missing = 1, compliant = 0 "
            "WHERE batch_id = ? AND idx = ?",
            (batch_id, idx),
        )
        connection.commit()
    finally:
        connection.close()


def test_check_accepts_batch_ids_and_resolves_aliases(client: TestClient) -> None:
    id_a = _create(
        client,
        _normal_payload("a", ["2026-09-22T08:00:00Z", "2026-09-22T08:20:00Z"]),
    )
    _create(
        client,
        _normal_payload("b", ["2026-09-22T08:25:00Z", "2026-09-22T08:40:00Z"]),
    )
    # ID 与 external_ref 混用。
    response = _check(client, [id_a, "b"], 600)
    assert response.status_code == 200
    handoff = response.json()["handoffs"][0]
    assert handoff["from"]["batch_id"] == id_a
    assert handoff["to"]["external_ref"] == "b"


def test_unknown_batch_ref_returns_404(client: TestClient) -> None:
    _create(
        client,
        _normal_payload("a", ["2026-09-22T08:00:00Z", "2026-09-22T08:20:00Z"]),
    )
    first_missing = _check(client, ["ghost", "a"], 600)
    assert first_missing.status_code == 404
    assert "ghost" in first_missing.json()["detail"]

    second_missing = _check(client, ["a", "ghost"], 600)
    assert second_missing.status_code == 404
    assert "ghost" in second_missing.json()["detail"]


def test_fewer_than_two_refs_returns_422(client: TestClient) -> None:
    assert _check(client, [], 600).status_code == 422
    assert _check(client, ["only-one"], 600).status_code == 422


def test_duplicate_refs_return_422(client: TestClient) -> None:
    _create(
        client,
        _normal_payload("a", ["2026-09-22T08:00:00Z", "2026-09-22T08:20:00Z"]),
    )
    _create(
        client,
        _normal_payload("b", ["2026-09-22T08:25:00Z", "2026-09-22T08:40:00Z"]),
    )
    response = _check(client, ["a", "b", "a"], 600)
    assert response.status_code == 422


def test_id_and_external_ref_of_same_batch_return_422(
    client: TestClient,
) -> None:
    id_a = _create(
        client,
        _normal_payload("a", ["2026-09-22T08:00:00Z", "2026-09-22T08:20:00Z"]),
    )
    response = _check(client, [id_a, "a"], 600)
    assert response.status_code == 422


def test_invalid_allowed_gap_returns_422(client: TestClient) -> None:
    _create(
        client,
        _normal_payload("a", ["2026-09-22T08:00:00Z", "2026-09-22T08:20:00Z"]),
    )
    _create(
        client,
        _normal_payload("b", ["2026-09-22T08:25:00Z", "2026-09-22T08:40:00Z"]),
    )
    assert _check(client, ["a", "b"], 0).status_code == 422
    assert _check(client, ["a", "b"], -1).status_code == 422


def test_default_allowed_gap_when_omitted(client: TestClient) -> None:
    _create(
        client,
        _normal_payload("a", ["2026-09-22T08:00:00Z", "2026-09-22T08:20:00Z"]),
    )
    _create(
        client,
        _normal_payload("b", ["2026-09-22T08:30:00Z", "2026-09-22T08:40:00Z"]),
    )
    # 省略 allowed_gap_seconds：默认 900，缺口 600 不超限。
    response = client.post(
        "/handoffs/check", json={"batch_refs": ["a", "b"]}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allowed_gap_seconds"] == 900.0
    assert body["route_compliant"] is True

    _create(
        client,
        _normal_payload("c", ["2026-09-22T09:00:00Z", "2026-09-22T09:10:00Z"]),
    )
    # b 08:40 → c 09:00 缺口 1200s，超过默认 900。
    over_default = client.post(
        "/handoffs/check", json={"batch_refs": ["b", "c"]}
    )
    assert over_default.json()["route_compliant"] is False


def test_check_is_read_only(client: TestClient) -> None:
    _three_leg_chain(client)
    seal = client.post("/batches/leg-1/seal")
    assert seal.status_code == 200
    seal_before = seal.json()
    report_before = client.get("/batches/leg-2/report").json()
    verify_before = client.get("/batches/leg-1/verify").json()
    review_before = client.get("/batches/leg-3/review").json()

    for _ in range(2):
        response = _check(client, ["leg-1", "leg-2", "leg-3"], 600)
        assert response.status_code == 200

    assert client.get("/batches/leg-2/report").json() == report_before
    assert client.post("/batches/leg-1/seal").json() == seal_before
    verify_after = client.get("/batches/leg-1/verify").json()
    assert verify_after == verify_before
    assert verify_after["valid"] is True
    assert client.get("/batches/leg-3/review").json() == review_before

    # 批次总数与列表也不受影响。
    listing = client.get("/batches")
    assert listing.json()["total"] == 3


# ---- 纯逻辑单元测试 ----


BASE = datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc)


def _timeline(
    ref: str,
    start: datetime,
    end: datetime,
    *,
    first_type: str = "normal",
    first_missing: bool = False,
    last_type: str = "normal",
    last_missing: bool = False,
) -> handoffs.BatchTimeline:
    return handoffs.BatchTimeline(
        id=f"id-{ref}",
        external_ref=ref,
        start_time=start,
        end_time=end,
        first_segment_type=first_type,
        first_segment_missing=first_missing,
        last_segment_type=last_type,
        last_segment_missing=last_missing,
    )


def test_pure_logic_gap_overlap_and_contiguous() -> None:
    left = _timeline("a", BASE, BASE + timedelta(minutes=20))
    gapped = _timeline(
        "b", BASE + timedelta(minutes=25), BASE + timedelta(minutes=40)
    )
    touching = _timeline(
        "c", BASE + timedelta(minutes=40), BASE + timedelta(minutes=50)
    )
    overlapping = _timeline(
        "d", BASE + timedelta(minutes=45), BASE + timedelta(minutes=60)
    )

    verdicts = handoffs.evaluate_handoffs(
        [left, gapped, touching, overlapping], allowed_gap_seconds=600
    )
    assert len(verdicts) == 3

    assert verdicts[0].gap_seconds == 300.0
    assert verdicts[0].overlap_seconds == 0.0
    assert verdicts[0].handoff_issue is False

    assert verdicts[1].interval_seconds == 0.0
    assert verdicts[1].gap_seconds == 0.0
    assert verdicts[1].overlap_seconds == 0.0

    assert verdicts[2].interval_seconds == -300.0
    assert verdicts[2].gap_seconds == 0.0
    assert verdicts[2].overlap_seconds == 300.0
    assert verdicts[2].handoff_issue is False
    assert handoffs.route_is_compliant(verdicts) is True


def test_pure_logic_missing_either_side_flags_issue() -> None:
    left = _timeline(
        "a",
        BASE,
        BASE + timedelta(minutes=20),
        last_type="missing",
        last_missing=True,
    )
    right = _timeline(
        "b",
        BASE + timedelta(minutes=25),
        BASE + timedelta(minutes=40),
        first_type="high_excursion",
    )
    verdict = handoffs.evaluate_handoffs([left, right], 600)[0]
    assert verdict.handoff_issue is True
    assert verdict.issue_reasons == ("missing_boundary_segment",)
    assert verdict.from_timeline.last_segment_type == "missing"
    assert verdict.to_timeline.first_segment_type == "high_excursion"

    right_missing = _timeline(
        "c",
        BASE + timedelta(minutes=25),
        BASE + timedelta(minutes=40),
        first_type="missing",
        first_missing=True,
    )
    verdict = handoffs.evaluate_handoffs([left, right_missing], 600)[0]
    assert verdict.issue_reasons == ("missing_boundary_segment",)


def test_pure_logic_gap_and_missing_reasons_are_ordered() -> None:
    left = _timeline(
        "a",
        BASE,
        BASE + timedelta(minutes=20),
        last_type="missing",
        last_missing=True,
    )
    right = _timeline("b", BASE + timedelta(hours=2), BASE + timedelta(hours=3))
    verdict = handoffs.evaluate_handoffs([left, right], 600)[0]
    assert verdict.handoff_issue is True
    assert verdict.issue_reasons == (
        "gap_exceeds_threshold",
        "missing_boundary_segment",
    )
    assert handoffs.route_is_compliant([verdict]) is False


def test_pure_logic_equality_and_empty_verdicts() -> None:
    left = _timeline("a", BASE, BASE + timedelta(seconds=600))
    right = _timeline(
        "b", BASE + timedelta(seconds=1200), BASE + timedelta(seconds=1800)
    )
    verdict = handoffs.evaluate_handoffs([left, right], 600.0)[0]
    assert verdict.handoff_issue is False
    assert handoffs.route_is_compliant([verdict]) is True

    # 不足两项时没有交接，路线视为合规（输入约束由请求模型在接口层保证）。
    assert handoffs.evaluate_handoffs([left], 600) == []
    assert handoffs.route_is_compliant([]) is True
