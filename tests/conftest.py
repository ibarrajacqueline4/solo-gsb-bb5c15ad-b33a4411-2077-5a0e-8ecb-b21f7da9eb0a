"""pytest 共享夹具：使用临时 SQLite 文件，互不影响开发库。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("COLDCHAIN_DB_PATH", str(db_path))

    # 必须在设置环境变量后再导入，保证配置生效。
    import app.config as config
    import app.database as database
    from app.main import app

    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(database, "DB_PATH", db_path)

    with TestClient(app) as test_client:
        yield test_client
