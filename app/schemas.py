"""请求 / 响应 Pydantic 模型。"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .config import (
    DEFAULT_HYSTERESIS_C,
    DEFAULT_MAX_INTERVAL_SECONDS,
    MAX_READINGS_PER_BATCH,
)

_EXTERNAL_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.@\-/ ]{0,127}$"


class ReadingIn(BaseModel):
    timestamp: datetime = Field(
        ..., description="ISO 8601 时间戳，建议带时区；不带时区按 UTC 处理"
    )
    temperature_c: float = Field(..., alias="temperature")

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("temperature_c")
    @classmethod
    def _finite_temperature(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("temperature must be a finite number")
        return value


class BatchCreate(BaseModel):
    external_ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="业务批次号（唯一）；省略则自动生成",
        pattern=_EXTERNAL_REF_PATTERN,
    )
    low_limit_c: float = Field(..., alias="low_limit")
    high_limit_c: float = Field(..., alias="high_limit")
    hysteresis_c: float = Field(
        default=DEFAULT_HYSTERESIS_C,
        alias="hysteresis",
        ge=0,
        description="滞回阈值（°C），用于避免临界抖动",
    )
    max_interval_seconds: float = Field(
        default=DEFAULT_MAX_INTERVAL_SECONDS,
        gt=0,
        description="采样间隔上限（秒）；严格超过即切出缺测片段",
    )
    readings: list[ReadingIn] = Field(..., min_length=1)

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("readings")
    @classmethod
    def _limit_batch_size(cls, value: list[ReadingIn]) -> list[ReadingIn]:
        if len(value) > MAX_READINGS_PER_BATCH:
            raise ValueError(
                f"too many readings: max {MAX_READINGS_PER_BATCH}"
            )
        return value

    @model_validator(mode="after")
    def _validate_limits(self) -> "BatchCreate":
        if not (
            math.isfinite(self.low_limit_c)
            and math.isfinite(self.high_limit_c)
            and math.isfinite(self.hysteresis_c)
            and math.isfinite(self.max_interval_seconds)
        ):
            raise ValueError("limits and hysteresis must be finite numbers")
        if self.low_limit_c >= self.high_limit_c:
            raise ValueError("low_limit must be lower than high_limit")
        if self.hysteresis_c * 2 >= self.high_limit_c - self.low_limit_c:
            raise ValueError(
                "hysteresis must be less than half of the limit band"
            )
        return self


class SegmentOut(BaseModel):
    index: int
    type: str
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    sample_count: int
    peak_temperature_c: float | None
    peak_at: datetime | None
    max_temperature_c: float | None
    min_temperature_c: float | None
    missing: bool
    compliant: bool


class BatchReport(BaseModel):
    id: str
    external_ref: str
    submitted_at: datetime
    start_time: datetime
    end_time: datetime
    low_limit_c: float
    high_limit_c: float
    hysteresis_c: float
    max_interval_seconds: float
    compliant: bool
    reading_count: int
    segment_count: int
    high_excursion_count: int
    low_excursion_count: int
    missing_segment_count: int
    segments: list[SegmentOut]


class BatchSummary(BaseModel):
    id: str
    external_ref: str
    submitted_at: datetime
    start_time: datetime
    end_time: datetime
    compliant: bool
    reading_count: int
    high_excursion_count: int
    low_excursion_count: int
    missing_segment_count: int


class BatchList(BaseModel):
    items: list[BatchSummary]
    total: int
    limit: int
    offset: int


class BatchSeal(BaseModel):
    """封存摘要：首次封存保存摘要与 sealed_at，重复调用返回同一结果。"""

    id: str
    external_ref: str
    digest_algorithm: str
    digest: str
    sealed_at: datetime
    canonical: dict


class SealVerification(BaseModel):
    """封存校验：重新计算当前报告摘要并与封存摘要比对。

    校验为只读操作，valid=false 时也不会改动任何审计数据。
    未封存的批次 sealed_digest / sealed_at 为 null，valid 恒为 false。
    """

    id: str
    external_ref: str
    digest_algorithm: str
    digest: str
    sealed_digest: str | None
    sealed_at: datetime | None
    valid: bool


# ---- 人工复核工作流 ----


class ReviewDecision(str, Enum):
    """复核操作：通过 / 驳回 / 重新打开（回到待复核）。"""

    APPROVE = "approve"
    REJECT = "reject"
    REOPEN = "reopen"


class ReviewStatus(str, Enum):
    """复核状态。reopen 后状态回到 pending，可再次通过或驳回。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewCreate(BaseModel):
    """人工复核请求。

    - reject（驳回）必须填写非空白 comment；
    - request_id 为幂等键：同一批次重复提交返回首次写入的事件，
      若请求内容不同则返回 409。
    """

    reviewer: str = Field(
        ..., min_length=1, max_length=200, description="复核人标识"
    )
    decision: ReviewDecision = Field(..., description="approve/reject/reopen")
    comment: str | None = Field(
        default=None, max_length=4000, description="复核意见（驳回时必填）"
    )
    request_id: str = Field(
        ..., min_length=1, max_length=128, description="幂等键，由调用方生成"
    )

    @field_validator("reviewer")
    @classmethod
    def _strip_reviewer(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reviewer must not be blank")
        return value

    @field_validator("comment")
    @classmethod
    def _normalize_comment(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip() or None
        return value

    @model_validator(mode="after")
    def _reject_requires_comment(self) -> "ReviewCreate":
        if self.decision is ReviewDecision.REJECT and not self.comment:
            raise ValueError("comment is required when decision is reject")
        return self


class ReviewEventOut(BaseModel):
    """单次复核事件。"""

    seq: int = Field(..., description="事件序号，按发生顺序自增")
    decision: ReviewDecision
    from_status: ReviewStatus
    to_status: ReviewStatus
    reviewer: str
    comment: str | None
    request_id: str
    created_at: datetime


class BatchReview(BaseModel):
    """批次复核状态与完整事件历史。

    从未复核时 status 为 pending、events 为空。
    """

    id: str
    external_ref: str
    status: ReviewStatus
    events: list[ReviewEventOut]


def to_utc(value: datetime) -> datetime:
    """将时间戳统一换算到 UTC（naive 输入按 UTC 解释）。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
