"""Pydantic 请求/响应模型与输入校验。

发布时的强校验在这里完成：
- 版本必须是 PEP 440；
- 依赖版本范围、运行环境版本范围必须是合法 PEP 440 SpecifierSet；
- 名称仅允许字母数字及 ._-，防止路径穿越；
- 兼容运行环境项必须二选一提供 spec 或 equals。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, Field, field_validator, model_validator
from packaging.specifiers import InvalidSpecifier, SpecifierSet

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_name(value: str) -> str:
    if not NAME_RE.match(value):
        raise ValueError(
            f"名称 {value!r} 非法：仅允许字母/数字开头，其余可含字母数字、点、下划线、连字符（最长 128）"
        )
    return value


def validate_version(value: str) -> str:
    try:
        Version(value)
    except InvalidVersion as exc:
        raise ValueError(f"版本 {value!r} 不是合法的 PEP 440 版本: {exc}") from exc
    return value


def validate_spec(value: str) -> str:
    """规范化版本范围："*"/空串表示“任意版本”（空 SpecifierSet）。"""
    value = value.strip()
    if value in ("", "*"):
        return ""
    try:
        return str(SpecifierSet(value))
    except InvalidSpecifier as exc:
        raise ValueError(f"版本范围 {value!r} 非法: {exc}") from exc


class DependencyIn(BaseModel):
    name: str
    spec: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return validate_name(v)

    @field_validator("spec", mode="before")
    @classmethod
    def _spec(cls, v: Any) -> str:
        return validate_spec(str(v))


class ConflictIn(BaseModel):
    name: str
    spec: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return validate_name(v)

    @field_validator("spec", mode="before")
    @classmethod
    def _spec(cls, v: Any) -> str:
        return validate_spec(str(v))


class RuntimeConstraintIn(BaseModel):
    name: str
    spec: str | None = None
    equals: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return validate_name(v)

    @field_validator("spec", mode="before")
    @classmethod
    def _spec(cls, v: Any) -> str | None:
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        return validate_spec(str(v))

    @model_validator(mode="after")
    def _exactly_one(self) -> "RuntimeConstraintIn":
        if (self.spec is None) == (self.equals is None):
            raise ValueError("运行环境约束必须且只能提供 spec（版本范围）或 equals（精确匹配）之一")
        return self


class PublishMetadata(BaseModel):
    name: str
    version: str
    runtime_constraints: list[RuntimeConstraintIn] = Field(default_factory=list)
    dependencies: list[DependencyIn] = Field(default_factory=list)
    conflicts: list[ConflictIn] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return validate_name(v)

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        return validate_version(v)

    @model_validator(mode="after")
    def _no_duplicate_dependencies(self) -> "PublishMetadata":
        dep_names = [d.name for d in self.dependencies]
        if len(set(dep_names)) != len(dep_names):
            raise ValueError("同一依赖出现了多次，请合并为一个版本范围")
        conflict_names = [c.name for c in self.conflicts]
        if len(set(conflict_names)) != len(conflict_names):
            raise ValueError("同一互斥插件出现了多次")
        return self


class RequestPluginIn(BaseModel):
    name: str
    spec: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return validate_name(v)

    @field_validator("spec", mode="before")
    @classmethod
    def _spec(cls, v: Any) -> str:
        return validate_spec(str(v))


class SolveRequest(BaseModel):
    # 运行环境，如 {"python": "3.11.5", "cuda": "12.2", "os": "linux"}
    runtime: dict[str, str] = Field(default_factory=dict)
    # 目标插件及版本范围
    plugins: list[RequestPluginIn] = Field(min_length=1)
    # 可选：钉住历史目录修订；缺省为最新修订
    revision: int | None = Field(default=None, ge=0)


class RevokeRequest(BaseModel):
    reason: str | None = None


# ---------- 响应模型（保持宽松 dict 序列化，这里仅作文档用途） ----------


class RevisionOut(BaseModel):
    id: int
    action: str
    plugin_name: str | None
    version: str | None
    detail: str | None
    created_at: datetime


class PackageOut(BaseModel):
    filename: str
    sha256: str
    size: int
    download_url: str


class VersionOut(BaseModel):
    name: str
    version: str
    dependencies: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    runtime_constraints: list[dict[str, Any]]
    package: PackageOut
    published_revision: int
    created_at: datetime
    revoked: bool
    revoked_revision: int | None
    revoked_reason: str | None


class SelectionOut(BaseModel):
    name: str
    version: str
    dependencies: list[dict[str, Any]]
    package: PackageOut
    # 该版本在求解之后是否已被撤销（仅历史方案查询时可能为 True）
    revoked: bool


class SolveResponse(BaseModel):
    plan_id: int
    catalog_revision: int
    created: bool
    request_hash: str
    selections: list[dict[str, Any]]


class PlanOut(BaseModel):
    id: int
    catalog_revision: int
    request_runtime: dict[str, str]
    request_plugins: list[dict[str, Any]]
    selections: list[dict[str, Any]]
    created_at: datetime
    has_revoked_items: bool


class ErrorResponse(BaseModel):
    error: Literal[
        "digest_mismatch",
        "version_already_exists",
        "version_not_found",
        "revision_not_found",
        "already_revoked",
        "unsatisfiable",
        "dependency_cycle",
        "file_too_large",
        "bad_metadata",
    ]
    message: str
    details: Any = None
