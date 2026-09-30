"""应用配置：全部通过环境变量注入，便于离线环境与容器编排。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    host = os.environ.get("POSTGRES_HOST", "db")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "pluginhub")
    password = os.environ.get("POSTGRES_PASSWORD", "pluginhub")
    db = os.environ.get("POSTGRES_DB", "pluginhub")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}"


@dataclass(frozen=True)
class Settings:
    database_url: str = _database_url()
    # 安装包落盘目录（容器内挂载卷）
    package_dir: str = os.environ.get("PACKAGE_DIR", "/data/packages")
    # 上传安装包大小上限（字节），默认 1 GiB
    max_upload_bytes: int = int(os.environ.get("MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
    # 单次求解回溯节点上限，防止病态请求占用 CPU
    solver_node_limit: int = int(os.environ.get("SOLVER_NODE_LIMIT", "200000"))


settings = Settings()
