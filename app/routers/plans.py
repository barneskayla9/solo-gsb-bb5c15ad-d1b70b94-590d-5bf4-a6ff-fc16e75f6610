"""求解与历史方案接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from packaging.specifiers import SpecifierSet
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, service
from ..config import settings
from ..database import get_db
from ..errors import APIError
from ..schemas import SolveRequest
from ..solver import Dependency, solve

router = APIRouter(prefix="/api", tags=["plans"])


def _begin_snapshot_transaction(db: Session) -> None:
    """以 REPEATABLE READ 开启事务。

    这样一次求解中读取“目录修订 + 版本行 + 撤销标记”期间，
    即使有并发发布/撤销，本事务看到的目录快照也保持不变，
    一份方案绝不会混用不同目录修订。

    SQLite 不支持设置隔离级别（单文件库本身串行），跳过即可，
    便于本机/CI 测试；生产 PostgreSQL 必须生效。
    """
    if db.bind.dialect.name == "postgresql":
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})


@router.post("/solve")
def solve_plan(body: SolveRequest, db: Session = Depends(get_db)):
    # 规范化请求（合并同插件的多个范围）
    merged_plugins, clean_runtime = service.normalize_request(
        body.runtime, [p.model_dump() for p in body.plugins]
    )

    _begin_snapshot_transaction(db)

    # 确定目录修订
    latest = service.latest_revision_id(db)
    revision_id = body.revision if body.revision is not None else latest
    if service.get_revision(db, revision_id) is None:
        db.rollback()
        raise APIError(404, "revision_not_found", f"目录修订 {revision_id} 不存在")

    # 哈希按“解析后的修订”计算：隐式最新修订 R 与显式钉住 R 得到同一方案
    request_hash = service.request_hash(clean_runtime, merged_plugins, revision_id)

    snapshot = service.load_snapshot(db, revision_id)
    requirements = [
        Dependency(name=p["name"], spec=p["spec"], specset=SpecifierSet(p["spec"]))
        for p in merged_plugins
    ]
    result = solve(snapshot, clean_runtime, requirements, node_limit=settings.solver_node_limit)

    if result.kind == "unsat":
        # 明确要求：无解不保存部分方案
        db.rollback()
        raise APIError(
            409,
            "unsatisfiable",
            "不存在满足全部传递依赖、互斥与运行环境要求的安装方案",
            details=result.conflicts,
        )
    if result.kind == "cycle":
        db.rollback()
        raise APIError(
            409,
            "dependency_cycle",
            f"选定版本之间存在依赖环: {' -> '.join(result.cycle)}",
            details={"cycle": result.cycle},
        )

    # 成功：在同一快照事务内取行并落库完整方案。
    # 快照中的版本在该修订下均未撤销，故落库时 revoked 恒为 false；
    # 历史查询时再用当前撤销状态重新标记。
    rows = [db.get(models.PluginVersion, v.id) for v in result.selections]
    selections = [service.selection_dict(row, revoked=False) for row in rows]

    plan = models.Plan(
        request_hash=request_hash,
        catalog_revision_id=revision_id,
        request_runtime=clean_runtime,
        request_plugins=merged_plugins,
        selections=selections,
    )
    db.add(plan)
    created = True
    try:
        db.commit()
    except IntegrityError:
        # 同请求 + 同修订的并发求解：去重，返回已保存的同一份方案
        db.rollback()
        existing = db.execute(
            select(models.Plan).where(
                models.Plan.request_hash == request_hash,
                models.Plan.catalog_revision_id == revision_id,
            )
        ).scalar_one()
        return {
            "plan_id": existing.id,
            "catalog_revision": revision_id,
            "created": False,
            "request_hash": request_hash,
            "selections": existing.selections,
        }

    return {
        "plan_id": plan.id,
        "catalog_revision": revision_id,
        "created": created,
        "request_hash": request_hash,
        "selections": selections,
    }


@router.get("/plans")
def list_plans(limit: int = 100, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 1000))
    rows = db.execute(select(models.Plan).order_by(models.Plan.id.desc()).limit(limit)).scalars().all()
    current = service.version_map(db)
    return [service.plan_dict(p, current_rows=current) for p in rows]


@router.get("/plans/{plan_id}")
def get_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.get(models.Plan, plan_id)
    if plan is None:
        raise APIError(404, "version_not_found", f"方案 {plan_id} 不存在")
    # 历史方案仍可查；用当前撤销状态标记其中条目（selections 本身保留求解时内容）
    current = service.version_map(db)
    return service.plan_dict(plan, current_rows=current)
