"""求解器纯逻辑测试：传递依赖、回溯、互斥、环、撤销、确定性。"""
from __future__ import annotations

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from app.solver import (
    CatalogSnapshot,
    ConflictRule,
    Dependency,
    RuntimeConstraint,
    VersionInfo,
    solve,
)


def dep(name, spec, origin="request"):
    spec = "" if spec == "*" else spec
    return Dependency(name=name, spec=spec, specset=SpecifierSet(spec), origin=origin)


def conflict(name, spec="*"):
    spec = "" if spec == "*" else spec
    return ConflictRule(name, spec, SpecifierSet(spec))


def ver(plugin_id, name, version, deps=(), conflicts=(), runtime=()):
    return VersionInfo(
        id=plugin_id,
        name=name,
        version=version,
        pv=Version(version),
        dependencies=tuple(deps),
        conflicts=tuple(conflicts),
        runtime_constraints=tuple(runtime),
    )


def snap(versions_by_name, revision=1):
    ordered = {}
    for name, vs in versions_by_name.items():
        ordered[name] = sorted(vs, key=lambda v: v.pv, reverse=True)
    return CatalogSnapshot(revision_id=revision, versions=ordered)


def coords(result):
    return [(v.name, v.version) for v in result.selections]


def test_simple_transitive_picks_latest():
    a1 = ver(1, "a", "1.0.0", deps=(dep("b", ">=1", origin="a@1.0.0"),))
    b1 = ver(2, "b", "1.0.0")
    b2 = ver(3, "b", "1.5.0")
    r = solve(snap({"a": [a1], "b": [b1, b2]}), {}, [dep("a", "*")])
    assert r.kind == "ok"
    assert coords(r) == [("b", "1.5.0"), ("a", "1.0.0")]  # 拓扑序：依赖在前


def test_backtracking_version_range():
    # b 2.x 需要 c>=2（不存在），b 1.x 可行 -> 必须回溯到 1.x
    a1 = ver(1, "a", "1.0.0", deps=(dep("b", "*", origin="a@1.0.0"),))
    b2 = ver(2, "b", "2.0.0", deps=(dep("c", ">=2", origin="b@2.0.0"),))
    b1 = ver(3, "b", "1.0.0", deps=(dep("c", ">=1", origin="b@1.0.0"),))
    c1 = ver(4, "c", "1.0.0")
    r = solve(snap({"a": [a1], "b": [b1, b2], "c": [c1]}), {}, [dep("a", "*")])
    assert r.kind == "ok"
    assert dict(coords(r)) == {"a": "1.0.0", "b": "1.0.0", "c": "1.0.0"}


def test_unsat_reports_conflict_plugin():
    a1 = ver(1, "a", "1.0.0", deps=(dep("b", ">=2", origin="a@1.0.0"),))
    b1 = ver(2, "b", "1.0.0")
    r = solve(snap({"a": [a1], "b": [b1]}), {}, [dep("a", "*")])
    assert r.kind == "unsat"
    names = [p["plugin"] for p in r.conflicts["plugins"]]
    assert "b" in names


def test_unknown_plugin_is_conflict():
    r = solve(snap({}), {}, [dep("ghost", "*")])
    assert r.kind == "unsat"
    plugin = r.conflicts["plugins"][0]
    assert plugin["plugin"] == "ghost"
    assert plugin["plugin_not_found"] is True


def test_single_version_per_plugin_intersect_ranges():
    # a 要 b<2，a2(?) 另外再要 b>1.5；1.9 同时满足，2.0 不满足
    a1 = ver(1, "a", "1.0.0", deps=(dep("b", "<2", origin="a@1.0.0"), dep("b", ">=1.5", origin="a@1.0.0")))
    b1 = ver(2, "b", "1.9.0")
    b2 = ver(3, "b", "2.0.0")
    r = solve(snap({"a": [a1], "b": [b1, b2]}), {}, [dep("a", "*")])
    assert r.kind == "ok"
    assert dict(coords(r))["b"] == "1.9.0"


