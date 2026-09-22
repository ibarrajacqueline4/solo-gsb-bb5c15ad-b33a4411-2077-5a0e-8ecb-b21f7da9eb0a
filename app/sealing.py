"""报告封存摘要：canonical JSON 构造与 SHA-256 摘要。

封存（seal）对一份完整审计报告（结构与 ``schemas.BatchReport`` 一致）按
**固定字段顺序**构造 canonical JSON：

- 顶层与片段字段顺序固定（与对外报告字段顺序一致），不依赖调用方；
- 所有时间戳统一换算到 UTC，以 ``Z`` 结尾；
- 紧凑序列化（键值间无多余空白），UTF-8 编码，非 ASCII 字符不转义；
- 摘要为 canonical JSON 的 SHA-256 十六进制小写字符串。

摘要只依赖已持久化的审计数据，因此同一份报告在任意时间、任意进程中
重新计算都会得到完全相同的结果。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from .schemas import to_utc

DIGEST_ALGORITHM = "sha256"

# 顶层字段固定顺序（与 BatchReport 的字段顺序保持一致）。
REPORT_FIELDS: tuple[str, ...] = (
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
)

# 片段字段固定顺序（与 SegmentOut 的字段顺序保持一致）。
SEGMENT_FIELDS: tuple[str, ...] = (
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
)

_REPORT_DATETIME_FIELDS = frozenset(("submitted_at", "start_time", "end_time"))
_SEGMENT_DATETIME_FIELDS = frozenset(("start_time", "end_time", "peak_at"))


def _to_utc_iso(value: object) -> object:
    """将持久化的时间字符串归一化为带 Z 的 UTC ISO 8601；None 原样返回。"""

    if not isinstance(value, str):
        return value
    # Python 3.11 的 fromisoformat 原生支持 Z；旧写法做一次兜底替换。
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return to_utc(parsed).isoformat().replace("+00:00", "Z")


def build_canonical_document(report: dict) -> dict:
    """按固定字段顺序构造 canonical 报告文档（普通 dict，保留插入顺序）。"""

    segments: list[dict] = []
    for segment in report["segments"]:
        canonical_segment = {}
        for field in SEGMENT_FIELDS:
            value = segment[field]
            if field in _SEGMENT_DATETIME_FIELDS:
                value = _to_utc_iso(value)
            canonical_segment[field] = value
        segments.append(canonical_segment)

    document: dict = {}
    for field in REPORT_FIELDS:
        if field == "segments":
            document[field] = segments
        elif field in _REPORT_DATETIME_FIELDS:
            document[field] = _to_utc_iso(report[field])
        else:
            document[field] = report[field]
    return document


def serialize_canonical(document: dict) -> str:
    """紧凑序列化 canonical 文档（无空白、UTF-8 安全、禁止 NaN/Infinity）。"""

    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest_of(canonical_json: str) -> str:
    """计算 canonical JSON 的 SHA-256 摘要（十六进制小写）。"""

    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def canonical_summary(report: dict) -> tuple[str, str]:
    """由审计报告生成 ``(canonical_json, digest)``。"""

    document = build_canonical_document(report)
    canonical_json = serialize_canonical(document)
    return canonical_json, digest_of(canonical_json)
