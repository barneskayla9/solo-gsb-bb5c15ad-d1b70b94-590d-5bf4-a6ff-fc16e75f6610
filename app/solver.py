"""依赖求解核心（纯 Python，不接触数据库与 HTTP）。

求解目标
========
给定运行环境、目标插件版本范围以及某一“目录修订”下全部未撤销版本，
选出每个插件恰好一个版本，使得：

1. 所有插件（含传递依赖）版本满足其全部被要求的版本范围（取交集）；
2. 每个被选版本都与给定运行环境兼容；
3. 互斥项不得共存（双向检查）；
4. 依赖图无环；若求得完整方案却存在环，明确报错；
5. 无方案时返回冲突插件与约束，调用方不得保存部分方案。

确定性
======
候选版本按 PEP 440 版本号降序；待选插件按“候选数最少（MRV）”、
再按插件名排序。相同输入必然得到相同方案。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from packaging.specifiers import SpecifierSet
from packaging.version import Version


@dataclass(frozen=True)
class Dependency:
    name: str
    spec: str
    specset: SpecifierSet
    # 谁要求的："request" 表示用户请求，其余为 "插件@版本"
    origin: str = "request"

    @property
    def origin_label(self) -> str:
        return "请求" if self.origin == "request" else self.origin


@dataclass(frozen=True)
class RuntimeConstraint:
    name: str
    spec: str | None = None
    equals: str | None = None

    @property
    def specset(self) -> SpecifierSet | None:
        return SpecifierSet(self.spec) if self.spec else None


@dataclass(frozen=True)
class ConflictRule:
    name: str
    spec: str
    specset: SpecifierSet


@dataclass
class VersionInfo:
    id: int
    name: str
    version: str
    pv: Version
    dependencies: tuple[Dependency, ...]
    conflicts: tuple[ConflictRule, ...]
    runtime_constraints: tuple[RuntimeConstraint, ...]

    @property
    def coord(self) -> str:
        return f"{self.name}@{self.version}"


@dataclass
class CatalogSnapshot:
    """某一目录修订下、求解可见的全部未撤销版本。"""

    revision_id: int
    # name -> 版本按 PEP440 降序
    versions: dict[str, list[VersionInfo]] = field(default_factory=dict)


@dataclass
class _State:
    reqs: dict[str, list[Dependency]]  # 插件 -> 对它的全部约束（含 request 来源）
    assignment: dict[str, VersionInfo]

    def copy(self) -> "_State":
        return _State(reqs={k: list(v) for k, v in self.reqs.items()}, assignment=dict(self.assignment))


@dataclass
class SolveResult:
    kind: str  # "ok" | "unsat" | "cycle"
    revision_id: int
    selections: list[VersionInfo] = field(default_factory=list)
    cycle: list[str] = field(default_factory=list)
    conflicts: dict = field(default_factory=dict)


# ---------- 运行环境匹配 ----------


def runtime_matches(constraints: tuple[RuntimeConstraint, ...], runtime: dict[str, str]) -> str | None:
    """返回不兼容原因；兼容返回 None。"""
    for c in constraints:
        actual = runtime.get(c.name)
        if actual is None:
            return f"运行环境缺少 {c.name}"
        if c.equals is not None:
            if actual != c.equals:
                return f"运行环境 {c.name} 要求精确等于 {c.equals!r}，实际为 {actual!r}"
        else:
            assert c.specset is not None
            pv = _safe_version(actual)
            if pv is None or not c.specset.contains(pv, prereleases=True):
                return f"运行环境 {c.name}={actual!r} 不满足 {c.spec}"
    return None


def _safe_version(text: str) -> Version | None:
    try:
        return Version(text)
    except Exception:
        return None


def _spec_contains(specset: SpecifierSet, pv: Version) -> bool:
    return specset.contains(pv, prereleases=True)


def _pre_explicitly_requested(deps: list[Dependency]) -> bool:
    """版本范围中是否显式出现了预发布版本（如 >=1.0.0a1）。"""
    for dep in deps:
        for specifier in dep.specset:
            if specifier.version is not None and Version(specifier.version).is_prerelease:
                return True
    return False


def _has_final_satisfying(name: str, st: _State, snapshot: CatalogSnapshot, runtime: dict[str, str]) -> bool:
    """该插件是否存在满足全部约束的正式（非预发布）版本。"""
    for cand in snapshot.versions.get(name, []):
        if cand.pv.is_prerelease:
            continue
        if _exclusion_reasons(cand, st, runtime) == []:
            return True
    return False


# ---------- 互斥 ----------


def _pair_conflicts(a: VersionInfo, b: VersionInfo) -> str | None:
    """检查两个候选版本是否互斥（双向）。"""
    for rule in a.conflicts:
        if rule.name == b.name and _spec_contains(rule.specset, b.pv):
            return f"{a.coord} 声明与 {b.name} {rule.spec} 互斥，命中 {b.version}"
    for rule in b.conflicts:
        if rule.name == a.name and _spec_contains(rule.specset, a.pv):
            return f"{b.coord} 声明与 {a.name} {rule.spec} 互斥，命中 {a.version}"
    return None


# ---------- 求解器 ----------


class SolverLimitReached(Exception):
    pass


def solve(
    snapshot: CatalogSnapshot,
    runtime: dict[str, str],
    requirements: list[Dependency],
    node_limit: int = 200_000,
) -> SolveResult:
    initial_reqs: dict[str, list[Dependency]] = {}
    for dep in requirements:
        initial_reqs.setdefault(dep.name, []).append(dep)

    state = _State(reqs=initial_reqs, assignment={})
    deadends: list[dict] = []
    nodes = 0

    def search(st: _State) -> dict[str, VersionInfo] | None:
        nonlocal nodes
        nodes += 1
        if nodes > node_limit:
            raise SolverLimitReached()

        pending = [name for name in st.reqs if name not in st.assignment]
        if not pending:
            return st.assignment

        # 计算每个待选插件的候选，MRV：候选最少者优先
        scored: list[tuple[int, str, list[VersionInfo]]] = []
        for name in pending:
            cands = _eligible(name, st, snapshot, runtime)
            scored.append((len(cands), name, cands))
        scored.sort(key=lambda item: (item[0], item[1]))
        cand_count, chosen_name, candidates = scored[0]

        if cand_count == 0:
            deadends.append(_deadend_for(chosen_name, st, snapshot, runtime))
            return None

        for cand in candidates:
            nxt, reason = _apply(st, chosen_name, cand)
            if nxt is None:
                # cand 的依赖与已选版本直接矛盾：记录到当前插件的死路信息
                deadends.append(
                    {
                        "plugin": chosen_name,
                        "plugin_not_found": False,
                        "versions": {cand.version: [reason]},
                    }
                )
                continue
            result = search(nxt)
            if result is not None:
                return result
        return None

    try:
        assignment = search(state)
    except SolverLimitReached:
        return SolveResult(
            kind="unsat",
            revision_id=snapshot.revision_id,
            conflicts={
                "note": "求解超过节点上限，可能是约束规模过大；请缩小版本范围后重试",
                "plugins": [],
                "constraints": _constraints_view(initial_reqs),
            },
        )

    if assignment is None:
        return SolveResult(
            kind="unsat",
            revision_id=snapshot.revision_id,
            conflicts=_build_conflicts(deadends, initial_reqs),
        )

    cycle = _find_cycle(assignment)
    if cycle:
        return SolveResult(kind="cycle", revision_id=snapshot.revision_id, cycle=cycle)

    ordered = _topological_order(assignment)
    return SolveResult(kind="ok", revision_id=snapshot.revision_id, selections=ordered)


def _eligible(
    name: str,
    st: _State,
    snapshot: CatalogSnapshot,
    runtime: dict[str, str],
) -> list[VersionInfo]:
    """插件 name 在当前状态下的全部可用候选（已按版本降序）。"""
    versions = snapshot.versions.get(name)
    if not versions:
        return []
    reqs = st.reqs.get(name, [])
    pre_allowed = _pre_explicitly_requested(reqs) or not _has_final_satisfying(name, st, snapshot, runtime)
    out: list[VersionInfo] = []
    for v in versions:
        if v.pv.is_prerelease and not pre_allowed:
            continue
        if _exclusion_reasons(v, st, runtime) == []:
            out.append(v)
    return out


def _exclusion_reasons(v: VersionInfo, st: _State, runtime: dict[str, str]) -> list[str]:
    """版本 v 不能入选的全部原因（空列表表示可入选）。"""
    reasons: list[str] = []

    # 1. 对该插件累积的版本范围（请求 + 已选版本的依赖）
    for dep in st.reqs.get(v.name, []):
        if not _spec_contains(dep.specset, v.pv):
            reasons.append(f"不满足 {dep.origin_label} 要求的 {dep.name} {dep.spec}")

    # 2. 运行环境
    rt_reason = runtime_matches(v.runtime_constraints, runtime)
    if rt_reason:
        reasons.append(rt_reason)

    # 3. 与已选版本互斥（双向）
    for assigned in st.assignment.values():
        cf = _pair_conflicts(v, assigned)
        if cf:
            reasons.append(cf)

    # 4. 它声明的依赖不能与“已选”版本矛盾（未选的留待后续决策）
    for dep in v.dependencies:
        assigned_dep = st.assignment.get(dep.name)
        if assigned_dep is not None and not _spec_contains(dep.specset, assigned_dep.pv):
            reasons.append(f"依赖 {dep.name}{dep.spec} 与已选 {assigned_dep.version} 冲突")

    return reasons


def _apply(st: _State, name: str, cand: VersionInfo) -> tuple[_State, str | None]:
    """选定 cand 后生成新状态；若其依赖与已选版本矛盾，返回 (None, 原因)。"""
    nxt = st.copy()
    nxt.assignment[name] = cand
    for dep in cand.dependencies:
        sourced = Dependency(dep.name, dep.spec, dep.specset, origin=cand.coord)
        if sourced.name in nxt.assignment:
            chosen = nxt.assignment[sourced.name]
            if not _spec_contains(sourced.specset, chosen.pv):
                return None, f"依赖 {sourced.name}{sourced.spec} 与已选 {chosen.version} 冲突"
        nxt.reqs.setdefault(sourced.name, []).append(sourced)
    return nxt, None


# ---------- 死路与冲突报告 ----------


def _deadend_for(
    name: str,
    st: _State,
    snapshot: CatalogSnapshot,
    runtime: dict[str, str],
) -> dict:
    versions = snapshot.versions.get(name)
    if not versions:
        return {"plugin": name, "plugin_not_found": True, "versions": {}}
    reqs = st.reqs.get(name, [])
    pre_allowed = _pre_explicitly_requested(reqs) or not _has_final_satisfying(name, st, snapshot, runtime)
    report: dict[str, list[str]] = {}
    for v in versions:
        reasons = _exclusion_reasons(v, st, runtime)
        if not reasons and v.pv.is_prerelease and not pre_allowed:
            reasons = ["预发布版本：存在满足范围的正式版本，默认不选预发布版（需在范围中显式要求）"]
        if reasons:
            report[v.version] = reasons
    if not report:
        # 理论上不会发生（候选为 0 但没有排除原因），给一个保底原因
        report[versions[0].version] = ["没有满足全部约束的版本"]
    return {"plugin": name, "plugin_not_found": False, "versions": report}


def _build_conflicts(deadends: list[dict], initial_reqs: dict[str, list[Dependency]]) -> dict:
    """聚合同一插件的所有死路信息并去重，形成无解报告。"""
    merged: dict[str, dict] = {}
    for item in deadends:
        name = item["plugin"]
        slot = merged.setdefault(
            name, {"plugin": name, "plugin_not_found": False, "versions": {}}
        )
        slot["plugin_not_found"] = slot["plugin_not_found"] or item["plugin_not_found"]
        for ver, reasons in item["versions"].items():
            bucket = slot["versions"].setdefault(ver, [])
            for r in reasons:
                if r not in bucket:
                    bucket.append(r)

    plugins = sorted(merged.values(), key=lambda p: p["plugin"])
    # 每个插件内的版本按 PEP440 降序，原因保持稳定顺序
    for p in plugins:
        p["versions"] = dict(
            sorted(p["versions"].items(), key=lambda kv: Version(kv[0]), reverse=True)
        )

    return {
        "message": "不存在满足全部约束、互斥与无环要求的安装方案",
        "plugins": plugins,
        "constraints": _constraints_view(initial_reqs),
    }


def _constraints_view(initial_reqs: dict[str, list[Dependency]]) -> list[dict]:
    out = []
    for name in sorted(initial_reqs):
        for dep in initial_reqs[name]:
            out.append({"plugin": name, "required_by": dep.origin, "spec": dep.spec})
    return out


# ---------- 环检测与拓扑序 ----------


def _find_cycle(assignment: dict[str, VersionInfo]) -> list[str]:
    """在已选版本构成的依赖图中找环，返回形如 a@1 -> b@2 -> a@1 的路径。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in assignment}
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        v = assignment[node]
        for dep in sorted(v.dependencies, key=lambda d: d.name):
            if dep.name not in assignment:
                continue
            if color[dep.name] == GRAY:
                idx = stack.index(dep.name)
                cycle_names = stack[idx:] + [dep.name]
                return [f"{n}@{assignment[n].version}" for n in cycle_names]
            if color[dep.name] == WHITE:
                found = dfs(dep.name)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for name in sorted(assignment):
        if color[name] == WHITE:
            found = dfs(name)
            if found:
                return found
    return []


def _topological_order(assignment: dict[str, VersionInfo]) -> list[VersionInfo]:
    """依赖在前、被依赖者在后；同层按插件名排序，保证输出确定。"""
    indegree = {name: 0 for name in assignment}
    dependents: dict[str, list[str]] = {name: [] for name in assignment}
    for name, v in assignment.items():
        for dep in v.dependencies:
            if dep.name in assignment:
                indegree[name] += 1
                dependents[dep.name].append(name)

    ready = deque(sorted(n for n, d in indegree.items() if d == 0))
    ordered_names: list[str] = []
    while ready:
        name = ready.popleft()
        ordered_names.append(name)
        newly_ready = []
        for dep in dependents[name]:
            indegree[dep] -= 1
            if indegree[dep] == 0:
                newly_ready.append(dep)
        for n in sorted(newly_ready):
            ready.append(n)

    return [assignment[n] for n in ordered_names]
