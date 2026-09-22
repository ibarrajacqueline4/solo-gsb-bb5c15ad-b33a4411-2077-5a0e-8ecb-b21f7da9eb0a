"""数据库读写：批次提交（写入读数与审计片段）、报告查询、封存摘要与人工复核。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from . import audit, handoffs, sealing
from .schemas import BatchCreate, ReviewDecision, ReviewStatus, to_utc


class DuplicateBatchError(Exception):
    """external_ref 已存在。"""


class BatchNotFoundError(Exception):
    """交接检查中引用的批次不存在（参数为调用方提供的 batch_ref）。"""


class DuplicateHandoffBatchError(Exception):
    """交接检查中多个引用解析到了同一批次（参数为重复批次的标识）。"""


class ReviewStateConflictError(Exception):
    """复核操作不符合状态流转规则（如 approved 后再次 approve）。"""


class ReviewRequestConflictError(Exception):
    """request_id 已用于该批次，但本次请求内容与首次提交不一致。"""


# 允许的复核状态流转：decision -> {from_status}。
# pending 可通过/驳回；驳回后仅可 reopen；reopen 回到 pending。
_REVIEW_TRANSITIONS: dict[str, frozenset[str]] = {
    ReviewDecision.APPROVE.value: frozenset({ReviewStatus.PENDING.value}),
    ReviewDecision.REJECT.value: frozenset({ReviewStatus.PENDING.value}),
    ReviewDecision.REOPEN.value: frozenset({ReviewStatus.REJECTED.value}),
}
_REVIEW_TARGET_STATUS: dict[str, str] = {
    ReviewDecision.APPROVE.value: ReviewStatus.APPROVED.value,
    ReviewDecision.REJECT.value: ReviewStatus.REJECTED.value,
    ReviewDecision.REOPEN.value: ReviewStatus.PENDING.value,
}


def _utc_iso(value: datetime) -> str:
    """以带 Z 的 ISO 8601 字符串持久化 UTC 时间。"""

    return value.isoformat().replace("+00:00", "Z")


def create_batch(conn: sqlite3.Connection, payload: BatchCreate) -> str:
    """执行审计并原子化写入批次、读数与全部片段，返回批次 ID。"""

    external_ref = payload.external_ref or uuid.uuid4().hex
    batch_id = uuid.uuid4().hex
    submitted_at = datetime.now(timezone.utc)

    readings = [
        audit.Reading(
            timestamp=to_utc(item.timestamp),
            temperature=item.temperature_c,
            source_index=index,
        )
        for index, item in enumerate(payload.readings)
    ]
    result = audit.analyze(
        readings,
        low_limit=payload.low_limit_c,
        high_limit=payload.high_limit_c,
        hysteresis=payload.hysteresis_c,
        max_interval_seconds=payload.max_interval_seconds,
    )

    try:
        with conn:
            existing = conn.execute(
                "SELECT 1 FROM batches WHERE external_ref = ?",
                (external_ref,),
            ).fetchone()
            if existing is not None:
                raise DuplicateBatchError(external_ref)

            start_time = min(r.timestamp for r in readings)
            end_time = max(r.timestamp for r in readings)

            conn.execute(
                """
                INSERT INTO batches (
                    id, external_ref, submitted_at, start_time, end_time,
                    low_limit_c, high_limit_c, hysteresis_c,
                    max_interval_seconds, compliant, reading_count,
                    high_excursion_count, low_excursion_count,
                    missing_segment_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    external_ref,
                    _utc_iso(submitted_at),
                    _utc_iso(start_time),
                    _utc_iso(end_time),
                    payload.low_limit_c,
                    payload.high_limit_c,
                    payload.hysteresis_c,
                    payload.max_interval_seconds,
                    int(result.compliant),
                    len(readings),
                    result.high_excursion_count,
                    result.low_excursion_count,
                    result.missing_segment_count,
                ),
            )

            conn.executemany(
                """
                INSERT INTO readings (batch_id, idx, timestamp, temperature_c)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        batch_id,
                        reading.source_index,
                        _utc_iso(reading.timestamp),
                        reading.temperature,
                    )
                    for reading in readings
                ],
            )

            conn.executemany(
                """
                INSERT INTO segments (
                    batch_id, idx, type, start_time, end_time,
                    duration_seconds, sample_count, peak_temperature_c,
                    peak_at, max_temperature_c, min_temperature_c,
                    missing, compliant
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        batch_id,
                        index,
                        segment.type,
                        _utc_iso(segment.start_time),
                        _utc_iso(segment.end_time),
                        segment.duration_seconds,
                        segment.sample_count,
                        segment.peak_temperature,
                        _utc_iso(segment.peak_at)
                        if segment.peak_at is not None
                        else None,
                        segment.max_temperature,
                        segment.min_temperature,
                        int(segment.missing),
                        int(segment.compliant),
                    )
                    for index, segment in enumerate(result.segments)
                ],
            )
    except sqlite3.IntegrityError as exc:
        # 并发下 UNIQUE(external_ref) 的兜底。
        raise DuplicateBatchError(external_ref) from exc

    return batch_id


