"""封存（seal）与校验（verify）接口测试。

覆盖：
- 首次封存生成 canonical JSON 与 SHA-256 摘要，重复调用幂等；
- canonical 文档固定字段顺序、UTC（Z）时间归一化；
- verify 在未篡改时 valid=true，且不修改任何数据；
- 直接篡改持久化数据后 verify valid=false，审计数据与封存摘要均不被改写；
- 未封存批次与不存在批次的行为。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from fastapi.testclient import TestClient

from tests.test_api import _payload


def _seal(client: TestClient, ref: str = "shipment-001"):
    response = client.post(f"/batches/{ref}/seal")
    assert response.status_code == 200, response.text
    return response.json()


def test_seal_creates_sha256_digest_and_canonical(client: TestClient) -> None:
    created = client.post("/batches", json=_payload())
    assert created.status_code == 201, created.text
    report = created.json()

    seal = _seal(client)

    assert seal["external_ref"] == "shipment-001"
    assert seal["id"] == report["id"]
    assert seal["digest_algorithm"] == "sha256"
    assert len(seal["digest"]) == 64
    int(seal["digest"], 16)  # 纯十六进制
    assert seal["sealed_at"].endswith("Z")

    # canonical 文档与报告内容一致，但字段顺序固定。
    canonical = seal["canonical"]
    assert list(canonical.keys()) == [
        "id",
        "external_ref",
        "submitted_at",
        "start_time",
        "end_time",
        "low_limit_c",
        "high_limit_c",
        "hysteresis_c",
        "max_interval_seconds",
        "compliant",
        "reading_count",
        "segment_count",
        "high_excursion_count",
        "low_excursion_count",
        "missing_segment_count",
        "segments",
    ]
    assert list(canonical["segments"][0].keys()) == [
        "index",
        "type",
        "start_time",
        "end_time",
        "duration_seconds",
        "sample_count",
        "peak_temperature_c",
        "peak_at",
        "max_temperature_c",
        "min_temperature_c",
        "missing",
        "compliant",
    ]
    assert canonical["segments"] == report["segments"]

    # 摘要等于“紧凑序列化 canonical 文档”的 SHA-256。
    compact = json.dumps(
        canonical, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    assert hashlib.sha256(compact.encode("utf-8")).hexdigest() == seal["digest"]


def test_repeated_seal_returns_identical_result(client: TestClient) -> None:
    client.post("/batches", json=_payload())

    first = _seal(client)
    second = _seal(client)
    third = _seal(client)

    assert second == first
    assert third == first
    assert second["digest"] == first["digest"]
    assert second["sealed_at"] == first["sealed_at"]
    assert second["canonical"] == first["canonical"]


def test_verify_matches_after_seal_and_is_read_only(
    client: TestClient,
) -> None:
    client.post("/batches", json=_payload())
    seal = _seal(client)

    response = client.get("/batches/shipment-001/verify")
    assert response.status_code == 200, response.text
    verification = response.json()

    assert verification["valid"] is True
    assert verification["digest"] == seal["digest"]
    assert verification["sealed_digest"] == seal["digest"]
    assert verification["sealed_at"] == seal["sealed_at"]

    # 再次校验结果稳定，且封存摘要不变（verify 只读）。
    again = client.get("/batches/shipment-001/verify")
    assert again.json() == verification
    assert _seal(client) == seal


def test_tampering_makes_verify_invalid_without_modifying_audit_data(
    client: TestClient,
) -> None:
    import app.config as config

    client.post("/batches", json=_payload())
    seal = _seal(client)

    # 绕过 API 直接篡改持久化的审计数据（模拟库被改动）。
    tampered_conn = sqlite3.connect(config.DB_PATH)
    try:
        tampered_conn.execute(
            "UPDATE batches SET high_excursion_count = 9 WHERE external_ref = ?",
            ("shipment-001",),
        )
        tampered_conn.commit()
    finally:
        tampered_conn.close()

    verification = client.get("/batches/shipment-001/verify").json()
    assert verification["valid"] is False
    assert verification["digest"] != seal["digest"]
    assert verification["sealed_digest"] == seal["digest"]
    assert verification["sealed_at"] == seal["sealed_at"]

    # 报告接口反映的是被篡改后的当前数据，未被 verify 修复。
    report = client.get("/batches/shipment-001/report").json()
    assert report["high_excursion_count"] == 9

    # 重复 seal 仍是首次封存的结果：不会用被篡改数据覆盖封存摘要。
    assert _seal(client) == seal

    # 再校验一次：valid 仍为 false，数据维持篡改后的样子。
    again = client.get("/batches/shipment-001/verify").json()
    assert again["valid"] is False
    assert again["digest"] == verification["digest"]
    assert again["sealed_digest"] == seal["digest"]
    report_again = client.get("/batches/shipment-001/report").json()
    assert report_again["high_excursion_count"] == 9


def test_tampering_segment_payload_invalidates_digest(
    client: TestClient,
) -> None:
    import app.config as config

    client.post("/batches", json=_payload())
    seal = _seal(client)

    tampered_conn = sqlite3.connect(config.DB_PATH)
    try:
        tampered_conn.execute(
            "UPDATE segments SET peak_temperature_c = 42.0 WHERE idx = 0"
        )
        tampered_conn.commit()
    finally:
        tampered_conn.close()

    verification = client.get("/batches/shipment-001/verify").json()
    assert verification["valid"] is False
    assert verification["sealed_digest"] == seal["digest"]


def test_unsealed_batch_verify_reports_invalid_with_nulls(
    client: TestClient,
) -> None:
    client.post("/batches", json=_payload())

    verification = client.get("/batches/shipment-001/verify")
    assert verification.status_code == 200, verification.text
    body = verification.json()
    assert body["valid"] is False
    assert body["sealed_digest"] is None
    assert body["sealed_at"] is None
    assert len(body["digest"]) == 64


def test_seal_and_verify_by_batch_id(client: TestClient) -> None:
    created = client.post("/batches", json=_payload())
    batch_id = created.json()["id"]

    seal = client.post(f"/batches/{batch_id}/seal")
    assert seal.status_code == 200, seal.text
    assert seal.json()["external_ref"] == "shipment-001"

    verification = client.get(f"/batches/{batch_id}/verify")
    assert verification.status_code == 200
    assert verification.json()["valid"] is True


def test_seal_and_verify_unknown_batch_return_404(client: TestClient) -> None:
    assert client.post("/batches/unknown/seal").status_code == 404
    assert client.get("/batches/unknown/verify").status_code == 404


def test_canonical_normalizes_offset_timestamps_to_utc_z(
    client: TestClient,
) -> None:
    payload = _payload(
        external_ref="tz-batch",
        readings=[
            {"timestamp": "2026-09-22T16:00:00+08:00", "temperature": 5.0},
            {"timestamp": "2026-09-22T16:10:00+08:00", "temperature": 5.2},
        ],
    )
    client.post("/batches", json=payload)
    seal = _seal(client, ref="tz-batch")

    canonical = seal["canonical"]
    assert canonical["start_time"] == "2026-09-22T08:00:00Z"
    assert canonical["end_time"] == "2026-09-22T08:10:00Z"
    assert canonical["segments"][0]["start_time"] == "2026-09-22T08:00:00Z"

    # 与按当前报告重算的摘要完全一致（确定性）。
    report = client.get("/batches/tz-batch/report").json()
    from app import sealing

    _, recomputed = sealing.canonical_summary(report)
    assert recomputed == seal["digest"]
