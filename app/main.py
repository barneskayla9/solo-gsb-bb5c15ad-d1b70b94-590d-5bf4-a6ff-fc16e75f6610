"""FastAPI 应用入口。

启动时自动建表并写入初始目录修订（幂等），因此 docker compose up
即可完成含数据库初始化的一键启动。
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .database import init_db
from .errors import register_exception_handlers
from .routers import catalog, plans

logger = logging.getLogger("pluginhub")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 容器编排中数据库就绪可能略晚于应用，简单重试即可（内网无需额外组件）
    last_exc: Exception | None = None
    for attempt in range(30):
        try:
            init_db()
            last_exc = None
            break
        except Exception as exc:  # pragma: no cover - 仅启动期网络重试
            last_exc = exc
            logger.warning("数据库尚未就绪（第 %s 次尝试）: %s", attempt + 1, exc)
            time.sleep(2)
    if last_exc is not None:
        raise last_exc
    yield


app = FastAPI(
    title="离线实验室插件包发布与安装方案服务",
    description=(
        "为无法联网的实验室提供插件版本发布（含 SHA-256、运行环境、依赖范围、互斥项校验）"
        "与可复现的完整安装方案求解。所有求解基于单调递增的目录修订，支持撤销与历史方案查询。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

register_exception_handlers(app)
app.include_router(catalog.router)
app.include_router(plans.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root():
    return {
        "service": "plugin-hub",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/health",
    }
