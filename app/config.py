"""运行配置：全部通过环境变量覆盖，开箱即用，无需任何第三方账号。"""

from __future__ import annotations

import os
from pathlib import Path


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    return float(value)


DB_PATH = Path(_env_str("COLDCHAIN_DB_PATH", "/data/coldchain.db"))
DEFAULT_HYSTERESIS_C = _env_float("COLDCHAIN_DEFAULT_HYSTERESIS", 0.5)
DEFAULT_MAX_INTERVAL_SECONDS = _env_float(
    "COLDCHAIN_DEFAULT_MAX_INTERVAL", 900.0  # 15 分钟
)
MAX_READINGS_PER_BATCH = int(
    _env_str("COLDCHAIN_MAX_READINGS", "100000")
)
