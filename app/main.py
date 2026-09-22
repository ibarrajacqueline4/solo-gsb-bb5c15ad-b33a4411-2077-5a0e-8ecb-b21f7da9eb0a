"""冷链温度合规审计 API（FastAPI + SQLite）。

启动时自动建库建表，默认零配置、无需第三方账号。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse, RedirectResponse

from . import crud
from .database import get_db, init_db
from .schemas import (
    BatchCreate,
    BatchList,
    BatchReport,
    BatchReview,
    BatchSeal,
    HandoffCheckRequest,
    HandoffCheckResponse,
    ReviewCreate,
    ReviewEventOut,
    SealVerification,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> Iterator[None]:
    # 依赖与数据库初始化：目录创建 + 幂等建表。
    init_db()
    yield


app = FastAPI(
    title="冷链温度合规审计 API",
    version="1.3.0",
    description=(
        "提交按批次组织的 ISO 时间戳温度读数，服务端排序后使用滞回阈值"
        "识别连续超温事件，并将超过采样间隔上限的缺测切成独立片段；"
        "支持对审计报告按固定字段顺序生成 canonical JSON 并以 SHA-256"
        "封存摘要（seal），随后可重新计算摘要进行一致性校验（verify）；"
        "另支持批次人工复核工作流（pending → approved/rejected → "
        "reopen），复核事件追加留痕，不改动审计报告与封存数据；"
        "还可对按运输顺序排列的多个批次做运输交接连续性检查"
        "（POST /handoffs/check），报告相邻批次间的缺口、重叠与两侧"
        "末/首段类型，全程只读。"
    ),
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/batches",
    response_model=BatchReport,
    status_code=status.HTTP_201_CREATED,
    tags=["batches"],
    summary="提交一批温度读数并完成审计",
)
def submit_batch(
    payload: BatchCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    try:
        batch_id = crud.create_batch(conn, payload)
    except crud.DuplicateBatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"batch with external_ref already exists: {exc}",
        )

    report = crud.get_report(conn, batch_id)
    assert report is not None  # 刚写入，必然存在
    return report


@app.get(
    "/batches",
    response_model=BatchList,
    tags=["batches"],
    summary="列出批次摘要",
)
def list_batches(
    compliant: bool | None = Query(
        default=None, description="按是否合规过滤"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    items, total = crud.list_batches(
        conn, compliant=compliant, limit=limit, offset=offset
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.post(
    "/handoffs/check",
    response_model=HandoffCheckResponse,
    tags=["handoffs"],
    summary="运输交接连续性检查（按运输顺序校验相邻批次，只读）",
)
def check_handoffs(
    payload: HandoffCheckRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """逐对检查“前一批 end_time → 后一批 start_time”的交接连续性。

    读取各批次**已审计时间轴**，按给定运输顺序比较相邻批次：

    - 报告有符号 ``interval_seconds``，以及非负的 ``gap_seconds``（缺口）
      与 ``overlap_seconds``（重叠）；
    - 同时给出前一批**末段**与后一批**首段**的类型与 missing 标记；
    - 缺口**严格大于** ``allowed_gap_seconds``，或任一侧相接片段为
      ``missing`` 时标记 ``handoff_issue`` 并附 ``issue_reasons``；
    - ``route_compliant`` 当且仅当所有交接均无问题。

    全程只读，不改变审计报告、封存（seal/verify）与人工复核数据。
    列表不足两项或含重复批次返回 422；引用不存在的批次返回 404。
    """

    try:
        return crud.check_handoffs(
            conn,
            payload.batch_refs,
            allowed_gap_seconds=payload.allowed_gap_seconds,
        )
    except crud.BatchNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"batch not found: {exc}",
        )
    except crud.DuplicateHandoffBatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"batch_refs must not reference the same batch twice: {exc}",
        )


@app.get(
    "/batches/{batch_ref}/report",
    response_model=BatchReport,
    tags=["batches"],
    summary="查询批次审计报告（批次 ID 或业务批次号）",
)
def get_batch_report(
    batch_ref: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    report = crud.get_report(conn, batch_ref)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"batch not found: {batch_ref}",
        )
    return report


@app.post(
    "/batches/{batch_ref}/seal",
    response_model=BatchSeal,
    tags=["sealing"],
    summary="封存批次报告摘要（canonical JSON + SHA-256）",
)
def seal_batch(
    batch_ref: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    seal = crud.seal_report(conn, batch_ref)
    if seal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"batch not found: {batch_ref}",
        )
    return seal


@app.get(
    "/batches/{batch_ref}/verify",
    response_model=SealVerification,
    tags=["sealing"],
    summary="校验当前报告摘要与封存摘要是否一致（只读）",
)
def verify_batch(
    batch_ref: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    verification = crud.get_verification(conn, batch_ref)
    if verification is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"batch not found: {batch_ref}",
        )
    return verification


@app.post(
    "/batches/{batch_ref}/review",
    tags=["review"],
    summary="提交人工复核结论（approve/reject/reopen）",
    responses={
        200: {"description": "request_id 幂等重放，返回首次写入的事件"},
        201: {"description": "新复核事件已追加"},
        409: {"description": "状态流转不合法，或 request_id 内容冲突"},
    },
)
def review_batch(
    batch_ref: str,
    payload: ReviewCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    """追加一条人工复核事件。

    状态仅允许 ``pending → approved/rejected`` 及 ``rejected → pending``
    （reopen）；reject 必须携带 comment。每次操作向 review_events 追加
    留痕，**不改动审计报告或 seal/verify 数据**。``request_id`` 为幂等键：
    同批次重复提交且内容一致时返回原事件（200），内容不一致返回 409。
    """

    try:
        event, created = crud.submit_review(
            conn,
            batch_ref,
            reviewer=payload.reviewer,
            decision=payload.decision,
            comment=payload.comment,
            request_id=payload.request_id,
        )
    except crud.ReviewStateConflictError as exc:
        decision_value, current_status = exc.args
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"illegal review transition: decision={decision_value} "
                f"from status={current_status}"
            ),
        )
    except crud.ReviewRequestConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "request_id already used for this batch with a different "
                f"payload: {exc}"
            ),
        )

    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"batch not found: {batch_ref}",
        )
    return JSONResponse(
        content=ReviewEventOut.model_validate(event).model_dump(mode="json"),
        status_code=(
            status.HTTP_201_CREATED if created else status.HTTP_200_OK
        ),
    )


@app.get(
    "/batches/{batch_ref}/review",
    response_model=BatchReview,
    tags=["review"],
    summary="查询当前复核状态与按时间排序的复核历史（只读）",
)
def get_batch_review(
    batch_ref: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    review = crud.get_review(conn, batch_ref)
    if review is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"batch not found: {batch_ref}",
        )
    return review