def test_conflict_pair_forbidden():
    a1 = ver(1, "a", "1.0.0", conflicts=(conflict("b"),))
    b1 = ver(2, "b", "1.0.0")
    # b 2.x 与 a 不互斥，但 b 2.x 需要 c>=9 无解；必须不能选 b1（互斥），应无解
    b2 = ver(3, "b", "2.0.0", deps=(dep("c", ">=9", origin="b@2.0.0"),))
    r = solve(
        snap({"a": [a1], "b": [b1, b2]}),
        {},
        [dep("a", "*"), dep("b", "*")],
    )
    assert r.kind == "unsat"
    reasons = " ".join(
        reason
        for p in r.conflicts["plugins"]
        for vs in p["versions"].values()
        for reason in vs
    )
    assert "互斥" in reasons


def test_conflict_reverse_direction_checked():
    # b 声明与 a 互斥（a 没声明），双向检查必须生效
    a1 = ver(1, "a", "1.0.0")
    b1 = ver(2, "b", "1.0.0", conflicts=(conflict("a"),))
    r = solve(snap({"a": [a1], "b": [b1]}), {}, [dep("a", "*"), dep("b", "*")])
    assert r.kind == "unsat"


def test_dependency_cycle_explicit_error():
    a1 = ver(1, "a", "1.0.0", deps=(dep("b", "*", origin="a@1.0.0"),))
    b1 = ver(2, "b", "1.0.0", deps=(dep("a", "*", origin="b@1.0.0"),))
    r = solve(snap({"a": [a1], "b": [b1]}), {}, [dep("a", "*")])
    assert r.kind == "cycle"
    assert r.cycle[0].split("@")[0] == r.cycle[-1].split("@")[0]


def test_self_dependency_cycle():
    a1 = ver(1, "a", "1.0.0", deps=(dep("a", "*", origin="a@1.0.0"),))
    r = solve(snap({"a": [a1]}), {}, [dep("a", "*")])
    assert r.kind == "cycle"


def test_runtime_constraints():
    a1 = ver(1, "a", "1.0.0", runtime=(RuntimeConstraint("python", spec=">=3.9"),))
    r_ok = solve(snap({"a": [a1]}), {"python": "3.11.0"}, [dep("a", "*")])
    assert r_ok.kind == "ok"
    r_bad = solve(snap({"a": [a1]}), {"python": "3.8.0"}, [dep("a", "*")])
    assert r_bad.kind == "unsat"
    r_missing = solve(snap({"a": [a1]}), {}, [dep("a", "*")])
    assert r_missing.kind == "unsat"


def test_runtime_equals():
    a1 = ver(1, "a", "1.0.0", runtime=(RuntimeConstraint("os", equals="linux"),))
    assert solve(snap({"a": [a1]}), {"os": "linux"}, [dep("a", "*")]).kind == "ok"
    r = solve(snap({"a": [a1]}), {"os": "windows"}, [dep("a", "*")])
    assert r.kind == "unsat"


def test_determinism_same_input_same_output():
    a1 = ver(1, "a", "1.0.0", deps=(dep("b", "*", origin="a@1.0.0"),))
    bs = [ver(i, "b", f"1.{i}.0") for i in range(5)]
    snapshot = snap({"a": [a1], "b": bs})
    r1 = solve(snapshot, {}, [dep("a", "*")])
    r2 = solve(snapshot, {}, [dep("a", "==*".replace("==", "")), dep("a", "")])
    # 第二次用两个等价范围请求，合并后应得到同一最高版本
    assert coords(r1) == coords(r2)


def test_diamond_dependency_resolves():
    # a -> b,c ; b -> d>=1,<2 ; c -> d>=1.5
    a1 = ver(
        1, "a", "1.0.0",
        deps=(dep("b", "*", origin="a@1.0.0"), dep("c", "*", origin="a@1.0.0")),
    )
    b1 = ver(2, "b", "1.0.0", deps=(dep("d", ">=1,<2", origin="b@1.0.0"),))
    c1 = ver(3, "c", "1.0.0", deps=(dep("d", ">=1.5", origin="c@1.0.0"),))
    d15 = ver(4, "d", "1.5.0")
    d20 = ver(5, "d", "2.0.0")
    r = solve(snap({"a": [a1], "b": [b1], "c": [c1], "d": [d15, d20]}), {}, [dep("a", "*")])
    assert r.kind == "ok"
    chosen = dict(coords(r))
    assert chosen["d"] == "1.5.0"
    # 每个插件只选一个版本
    names = [n for n, _ in coords(r)]
    assert len(names) == len(set(names))
