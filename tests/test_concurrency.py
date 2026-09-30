"""并发场景测试：同名同版本并发发布只能成功一次。

SQLite 内存库 + StaticPool 是单连接、线程不安全，无法验证并发；
这里改用文件型 SQLite（QueuePool 每线程独立连接，写锁串行化），
生产 PostgreSQL 下由 uq_plugin_version 唯一约束提供相同保证。
"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import database


@pytest.fixture()
def file_client(client, tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'race.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", session_factory)
    database.init_db()
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_concurrent_publish_only_one_wins(file_client):
    content = b"race-payload"
    sha = hashlib.sha256(content).hexdigest()
    meta = json.dumps(
        {"name": "race", "version": "1.0.0", "dependencies": [], "conflicts": [], "runtime_constraints": []}
    )

    def one(_):
        return file_client.post(
            "/api/plugins/race/versions",
            data={"metadata": meta, "sha256": sha},
            files={"file": ("race-1.0.0.pkg", content, "application/octet-stream")},
        ).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(one, range(6)))

    assert codes.count(201) == 1, codes
    assert all(c in (201, 409) for c in codes), codes

    versions = file_client.get("/api/plugins/race/versions").json()
    assert len(versions) == 1
