"""数据库模型。

核心概念：
- CatalogRevision：目录修订（单调递增）。发布、撤销都会追加新修订；
  每次求解在一个事务内读取某一修订下的全部版本快照，绝不混用修订。
- PluginVersion：某个插件的一个已发布版本；不可物理改写，撤销只置标记。
- Plan：求解成功后保存的完整安装方案（无冲突、无环），失败不落库。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

# PostgreSQL 用 BIGINT，SQLite（测试）必须是 INTEGER 主键才会自动 rowid 自增
BigIntPK = BigInteger().with_variant(Integer, "sqlite")
BigIntFK = BigInteger().with_variant(Integer, "sqlite")


class CatalogRevision(Base):
    __tablename__ = "catalog_revisions"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # publish | revoke | init
    plugin_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    versions: Mapped[list["PluginVersion"]] = relationship(
        back_populates="revision", foreign_keys="PluginVersion.published_revision_id"
    )


class PluginVersion(Base):
    __tablename__ = "plugin_versions"
    __table_args__ = (
        UniqueConstraint("plugin_name", "version", name="uq_plugin_version"),
        Index("ix_versions_revoked", "revoked"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    plugin_name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(128), nullable=False)

    # 依赖：[{"name": "...", "spec": ">=1.0,<2"}]
    dependencies: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 互斥项：[{"name": "...", "spec": ">=..."}]，spec 为 "*" 表示与该插件任意版本互斥
    conflicts: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 兼容运行环境：[{"name": "python", "spec": ">=3.9"} 或 {"name": "os", "equals": "linux"}]
    runtime_constraints: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    package_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    package_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    package_size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    published_revision_id: Mapped[int] = mapped_column(
        BigIntFK, ForeignKey("catalog_revisions.id"), nullable=False
    )
    revoked_revision_id: Mapped[int | None] = mapped_column(
        BigIntFK, ForeignKey("catalog_revisions.id"), nullable=True
    )
    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    revision: Mapped[CatalogRevision] = relationship(
        foreign_keys=[published_revision_id], back_populates="versions"
    )


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (
        UniqueConstraint("request_hash", "catalog_revision_id", name="uq_plan_request_revision"),
        Index("ix_plans_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_revision_id: Mapped[int] = mapped_column(
        BigIntFK, ForeignKey("catalog_revisions.id"), nullable=False
    )

    # 原始请求
    request_runtime: Mapped[dict] = mapped_column(JSON, nullable=False)
    request_plugins: Mapped[list] = mapped_column(JSON, nullable=False)

    # 求解结果：完整方案，[{"name","version","dependencies":[...], "package":{...}}]
    selections: Mapped[list] = mapped_column(JSON, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
