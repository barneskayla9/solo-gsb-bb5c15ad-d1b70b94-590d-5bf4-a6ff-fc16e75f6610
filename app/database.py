"""数据库引擎、会话与建表初始化。

建表由应用启动时执行（幂等），配合 Compose 即可完成“数据库初始化”，
无需额外迁移组件，也适合无法联网的内网环境。
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    url = settings.database_url
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    # 必须先 import 模型，元数据中才注册表定义
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    # 写入编号 0 的初始目录修订
    with SessionLocal() as session:
        if session.get(models.CatalogRevision, 0) is None:
            session.add(models.CatalogRevision(id=0, action="init", detail="initial empty catalog"))
            session.commit()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个会话。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
