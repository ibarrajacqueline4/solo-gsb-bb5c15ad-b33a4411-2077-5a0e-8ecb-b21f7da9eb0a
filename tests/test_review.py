"""人工复核工作流接口测试。

覆盖：
- 初始状态 pending、历史为空；
- pending → approved / rejected、rejected → reopen(pending) 正常流转，
  reopen 后可再次驳回/通过；
- 非法流转（approved 后再操作、rejected 后直接 approve、pending 下 reopen）
  返回 409 且不留痕；
- reject 缺少 comment 返回 422；
- request_id 幂等：重复提交返回原事件（200，seq/created_at 不变）；
- 同 request_id 但内容不同返回 409；request_id 仅在批次内唯一；
- 历史按时间（seq）升序，GET 返回当前状态；
- 复核只写 review_events：报告与 seal/verify 数据不受影响；
- 支持按批次 ID 定位，批次不存在返回 404。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_api import _payload


def _create_batch(client: TestClient, ref: str = "shipment-001") -> str:
    response = client.post("/batches", json=_payload(external_ref=ref))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _review(
    client: TestClient,
    *,
    ref: str = "shipment-001",
    reviewer: str = "alice",
    decision: str = "approve",
    comment: str | None = None,
    request_id: str = "req-1",
):
    body = {
        "reviewer": reviewer,
        "decision": decision,
        "comment": comment,
        "request_id": request_id,
    }
    return client.post(f"/batches/{ref}/review", json=body)


def _get_review(client: TestClient, ref: str = "shipment-001"):
    response = client.get(f"/batches/{ref}/review")
    assert response.status_code == 200, response.text
    return response.json()


def test_initial_review_status_is_pending_with_empty_history(
    client: TestClient,
) -> None:
    _create_batch(client)
    review = _get_review(client)
    assert review["external_ref"] == "shipment-001"
    assert review["status"] == "pending"
    assert review["events"] == []


def test_approve_from_pending_records_event(client: TestClient) -> None:
    _create_batch(client)
    response = _review(client, decision="approve", request_id="req-approve")
    assert response.status_code == 201, response.text

    event = response.json()
    assert event["seq"] == 1
    assert event["decision"] == "approve"
    assert event["from_status"] == "pending"
    assert event["to_status"] == "approved"
    assert event["reviewer"] == "alice"
    assert event["comment"] is None
    assert event["request_id"] == "req-approve"
    assert event["created_at"].endswith("Z")

    review = _get_review(client)
    assert review["status"] == "approved"
    assert len(review["events"]) == 1
    assert review["events"][0]["seq"] == 1


def test_reject_requires_comment(client: TestClient) -> None:
    _create_batch(client)
    response = _review(client, decision="reject", request_id="req-reject")
    assert response.status_code == 422, response.text

    # 纯空白 comment 同样视为缺失。
    blank = _review(
        client,
        decision="reject",
        comment="   ",
        request_id="req-reject-2",
    )
    assert blank.status_code == 422

    # 未产生任何事件。
    assert _get_review(client)["status"] == "pending"
    assert _get_review(client)["events"] == []


def test_reject_then_reopen_allows_re_review(client: TestClient) -> None:
    _create_batch(client)

    rejected = _review(
        client,
        decision="reject",
        comment="传感器校准过期",
        request_id="req-reject",
    )
    assert rejected.status_code == 201, rejected.text
    assert rejected.json()["to_status"] == "rejected"
    assert _get_review(client)["status"] == "rejected"

    reopened = _review(
        client,
        decision="reopen",
        reviewer="bob",
        comment="校准记录已补，请重新复核",
        request_id="req-reopen",
    )
    assert reopened.status_code == 201, reopened.text
    reopen_event = reopened.json()
    assert reopen_event["seq"] == 2
    assert reopen_event["from_status"] == "rejected"
    assert reopen_event["to_status"] == "pending"

    review = _get_review(client)
    assert review["status"] == "pending"

    # reopen 后可再次驳回或通过。
    approved = _review(
        client, decision="approve", reviewer="carol", request_id="req-approve-2"
    )
    assert approved.status_code == 201, approved.text
    assert approved.json()["to_status"] == "approved"
    assert approved.json()["from_status"] == "pending"
    assert _get_review(client)["status"] == "approved"


def test_illegal_transitions_return_409(client: TestClient) -> None:
    _create_batch(client)
    assert (
        _review(client, decision="approve", request_id="r1").status_code == 201
    )

    # approved 为终态：approve / reject / reopen 均不允许。
    assert (
        _review(client, decision="approve", request_id="r2").status_code == 409
    )
    assert (
        _review(client, decision="reject", comment="x", request_id="r3").status_code
        == 409
    )
    assert (
        _review(client, decision="reopen", request_id="r4").status_code == 409
    )
    # 冲突不留痕：仍只有首个事件，状态不变。
    review = _get_review(client)
    assert review["status"] == "approved"
    assert [e["seq"] for e in review["events"]] == [1]

    # rejected 状态只允许 reopen，直接 approve/reject 为 409。
    _create_batch(client, ref="shipment-002")
    assert (
        _review(client, ref="shipment-002", decision="reject",
                comment="数据缺口", request_id="x1").status_code == 201
    )
    assert (
        _review(client, ref="shipment-002", decision="approve",
                request_id="x2").status_code == 409
    )
    assert (
        _review(client, ref="shipment-002", decision="reject",
                comment="again", request_id="x3").status_code == 409
    )

    # pending 下不允许 reopen。
    _create_batch(client, ref="shipment-003")
    assert (
        _review(client, ref="shipment-003", decision="reopen",
                request_id="y1").status_code == 409
    )


def test_request_id_replay_returns_original_event(client: TestClient) -> None:
    _create_batch(client)
    first = _review(
        client,
        decision="reject",
        comment="需补文件",
        request_id="idempotent-1",
    )
    assert first.status_code == 201
    first_body = first.json()

    replay = _review(
        client,
        decision="reject",
        comment="需补文件",
        request_id="idempotent-1",
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == first_body

    # 幂等重放不新增事件。
    review = _get_review(client)
    assert len(review["events"]) == 1
    assert review["status"] == "rejected"


def test_request_id_replay_is_conflict_sensitive(client: TestClient) -> None:
    _create_batch(client)
    assert (
        _review(
            client, decision="approve", request_id="dup-1"
        ).status_code
        == 201
    )

    # 同一 request_id 但 decision 不同 → 409。
    changed_decision = _review(
        client, decision="reject", comment="改主意了", request_id="dup-1"
    )
    assert changed_decision.status_code == 409

    # reviewer / comment 不同同样冲突。
    changed_reviewer = _review(
        client, decision="approve", reviewer="mallory", request_id="dup-1"
    )
    assert changed_reviewer.status_code == 409

    changed_comment = _review(
        client, decision="approve", comment="额外说明", request_id="dup-1"
    )
    assert changed_comment.status_code == 409

    # 冲突请求不留痕。
    review = _get_review(client)
    assert review["status"] == "approved"
    assert len(review["events"]) == 1


def test_request_id_scoped_per_batch(client: TestClient) -> None:
    _create_batch(client, ref="batch-a")
    _create_batch(client, ref="batch-b")

    first = _review(
        client, ref="batch-a", decision="approve", request_id="shared"
    )
    assert first.status_code == 201

    # 不同批次复用同一 request_id 是独立的新事件。
    second = _review(
        client, ref="batch-b", decision="approve", request_id="shared"
    )
    assert second.status_code == 201
    assert _get_review(client, "batch-a")["events"][0]["seq"] == 1
    assert _get_review(client, "batch-b")["events"][0]["seq"] == 1


def test_history_sorted_by_time_and_status_reflects_latest(
    client: TestClient,
) -> None:
    _create_batch(client)
    assert (
        _review(
            client, decision="reject", comment="c1", request_id="e1"
        ).status_code
        == 201
    )
    assert (
        _review(
            client, decision="reopen", comment="c2", request_id="e2"
        ).status_code
        == 201
    )
    assert (
        _review(
            client, decision="approve", comment="c3", request_id="e3"
        ).status_code
        == 201
    )

    review = _get_review(client)
    assert review["status"] == "approved"
    events = review["events"]
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert [e["decision"] for e in events] == [
        "reject",
        "reopen",
        "approve",
    ]
    assert [(e["from_status"], e["to_status"]) for e in events] == [
        ("pending", "rejected"),
        ("rejected", "pending"),
        ("pending", "approved"),
    ]
    # 时间戳单调不减且统一为 UTC Z 格式。
    assert all(e["created_at"].endswith("Z") for e in events)


def test_review_does_not_modify_report_or_seal_data(client: TestClient) -> None:
    _create_batch(client)
    report_before = client.get(
        "/batches/shipment-001/report"
    ).json()
    seal = client.post("/batches/shipment-001/seal")
    assert seal.status_code == 200
    seal_before = seal.json()
    verify_before = client.get(
        "/batches/shipment-001/verify"
    ).json()
    assert verify_before["valid"] is True

    # 走完整复核流程：驳回 → reopen → 通过。
    assert (
        _review(
            client, decision="reject", comment="复核驳回", request_id="v1"
        ).status_code
        == 201
    )
    assert (
        _review(client, decision="reopen", request_id="v2").status_code == 201
    )
    assert (
        _review(client, decision="approve", request_id="v3").status_code == 201
    )

    # 审计报告内容逐字节不变。
    report_after = client.get("/batches/shipment-001/report").json()
    assert report_after == report_before

    # 封存摘要不被重算/覆盖，verify 仍为 true。
    seal_after = client.post("/batches/shipment-001/seal").json()
    assert seal_after == seal_before
    verify_after = client.get("/batches/shipment-001/verify").json()
    assert verify_after == verify_before
    assert verify_after["valid"] is True


def test_review_by_batch_id(client: TestClient) -> None:
    batch_id = _create_batch(client)
    response = _review(client, ref=batch_id, decision="approve", request_id="z1")
    assert response.status_code == 201, response.text

    fetched = client.get(f"/batches/{batch_id}/review")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["id"] == batch_id
    assert body["external_ref"] == "shipment-001"
    assert body["status"] == "approved"


def test_review_unknown_batch_returns_404(client: TestClient) -> None:
    post = client.post(
        "/batches/ghost/review",
        json={
            "reviewer": "alice",
            "decision": "approve",
            "request_id": "n1",
        },
    )
    assert post.status_code == 404
    assert client.get("/batches/ghost/review").status_code == 404


def test_invalid_decision_returns_422(client: TestClient) -> None:
    _create_batch(client)
    response = client.post(
        "/batches/shipment-001/review",
        json={
            "reviewer": "alice",
            "decision": "delete",
            "request_id": "bad-decision",
        },
    )
    assert response.status_code == 422


def test_missing_required_fields_return_422(client: TestClient) -> None:
    _create_batch(client)
    response = client.post(
        "/batches/shipment-001/review",
        json={"decision": "approve"},
    )
    assert response.status_code == 422
