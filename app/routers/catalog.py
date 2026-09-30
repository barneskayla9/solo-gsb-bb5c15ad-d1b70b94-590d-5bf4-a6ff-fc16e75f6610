"""目录相关接口：发布版本、撤销版本、查询、下载安装包。"""
from __future__ import annotations

import json
import os
import tempfile

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, packages, service
from ..config import settings
from ..database import get_db
from ..errors import APIError
from ..schemas import PublishMetadata, RevokeRequest

router = APIRouter(prefix="/api", tags=["catalog"])


@router.post("/plugins/{name}/versions", status_code=201)
async def publish_version(
    name: str,
    request: Request,
    metadata: str = Form(..., description="Pydantic 模型 PublishMetadata 的 JSON 字符串"),
    sha256: str = Form(..., description="安装包 SHA-256（小写十六进制）"),
    file: UploadFile = File(..., description="插件安装包"),
    db: Session = Depends(get_db),
):
    """发布插件版本。

    校验失败一律拒绝（400/409），不落任何数据：
    - 元数据/版本范围非法 -> 400 bad_metadata
    - SHA-256 与实际字节不符 -> 400 digest_mismatch
    - 同名同版本已存在 -> 409 version_already_exists（且不可改写）
    """
    # 1. 元数据校验（先于读文件，快速失败）
    try:
        raw_meta = json.loads(metadata)
    except json.JSONDecodeError as exc:
        raise APIError(400, "bad_metadata", f"metadata 不是合法 JSON: {exc}")
    if isinstance(raw_meta, dict) and raw_meta.get("name") and raw_meta["name"] != name:
        raise APIError(400, "bad_metadata", "路径中的插件名与 metadata.name 不一致")
    try:
        meta = PublishMetadata.model_validate(raw_meta)
    except ValidationError as exc:
        raise APIError(
            400,
            "bad_metadata",
            "发布元数据校验失败",
            details=json.loads(exc.json(include_url=False)),
        )
    if meta.name != name:
        raise APIError(400, "bad_metadata", "路径中的插件名与 metadata.name 不一致")

    sha = sha256.strip().lower()
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise APIError(400, "bad_metadata", "sha256 必须是 64 位小写十六进制")

    # 2. 快速预检同名同版本（唯一约束是最终防线）
    existing = db.execute(
        select(models.PluginVersion).where(
            models.PluginVersion.plugin_name == meta.name,
            models.PluginVersion.version == meta.version,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise APIError(
            409,
            "version_already_exists",
            f"{meta.name}@{meta.version} 已发布（修订 {existing.published_revision_id}），同名同版本不可改写",
        )

    # 3. 流式落临时文件并计算摘要、大小
    os.makedirs(settings.package_dir, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="upload-", dir=settings.package_dir)
    os.close(tmp_fd)
    import hashlib

    digest = hashlib.sha256()
    size = 0
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise APIError(
                        400,
                        "file_too_large",
                        f"安装包超过上限 {settings.max_upload_bytes} 字节",
                    )
                digest.update(chunk)
                out.write(chunk)
        actual = digest.hexdigest()
        if actual != sha:
            raise APIError(
                400,
                "digest_mismatch",
                f"摘要不符：提交 {sha}，实际 {actual}；发布已拒绝",
                details={"submitted": sha, "actual": actual},
            )

        # 4. 事务：追加修订 + 写入版本（行锁由唯一约束兜底并发发布）
        stored_filename = packages.safe_filename(meta.name, meta.version, file.filename or "package.bin")
        try:
            rev = service.append_revision(
                db,
                action="publish",
                plugin_name=meta.name,
                version=meta.version,
                detail=f"publish {meta.name}@{meta.version}",
            )
            row = models.PluginVersion(
                plugin_name=meta.name,
                version=meta.version,
                dependencies=[d.model_dump() for d in meta.dependencies],
                conflicts=[c.model_dump() for c in meta.conflicts],
                runtime_constraints=[r.model_dump(exclude_none=True) for r in meta.runtime_constraints],
                package_filename=stored_filename,
                package_sha256=sha,
                package_size=size,
                published_revision_id=rev.id,
            )
            db.add(row)
            db.commit()
        except IntegrityError:
            db.rollback()
            packages.discard_temp(tmp_path)
            raise APIError(
                409,
                "version_already_exists",
                f"{meta.name}@{meta.version} 已存在（并发发布冲突），同名同版本不可改写",
            )

        # 5. 提交成功后才将文件移动到最终位置
        packages.commit_blob(tmp_path, sha, stored_filename)
    except APIError:
        packages.discard_temp(tmp_path)
        raise
    except Exception:
        db.rollback()
        packages.discard_temp(tmp_path)
        raise

    return {
        "name": meta.name,
        "version": meta.version,
        "catalog_revision": rev.id,
        "package": {
            "filename": stored_filename,
            "sha256": sha,
            "size": size,
            "download_url": f"/api/plugins/{meta.name}/versions/{meta.version}/download",
        },
    }


@router.get("/revisions")
def list_revisions(limit: int = 100, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 1000))
    rows = db.execute(
        select(models.CatalogRevision).order_by(models.CatalogRevision.id.desc()).limit(limit)
    ).scalars().all()
    return [
        {
            "id": r.id,
            "action": r.action,
            "plugin_name": r.plugin_name,
            "version": r.version,
            "detail": r.detail,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/revisions/{revision_id}")
def get_revision(revision_id: int, db: Session = Depends(get_db)):
    rev = service.get_revision(db, revision_id)
    if rev is None:
        raise APIError(404, "revision_not_found", f"目录修订 {revision_id} 不存在")
    return {
        "id": rev.id,
        "action": rev.action,
        "plugin_name": rev.plugin_name,
        "version": rev.version,
        "detail": rev.detail,
        "created_at": rev.created_at.isoformat(),
    }


@router.get("/plugins")
def list_plugins(include_revoked: bool = False, db: Session = Depends(get_db)):
    cond = None if include_revoked else models.PluginVersion.revoked.is_(False)
    query = select(
        models.PluginVersion.plugin_name,
        func.count(models.PluginVersion.id),
    )
    if cond is not None:
        query = query.where(cond)
    rows = db.execute(query.group_by(models.PluginVersion.plugin_name)).all()
    return [{"name": name, "version_count": count} for name, count in sorted(rows)]


@router.get("/plugins/{name}/versions")
def list_versions(name: str, include_revoked: bool = False, db: Session = Depends(get_db)):
    query = select(models.PluginVersion).where(models.PluginVersion.plugin_name == name)
    if not include_revoked:
        query = query.where(models.PluginVersion.revoked.is_(False))
    rows = db.execute(query).scalars().all()
    rows = sorted(rows, key=lambda r: r.id)
    return [service.version_dict(r) for r in rows]


@router.get("/plugins/{name}/versions/{version}")
def get_version(name: str, version: str, db: Session = Depends(get_db)):
    row = db.execute(
        select(models.PluginVersion).where(
            models.PluginVersion.plugin_name == name,
            models.PluginVersion.version == version,
        )
    ).scalar_one_or_none()
    if row is None:
        raise APIError(404, "version_not_found", f"{name}@{version} 不存在")
    return service.version_dict(row)


@router.post("/plugins/{name}/versions/{version}/revoke", status_code=200)
def revoke_version(name: str, version: str, body: RevokeRequest | None = None, db: Session = Depends(get_db)):
    """撤销版本：只影响后续求解（生成新目录修订），历史方案保留并可查询。"""
    row = db.execute(
        select(models.PluginVersion).where(
            models.PluginVersion.plugin_name == name,
            models.PluginVersion.version == version,
        )
    ).scalar_one_or_none()
    if row is None:
        raise APIError(404, "version_not_found", f"{name}@{version} 不存在")
    if row.revoked:
        raise APIError(
            409,
            "already_revoked",
            f"{name}@{version} 已在修订 {row.revoked_revision_id} 被撤销",
        )

    rev = service.append_revision(
        db,
        action="revoke",
        plugin_name=name,
        version=version,
        detail=body.reason if body else f"revoke {name}@{version}",
    )
    row.revoked = True
    row.revoked_revision_id = rev.id
    row.revoked_reason = body.reason if body else None
    db.commit()
    return {
        "name": name,
        "version": version,
        "catalog_revision": rev.id,
        "revoked": True,
        "reason": row.revoked_reason,
    }


@router.get("/plugins/{name}/versions/{version}/download")
def download(name: str, version: str, db: Session = Depends(get_db)):
    row = db.execute(
        select(models.PluginVersion).where(
            models.PluginVersion.plugin_name == name,
            models.PluginVersion.version == version,
        )
    ).scalar_one_or_none()
    if row is None:
        raise APIError(404, "version_not_found", f"{name}@{version} 不存在")
    path = packages.blob_path(row.package_sha256, row.package_filename)
    if not os.path.exists(path):
        raise APIError(404, "version_not_found", "安装包文件缺失，请联系管理员检查存储卷")
    return FileResponse(
        path,
        filename=row.package_filename,
        headers={"X-Checksum-Sha256": row.package_sha256},
        media_type="application/octet-stream",
    )
