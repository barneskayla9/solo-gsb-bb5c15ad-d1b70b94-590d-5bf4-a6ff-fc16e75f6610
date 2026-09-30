#!/usr/bin/env python3
"""离线实验室插件服务完整调用示例（仅使用 Python 标准库）。

用法：
    python examples/demo.py                 # 默认 http://127.0.0.1:8000
    BASE_URL=http://host:8000 python examples/demo.py

演示流程覆盖：
    1. 发布基础库 / 应用（带传递依赖、运行环境、互斥项）
    2. 摘要不符被拒绝
    3. 同名同版本不可改写
    4. 求解完整安装方案
    5. 撤销版本只影响后续求解、历史方案仍可查并标记已撤销
    6. 钉住历史目录修订得到相同方案
    7. 无解返回冲突插件与约束
    8. 下载安装包并校验 SHA-256
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")


# ---------- 极简 multipart/form-data 构造（零依赖） ----------


def _http(method: str, path: str, *, data=None, headers=None):
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read()
            return resp.status, json.loads(body) if body else None, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body), dict(exc.headers)
        except json.JSONDecodeError:
            return exc.code, {"raw": body.decode("utf-8", "replace")}, dict(exc.headers)


def publish(name: str, version: str, *, deps=None, conflicts=None, runtime=None,
            payload: bytes | None = None, declared_sha256: str | None = None):
    payload = payload if payload is not None else f"package-of-{name}-{version}".encode()
    sha = hashlib.sha256(payload).hexdigest()
    metadata = json.dumps(
        {
            "name": name,
            "version": version,
            "dependencies": deps or [],
            "conflicts": conflicts or [],
            "runtime_constraints": runtime or [],
        }
    ).encode()

    boundary = "----pluginhubdemo" + uuid.uuid4().hex
    parts: list[bytes] = []
    for field, value in (("metadata", metadata), ("sha256", (declared_sha256 or sha).encode())):
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{field}"\r\n\r\n'.encode()
        )
        parts.append(value + b"\r\n")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{name}-{version}.pkg"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    parts.append(payload + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    return _http(
        "POST",
        f"/api/plugins/{name}/versions",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )


def solve(plugins, runtime=None, revision=None):
    payload = {"plugins": plugins, "runtime": runtime or {}}
    if revision is not None:
        payload["revision"] = revision
    return _http("POST", "/api/solve", data=json.dumps(payload).encode(),
                 headers={"Content-Type": "application/json"})


def show(title, status, body):
    print(f"\n=== {title} -> HTTP {status} ===")
    print(json.dumps(body, ensure_ascii=False, indent=2)[:2000])


def main() -> int:
    print(f"目标服务: {BASE_URL}")
    status, health, _ = _http("GET", "/health")
    if status != 200:
        print("服务不可达，请先启动：docker compose up -d")
        return 1
    print("健康检查通过:", health)

    # 1. 发布基础库
    show("发布 labcore 1.2.0", *publish(
        "labcore", "1.2.0",
        runtime=[{"name": "python", "spec": ">=3.9"}, {"name": "os", "equals": "linux"}],
    )[:2])
    show("发布 labcore 1.5.0", *publish(
        "labcore", "1.5.0",
        runtime=[{"name": "python", "spec": ">=3.10"}, {"name": "os", "equals": "linux"}],
    )[:2])

    # 2. 发布应用，声明传递依赖与互斥项
    show("发布 analyzer 3.0.0（依赖 labcore>=1.3,<2，互斥 oldreport）", *publish(
        "analyzer", "3.0.0",
        deps=[{"name": "labcore", "spec": ">=1.3,<2"}],
        conflicts=[{"name": "oldreport", "spec": ""}],
        runtime=[{"name": "python", "spec": ">=3.10"}],
    )[:2])

    # 3. 摘要不符必须拒绝
    show("错误演示：SHA-256 不符（应被拒绝）", *publish(
        "analyzer", "9.9.9", declared_sha256="0" * 64, payload=b"hello"
    )[:2])

    # 4. 同名同版本不可改写
    show("错误演示：重复发布 labcore 1.2.0（409，不可改写）",
         *publish("labcore", "1.2.0", payload=b"tampered")[:2])

    # 5. 求解
    status, plan, _ = solve(
        [{"name": "analyzer", "spec": ">=3,<4"}],
        runtime={"python": "3.11.5", "os": "linux"},
    )
    show("求解 analyzer 完整安装方案", status, plan)
    if status != 200:
        return 1
    revision = plan["catalog_revision"]
    plan_id = plan["plan_id"]

    # 6. 相同请求 + 相同目录修订 => 相同方案（幂等）
    status, plan2, _ = solve(
        [{"name": "analyzer", "spec": ">=3,<4"}],
        runtime={"python": "3.11.5", "os": "linux"},
    )
    print(f"\n幂等校验: 首次 plan_id={plan_id}, 再次 plan_id={plan2['plan_id']}, "
          f"created={plan2['created']}（created=false 表示复用同一方案）")
    assert plan2["plan_id"] == plan_id

    # 7. 无解演示：互斥项不得共存
    publish("oldreport", "1.0.0")
    status, unsat, _ = solve(
        [{"name": "analyzer", "spec": ""}, {"name": "oldreport", "spec": ""}],
        runtime={"python": "3.11.5", "os": "linux"},
    )
    show("错误演示：analyzer 与 oldreport 互斥（409 + 冲突明细，不保存方案）", status, unsat)

    # 8. 撤销 labcore 1.5.0：只影响后续求解
    show("撤销 labcore 1.5.0", *_http(
        "POST", "/api/plugins/labcore/versions/1.5.0/revoke",
        data=json.dumps({"reason": "演示撤销"}).encode(),
        headers={"Content-Type": "application/json"},
    )[:2])

    status, after_revoke, _ = solve(
        [{"name": "analyzer", "spec": ""}],
        runtime={"python": "3.11.5", "os": "linux"},
    )
    show("撤销后重新求解：labcore 1.5.0 不可见，1.2.0 不满足 >=1.3 -> 无解",
         status, after_revoke)

    # 9. 历史方案仍可查，并标记已撤销项
    status, history, _ = _http("GET", f"/api/plans/{plan_id}")
    show(f"历史方案 {plan_id} 仍可查（selections 中 labcore 标记 revoked）", status, history)

    # 10. 钉住旧修订，仍得到相同方案
    status, pinned, _ = solve(
        [{"name": "analyzer", "spec": ">=3,<4"}],
        runtime={"python": "3.11.5", "os": "linux"},
        revision=revision,
    )
    show(f"钉住修订 {revision} 重新求解（应复用同一方案）", status, pinned)
    assert pinned["plan_id"] == plan_id, "隐式最新修订与显式钉住同一修订应得到同一方案"
    assert pinned["created"] is False

    # 11. 下载并校验
    status, meta, _ = _http("GET", "/api/plugins/analyzer/versions/3.0.0")
    url = meta["package"]["download_url"]
    with urllib.request.urlopen(BASE_URL + url) as resp:
        status = resp.status
        blob = resp.read()
    digest = hashlib.sha256(blob).hexdigest()
    print(f"\n下载 {url}: HTTP {status}, {len(blob)} 字节, "
          f"SHA-256={digest[:16]}... 与元数据一致: {digest == meta['package']['sha256']}")

    print("\n演示完成。交互式文档: " + BASE_URL + "/docs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