def _row_to_report(row: sqlite3.Row, segments: list[sqlite3.Row]) -> dict:
    return {
        "id": row["id"],
        "external_ref": row["external_ref"],
        "submitted_at": row["submitted_at"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "low_limit_c": row["low_limit_c"],
        "high_limit_c": row["high_limit_c"],
        "hysteresis_c": row["hysteresis_c"],
        "max_interval_seconds": row["max_interval_seconds"],
        "compliant": bool(row["compliant"]),
        "reading_count": row["reading_count"],
        "segment_count": len(segments),
        "high_excursion_count": row["high_excursion_count"],
        "low_excursion_count": row["low_excursion_count"],
        "missing_segment_count": row["missing_segment_count"],
        "segments": [
            {
                "index": segment_row["idx"],
                "type": segment_row["type"],
                "start_time": segment_row["start_time"],
                "end_time": segment_row["end_time"],
                "duration_seconds": segment_row["duration_seconds"],
                "sample_count": segment_row["sample_count"],
                "peak_temperature_c": segment_row["peak_temperature_c"],
                "peak_at": segment_row["peak_at"],
                "max_temperature_c": segment_row["max_temperature_c"],
                "min_temperature_c": segment_row["min_temperature_c"],
                "missing": bool(segment_row["missing"]),
                "compliant": bool(segment_row["compliant"]),
            }
            for segment_row in segments
        ],
    }


def _fetch_batch_row(
    conn: sqlite3.Connection, batch_ref: str
) -> sqlite3.Row | None:
    """按批次 ID 或 external_ref 定位批次行。"""

    return conn.execute(
        "SELECT * FROM batches WHERE id = ? OR external_ref = ?",
        (batch_ref, batch_ref),
    ).fetchone()


def get_report(
    conn: sqlite3.Connection, batch_ref: str
) -> dict | None:
    """按批次 ID 或 external_ref 获取完整审计报告。"""

    row = _fetch_batch_row(conn, batch_ref)
    if row is None:
        return None

    segments = conn.execute(
        "SELECT * FROM segments WHERE batch_id = ? ORDER BY idx",
        (row["id"],),
    ).fetchall()
    return _row_to_report(row, segments)


def _row_to_seal(batch_row: sqlite3.Row, seal_row: sqlite3.Row) -> dict:
    return {
        "id": batch_row["id"],
        "external_ref": batch_row["external_ref"],
        "digest_algorithm": seal_row["digest_algorithm"],
        "digest": seal_row["digest"],
        "sealed_at": seal_row["sealed_at"],
        "canonical": json.loads(seal_row["canonical_json"]),
    }


def seal_report(conn: sqlite3.Connection, batch_ref: str) -> dict | None:
    """首次封存报告摘要并记录 sealed_at；重复调用幂等返回同一结果。

    封存后摘要不可变：重复调用不会覆盖已保存的 digest / canonical_json /
    sealed_at。批次不存在时返回 None。
    """

    batch_row = _fetch_batch_row(conn, batch_ref)
    if batch_row is None:
        return None

    with conn:
        existing = conn.execute(
            "SELECT * FROM batch_seals WHERE batch_id = ?",
            (batch_row["id"],),
        ).fetchone()
        if existing is None:
            segments = conn.execute(
                "SELECT * FROM segments WHERE batch_id = ? ORDER BY idx",
                (batch_row["id"],),
            ).fetchall()
            report = _row_to_report(batch_row, segments)
            canonical_json, digest = sealing.canonical_summary(report)
            sealed_at = _utc_iso(datetime.now(timezone.utc))
            # INSERT OR IGNORE：并发首次封存时只有一方写入，保证幂等不报错。
            conn.execute(
                """
                INSERT OR IGNORE INTO batch_seals (
                    batch_id, sealed_at, canonical_json, digest,
                    digest_algorithm
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    batch_row["id"],
                    sealed_at,
                    canonical_json,
                    digest,
                    sealing.DIGEST_ALGORITHM,
                ),
            )

        # 统一以库中已存在的封存记录为准返回。
        seal_row = conn.execute(
            "SELECT * FROM batch_seals WHERE batch_id = ?",
            (batch_row["id"],),
        ).fetchone()

    assert seal_row is not None  # 刚插入或必然存在
    return _row_to_seal(batch_row, seal_row)


def get_verification(conn: sqlite3.Connection, batch_ref: str) -> dict | None:
    """重新计算当前报告摘要并与封存摘要比对。

    只读操作：无论是否一致都不会修改批次、读数、片段或封存记录。
    返回中的 ``sealed_at`` / ``sealed_digest`` 在未封存时为 None，
    此时 ``valid`` 恒为 False。
    """

    batch_row = _fetch_batch_row(conn, batch_ref)
    if batch_row is None:
        return None

    segments = conn.execute(
        "SELECT * FROM segments WHERE batch_id = ? ORDER BY idx",
        (batch_row["id"],),
    ).fetchall()
    _, digest = sealing.canonical_summary(_row_to_report(batch_row, segments))

    seal_row = conn.execute(
        "SELECT * FROM batch_seals WHERE batch_id = ?",
        (batch_row["id"],),
    ).fetchone()

    if seal_row is None:
        sealed_digest = None
        sealed_at = None
        valid = False
    else:
        sealed_digest = seal_row["digest"]
        sealed_at = seal_row["sealed_at"]
        valid = digest == sealed_digest

    return {
        "id": batch_row["id"],
        "external_ref": batch_row["external_ref"],
        "digest_algorithm": sealing.DIGEST_ALGORITHM,
        "digest": digest,
        "sealed_digest": sealed_digest,
        "sealed_at": sealed_at,
        "valid": valid,
    }


def list_batches(
    conn: sqlite3.Connection,
    *,
    compliant: bool | None,
    limit: int,
    offset: int,
) -> tuple[list[dict], int]:
    where = ""
    params: list[object] = []
    if compliant is not None:
        where = "WHERE compliant = ?"
        params.append(int(compliant))

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM batches {where}", params
    ).fetchone()["c"]

    rows = conn.execute(
        f"""
        SELECT * FROM batches {where}
        ORDER BY submitted_at DESC, id
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()

    items = [
        {
            "id": row["id"],
            "external_ref": row["external_ref"],
            "submitted_at": row["submitted_at"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "compliant": bool(row["compliant"]),
            "reading_count": row["reading_count"],
            "high_excursion_count": row["high_excursion_count"],
            "low_excursion_count": row["low_excursion_count"],
            "missing_segment_count": row["missing_segment_count"],
        }
        for row in rows
    ]
    return items, total


# ---- 人工复核工作流 ----


def _row_to_review_event(row: sqlite3.Row) -> dict:
    return {
        "seq": row["seq"],
        "decision": row["decision"],
        "from_status": row["from_status"],
        "to_status": row["to_status"],
        "reviewer": row["reviewer"],
        "comment": row["comment"],
        "request_id": row["request_id"],
        "created_at": row["created_at"],
    }


def _latest_review_state(
    conn: sqlite3.Connection, batch_id: str
) -> tuple[str, int]:
    """返回批次当前复核状态与下一个事件序号。

    无事件时状态为 pending、序号从 1 开始；状态以最后一条事件为准。
    """

    latest = conn.execute(
        """
        SELECT to_status, seq FROM review_events
        WHERE batch_id = ?
        ORDER BY seq DESC
        LIMIT 1
        """,
        (batch_id,),
    ).fetchone()
    if latest is None:
        return ReviewStatus.PENDING.value, 1
    return latest["to_status"], latest["seq"] + 1


def submit_review(
    conn: sqlite3.Connection,
    batch_ref: str,
    *,
    reviewer: str,
    decision: ReviewDecision,
    comment: str | None,
    request_id: str,
) -> tuple[dict | None, bool]:
    """追加一条复核事件，返回 ``(event, created)``。

    - 批次不存在返回 ``(None, False)``；
    - request_id 重复：内容一致则幂等返回原事件 ``(event, False)``，
      内容不一致抛 :class:`ReviewRequestConflictError`；
    - 状态流转不合法抛 :class:`ReviewStateConflictError`；
    - 新事件成功写入时返回 ``(event, True)``。

    只写 review_events 表，不触碰批次、读数、片段与封存数据。
    """

    batch_row = _fetch_batch_row(conn, batch_ref)
    if batch_row is None:
        return None, False

    decision_value = decision.value

    # BEGIN IMMEDIATE：先取写锁再读最新状态，避免并发双方同时通过/驳回。
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            """
            SELECT * FROM review_events
            WHERE batch_id = ? AND request_id = ?
            """,
            (batch_row["id"], request_id),
        ).fetchone()
        if existing is not None:
            # 幂等重放：仅当业务内容完全一致时返回原事件，否则视为冲突。
            if (
                existing["decision"] == decision_value
                and existing["reviewer"] == reviewer
                and (existing["comment"] or None) == (comment or None)
            ):
                return _row_to_review_event(existing), False
            raise ReviewRequestConflictError(request_id)

        current_status, next_seq = _latest_review_state(
            conn, batch_row["id"]
        )
        if current_status not in _REVIEW_TRANSITIONS[decision_value]:
            raise ReviewStateConflictError(decision_value, current_status)

        to_status = _REVIEW_TARGET_STATUS[decision_value]
        created_at = _utc_iso(datetime.now(timezone.utc))
        conn.execute(
            """
            INSERT INTO review_events (
                batch_id, seq, decision, from_status, to_status,
                reviewer, comment, request_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_row["id"],
                next_seq,
                decision_value,
                current_status,
                to_status,
                reviewer,
                comment,
                request_id,
                created_at,
            ),
        )
    except Exception:
        conn.rollback()
        raise

    # 读取置于提交之后；get_db_ctx 退出时的再次 commit 对只读语句无副作用。
    conn.commit()
    event_row = conn.execute(
        "SELECT * FROM review_events WHERE batch_id = ? AND seq = ?",
        (batch_row["id"], next_seq),
    ).fetchone()
    assert event_row is not None  # 刚写入，必然存在
    return _row_to_review_event(event_row), True


def get_review(
    conn: sqlite3.Connection, batch_ref: str
) -> dict | None:
    """返回批次当前复核状态与按时间（seq）升序排列的事件历史。只读。"""

    batch_row = _fetch_batch_row(conn, batch_ref)
    if batch_row is None:
        return None

    event_rows = conn.execute(
        """
        SELECT * FROM review_events
        WHERE batch_id = ?
        ORDER BY seq ASC, id ASC
        """,
        (batch_row["id"],),
    ).fetchall()

    status = (
        event_rows[-1]["to_status"]
        if event_rows
        else ReviewStatus.PENDING.value
    )
    return {
        "id": batch_row["id"],
        "external_ref": batch_row["external_ref"],
        "status": status,
        "events": [_row_to_review_event(row) for row in event_rows],
    }


# ---- 运输交接连续性检查（只读）----


def _parse_timestamp(value: str) -> datetime:
    """解析持久化的带 Z ISO 8601 时间字符串为 UTC datetime。"""

    return to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _load_timeline(
    conn: sqlite3.Connection, batch_row: sqlite3.Row
) -> handoffs.BatchTimeline:
    """读取一个批次的边界时间与按时间轴排序的首/末片段。只读。"""

    batch_id = batch_row["id"]
    first_row = conn.execute(
        """
        SELECT * FROM segments
        WHERE batch_id = ?
        ORDER BY start_time ASC, idx ASC
        LIMIT 1
        """,
        (batch_id,),
    ).fetchone()
    last_row = conn.execute(
        """
        SELECT * FROM segments
        WHERE batch_id = ?
        ORDER BY end_time DESC, idx DESC
        LIMIT 1
        """,
        (batch_id,),
    ).fetchone()
    # 片段由审计引擎保证至少一个；写时审计不可能产生空时间轴。
    assert first_row is not None and last_row is not None

    return handoffs.BatchTimeline(
        id=batch_id,
        external_ref=batch_row["external_ref"],
        start_time=_parse_timestamp(batch_row["start_time"]),
        end_time=_parse_timestamp(batch_row["end_time"]),
        first_segment_type=first_row["type"],
        first_segment_missing=bool(first_row["missing"]),
        last_segment_type=last_row["type"],
        last_segment_missing=bool(last_row["missing"]),
    )


def check_handoffs(
    conn: sqlite3.Connection,
    batch_refs: list[str],
    allowed_gap_seconds: float,
) -> dict:
    """按运输顺序检查相邻批次的交接连续性。

    纯只读：不写入或修改批次、读数、片段、封存与复核数据。

    - 任一引用（ID 或 external_ref）不存在抛 :class:`BatchNotFoundError`；
    - 多个引用解析到同一批次抛 :class:`DuplicateHandoffBatchError`；
    - 其余输入约束（至少两项、字符串重复）由请求模型保证。
    """

    timelines: list[handoffs.BatchTimeline] = []
    seen_ids: set[str] = set()
    for batch_ref in batch_refs:
        batch_row = _fetch_batch_row(conn, batch_ref)
        if batch_row is None:
            raise BatchNotFoundError(batch_ref)
        if batch_row["id"] in seen_ids:
            raise DuplicateHandoffBatchError(
                f"{batch_ref} -> {batch_row['external_ref']}"
            )
        seen_ids.add(batch_row["id"])
        timelines.append(_load_timeline(conn, batch_row))

    verdicts = handoffs.evaluate_handoffs(timelines, allowed_gap_seconds)

    return {
        "batch_refs": batch_refs,
        "allowed_gap_seconds": allowed_gap_seconds,
        "handoff_count": len(verdicts),
        "issue_count": sum(1 for v in verdicts if v.handoff_issue),
        "route_compliant": handoffs.route_is_compliant(verdicts),
        "handoffs": [_verdict_to_dict(verdict) for verdict in verdicts],
    }


def _verdict_to_dict(verdict: handoffs.HandoffVerdict) -> dict:
    left = verdict.from_timeline
    right = verdict.to_timeline
    return {
        "index": verdict.index,
        "from_batch": {
            "batch_id": left.id,
            "external_ref": left.external_ref,
            "boundary_time": left.end_time,
            "segment_type": left.last_segment_type,
            "segment_missing": left.last_segment_missing,
        },
        "to_batch": {
            "batch_id": right.id,
            "external_ref": right.external_ref,
            "boundary_time": right.start_time,
            "segment_type": right.first_segment_type,
            "segment_missing": right.first_segment_missing,
        },
        "from_end_time": left.end_time,
        "to_start_time": right.start_time,
        "interval_seconds": verdict.interval_seconds,
        "gap_seconds": verdict.gap_seconds,
        "overlap_seconds": verdict.overlap_seconds,
        "handoff_issue": verdict.handoff_issue,
        "issue_reasons": list(verdict.issue_reasons),
    }
