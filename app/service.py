"""服务层：目录修订、快照构建、序列化、请求规范化。"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from packaging.specifiers import SpecifierSet
from packaging.version import Version
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .solver import (
    CatalogSnapshot,
    ConflictRule,
    Dependency,
    RuntimeConstraint,
    VersionInfo,
)


# ---------- 目录修订 ----------


def latest_revision_id(db: Session) -> int:
    row = db.execute(
        select(models.CatalogRevision.id).order_by(models.CatalogRevision.id.desc()).limit(1)
    ).scalar_one()
    return int(row)


def get_revision(db: Session, revision_id: int) -> models.CatalogRevision | None:
    return db.get(models.CatalogRevision, revision_id)


def append_revision(db: Session, action: str, detail: str, plugin_name=None, version=None) -> CatalogRevision:
    rev = models.CatalogRevision(
        action=action, detail=detail, plugin_name=plugin_name, version=version
    )
    db.add(rev)
    db.flush()  # 拿到自增 id
    return rev


# ---------- 快照 ----------


def load_snapshot(db: Session, revision_id: int) -> CatalogSnapshot:
    """加载某修订下所有可见版本：发布修订 <= revision 且未撤销（或撤销修订 > revision）。

    注意：发布/撤销在同一事务里先落版本/标记、再追加修订并一起提交，
    因此 revision_id 与版本可见性在提交后始终保持一致。
    """
    rows = db.execute(
        select(models.PluginVersion).where(models.PluginVersion.published_revision_id <= revision_id)
    ).scalars().all()

    grouped: dict[str, list[VersionInfo]] = defaultdict(list)
    for row in rows:
        revoked_at = row.revoked_revision_id
        if row.revoked and revoked_at is not None and revoked_at <= revision_id:
            continue
        grouped[row.plugin_name].append(_to_version_info(row))

    for versions in grouped.values():
        versions.sort(key=lambda v: v.pv, reverse=True)

    return CatalogSnapshot(revision_id=revision_id, versions=dict(grouped))


def _to_version_info(row: models.PluginVersion) -> VersionInfo:
    deps = tuple(
        Dependency(name=d["name"], spec=d["spec"], specset=SpecifierSet(d["spec"]))
        for d in (row.dependencies or [])
    )
    conflicts = tuple(
        ConflictRule(name=c["name"], spec=c["spec"], specset=SpecifierSet(c["spec"]))
        for c in (row.conflicts or [])
    )
    runtimes = tuple(
        RuntimeConstraint(name=r["name"], spec=r.get("spec"), equals=r.get("equals"))
        for r in (row.runtime_constraints or [])
    )
    return VersionInfo(
        id=row.id,
        name=row.plugin_name,
        version=row.version,
        pv=Version(row.version),
        dependencies=deps,
        conflicts=conflicts,
        runtime_constraints=runtimes,
    )


def version_map(db: Session, revision_id: int | None = None) -> dict[tuple[str, str], models.PluginVersion]:
    """当前（或指定修订下）可见版本索引。指定修订时撤销版本不可见。"""
    if revision_id is None:
        rows = db.execute(
            select(models.PluginVersion).where(models.PluginVersion.revoked.is_(False))
        ).scalars().all()
        return {(r.plugin_name, r.version): r for r in rows}
    snap = load_snapshot(db, revision_id)
    out: dict[tuple[str, str], models.PluginVersion] = {}
    for versions in snap.versions.values():
        for v in versions:
            row = db.get(models.PluginVersion, v.id)
            out[(v.name, v.version)] = row
    return out


# ---------- 请求规范化与哈希 ----------


def normalize_request(runtime: dict[str, str], plugins: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """规范化请求：合并同插件版本范围、排序，不涉及目录修订。"""
    clean_plugins = _merge_plugin_specs(plugins)
    clean_runtime = {str(k): str(v) for k, v in runtime.items()}
    return clean_plugins, clean_runtime


def request_hash(clean_runtime: dict[str, str], clean_plugins: list[dict], resolved_revision: int) -> str:
    """按“解析后的目录修订”计算哈希。

    隐式使用最新修订 R 与显式钉住 R 语义等价，必须命中同一条方案；
    因此哈希只与规范化请求和最终解析出的修订有关。
    """
    payload = {
        "runtime": dict(sorted(clean_runtime.items())),
        "plugins": sorted(clean_plugins, key=lambda p: p["name"]),
        "revision": resolved_revision,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _merge_plugin_specs(plugins: list[dict]) -> list[dict]:
    """同一插件出现多次时取范围交集，交集为空直接让求解器报无解。"""
    merged: dict[str, SpecifierSet] = {}
    for p in plugins:
        spec = SpecifierSet(p["spec"])
        if p["name"] in merged:
            merged[p["name"]] &= spec
        else:
            merged[p["name"]] = spec
    return [{"name": name, "spec": str(spec)} for name, spec in sorted(merged.items())]


# ---------- 序列化 ----------


def package_dict(row: models.PluginVersion) -> dict:
    return {
        "filename": row.package_filename,
        "sha256": row.package_sha256,
        "size": row.package_size,
        "download_url": f"/api/plugins/{row.plugin_name}/versions/{row.version}/download",
    }


def version_dict(row: models.PluginVersion) -> dict:
    return {
        "name": row.plugin_name,
        "version": row.version,
        "dependencies": row.dependencies or [],
        "conflicts": row.conflicts or [],
        "runtime_constraints": row.runtime_constraints or [],
        "package": package_dict(row),
        "published_revision": row.published_revision_id,
        "created_at": row.created_at.isoformat(),
        "revoked": row.revoked,
        "revoked_revision": row.revoked_revision_id,
        "revoked_reason": row.revoked_reason,
    }


def selection_dict(row: models.PluginVersion, revoked: bool | None = None) -> dict:
    """方案条目序列化。revoked 给定时以调用方判定为准（求解快照内恒为 false）。"""
    return {
        "name": row.plugin_name,
        "version": row.version,
        "dependencies": row.dependencies or [],
        "package": package_dict(row),
        "revoked": row.revoked if revoked is None else revoked,
    }


def plan_dict(plan: models.Plan, current_rows: dict[tuple[str, str], models.PluginVersion] | None = None) -> dict:
    """历史方案序列化；current_rows 给定时，用“当前是否已撤销”覆盖标记。"""
    selections = plan.selections
    if current_rows is not None:
        selections = []
        for item in plan.selections:
            row = current_rows.get((item["name"], item["version"]))
            item = dict(item)
            if row is not None:
                item["revoked"] = row.revoked
            else:
                item["revoked"] = True
            selections.append(item)
    has_revoked = any(item.get("revoked") for item in selections)
    return {
        "id": plan.id,
        "catalog_revision": plan.catalog_revision_id,
        "request_runtime": plan.request_runtime,
        "request_plugins": plan.request_plugins,
        "selections": selections,
        "created_at": plan.created_at.isoformat(),
        "has_revoked_items": has_revoked,
    }
