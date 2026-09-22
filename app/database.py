"""SQLite 持久化。

使用标准库 sqlite3：
- 启动时建表（应用 lifespan 中调用 init_db）；
- 开启 WAL 与外键约束；
- 每个请求一个连接（行工厂为 sqlite3.Row）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id                      TEXT PRIMARY KEY,
    external_ref            TEXT NOT NULL UNIQUE,
    submitted_at            TEXT NOT NULL,
    start_time              TEXT NOT NULL,
    end_time                TEXT NOT NULL,
    low_limit_c             REAL NOT NULL,
    high_limit_c            REAL NOT NULL,
    hysteresis_c            REAL NOT NULL,
    max_interval_seconds    REAL NOT NULL,
    compliant               INTEGER NOT NULL,
    reading_count           INTEGER NOT NULL,
    high_excursion_count    INTEGER NOT NULL,
    low_excursion_count     INTEGER NOT NULL,
    missing_segment_count   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    idx             INTEGER NOT NULL,
    timestamp       TEXT NOT NULL,
    temperature_c   REAL NOT NULL,
    UNIQUE(batch_id, idx)
);

CREATE TABLE IF NOT EXISTS segments (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id                TEXT NOT NULL REFERENCES batches(id)
                            ON DELETE CASCADE,
    idx                     INTEGER NOT NULL,
    type                    TEXT NOT NULL,
    start_time              TEXT NOT NULL,
    end_time                TEXT NOT NULL,
    duration_seconds        REAL NOT NULL,
    sample_count            INTEGER NOT NULL,
    peak_temperature_c      REAL,
    peak_at                 TEXT,
    max_temperature_c       REAL,
    min_temperature_c       REAL,
    missing                 INTEGER NOT NULL,
    compliant               INTEGER NOT NULL,
    UNIQUE(batch_id, idx)
);

CREATE TABLE IF NOT EXISTS batch_seals (
    batch_id        TEXT PRIMARY KEY REFERENCES batches(id) ON DELETE CASCADE,
    sealed_at       TEXT NOT NULL,
    canonical_json  TEXT NOT NULL,
    digest          TEXT NOT NULL,
    digest_algorithm TEXT NOT NULL DEFAULT 'sha256'
);

CREATE TABLE IF NOT EXISTS review_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    decision    TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status   TEXT NOT NULL,
    reviewer    TEXT NOT NULL,
    comment     TEXT,
    request_id  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE(batch_id, seq),
    UNIQUE(batch_id, request_id)
);

CREATE INDEX IF NOT EXISTS idx_readings_batch_time
    ON readings(batch_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_segments_batch
    ON segments(batch_id, idx);
CREATE INDEX IF NOT EXISTS idx_review_events_batch
    ON review_events(batch_id, seq);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        DB_PATH, check_same_thread=False, timeout=30.0
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_db() -> None:
    """创建数据目录与全部表（幂等）。"""

    with _connect() as connection:
        connection.executescript(_SCHEMA)


@contextmanager
def get_db_ctx() -> Iterator[sqlite3.Connection]:
    connection = _connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：请求级连接。"""

    with get_db_ctx() as connection:
        yield connection
