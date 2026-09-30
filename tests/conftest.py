"""pytest 夹具：用 SQLite 内存库跑 API 集成测试（CI/本机免数据库）。"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
_tmp_pkg = tempfile.mkdtemp(prefix="pkgtest-")
os.environ.setdefault("PACKAGE_DIR", _tmp_pkg)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import database


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    # 替换全局 engine / SessionLocal
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", TestSession)
    database.init_db()

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def publish(client):
    """返回发布辅助函数。"""
    import hashlib

    def _publish(name, version, *, deps=None, conflicts=None, runtime=None, content=None, sha=None):
        if content is None:
            content = f"{name}-{version}-payload".encode()
        elif isinstance(content, str):
            content = content.encode()
        actual_sha = hashlib.sha256(content).hexdigest()
        import json as _json

        meta = _json.dumps(
            {
                "name": name,
                "version": version,
                "dependencies": deps or [],
                "conflicts": conflicts or [],
                "runtime_constraints": runtime or [],
            }
        )
        return client.post(
            f"/api/plugins/{name}/versions",
            data={"metadata": meta, "sha256": sha or actual_sha},
            files={"file": (f"{name}-{version}.pkg", content, "application/octet-stream")},
        )

    return _publish
