"""安装包文件落盘存储。

离线实验室只需要文件系统 + 一个持久卷，不依赖对象存储。
路径不使用用户提供的文件名，避免路径穿越；文件以 sha256 寻址，
同一内容天然去重，同名同版本也无法改写（数据库唯一约束另行保证）。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil

from .config import settings

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, version: str, original: str) -> str:
    """保留原始扩展名，主体由插件名与版本构成，防止穿越路径。"""
    ext = os.path.splitext(original)[1][:16]
    ext = _SAFE_NAME_RE.sub("_", ext)
    stem = f"{_SAFE_NAME_RE.sub('_', name)}-{_SAFE_NAME_RE.sub('_', version)}"
    return f"{stem}{ext}" if ext else stem


def blob_path(sha256: str, filename: str) -> str:
    # 两级散列分桶，单目录文件数量可控
    return os.path.join(settings.package_dir, sha256[:2], sha256[2:4], f"{sha256}__{filename}")


def store_temp_file(path: str) -> str:
    os.makedirs(settings.package_dir, exist_ok=True)
    safe = os.path.join(settings.package_dir, f".tmp-{os.path.basename(path)}")
    shutil.move(path, safe)
    return safe


def commit_blob(temp_path: str, sha256: str, filename: str) -> str:
    """事务提交成功后调用：将临时文件原子重命名到最终位置。"""
    dest = blob_path(sha256, filename)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        os.remove(temp_path)
        return dest
    os.replace(temp_path, dest)
    return dest


def discard_temp(temp_path: str | None) -> None:
    if temp_path and os.path.exists(temp_path):
        os.remove(temp_path)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
