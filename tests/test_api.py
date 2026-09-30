"""HTTP API 集成测试（SQLite）。"""
from __future__ import annotations

import hashlib
import json


def solve(client, plugins, runtime=None, revision=None):
    payload = {"plugins": plugins, "runtime": runtime or {}}
    if revision is not None:
        payload["revision"] = revision
    return client.post("/api/solve", json=payload)


def test_publish_and_solve_transitive(client, publish):
    assert publish("libfoo", "1.0.0").status_code == 201
    assert publish("app", "2.0.0", deps=[{"name": "libfoo", "spec": ">=1.0"}]).status_code == 201

    r = solve(client, [{"name": "app", "spec": "*"}], runtime={"python": "3.11"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["created"] is True
    chosen = {s["name"]: s["version"] for s in data["selections"]}
    assert chosen == {"libfoo": "1.0.0", "app": "2.0.0"}
    # 拓扑序：libfoo 在 app 之前
    order = [s["name"] for s in data["selections"]]
    assert order.index("libfoo") < order.index("app")


def test_sha256_mismatch_rejected(client, publish):
    r = publish("bad", "1.0.0", sha="a" * 64)
    assert r.status_code == 400
    assert r.json()["error"] == "digest_mismatch"


def test_invalid_version_range_rejected(client, publish):
    r = publish("bad", "1.0.0", deps=[{"name": "x", "spec": "=>banana"}])
    assert r.status_code == 400
    assert r.json()["error"] == "bad_metadata"


def test_invalid_version_rejected(client, publish):
    r = publish("bad", "not-a-version")
    assert r.status_code == 400


def test_invalid_runtime_constraint_rejected(client, publish):
    r = publish("bad", "1.0.0", runtime=[{"name": "python", "spec": ">=3", "equals": "3.11"}])
    assert r.status_code == 400
    assert r.json()["error"] == "bad_metadata"


def test_malformed_sha256_rejected(client, publish):
    # 直接构造请求：错误长度的 sha256
    import hashlib as _h
    content = b"x"
    r = client.post(
        "/api/plugins/bad/versions",
        data={"metadata": json.dumps({"name": "bad", "version": "1.0.0"}), "sha256": "zzz"},
        files={"file": ("bad.pkg", content)},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "bad_metadata"


def test_same_name_version_cannot_overwrite(client, publish):
    assert publish("p", "1.0.0", content="first").status_code == 201
    r = publish("p", "1.0.0", content="second")
    assert r.status_code == 409
    assert r.json()["error"] == "version_already_exists"
    # 旧包仍可下载，内容未被改写
    versions = client.get("/api/plugins/p/versions").json()
    assert len(versions) == 1
    dl = client.get("/api/plugins/p/versions/1.0.0/download")
    assert dl.content == b"first"


def test_conflict_returned_no_plan_saved(client, publish):
    publish("a", "1.0.0", deps=[{"name": "b", "spec": ">=2.0"}])
    publish("b", "1.0.0")
    r = solve(client, [{"name": "a", "spec": "*"}])
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "unsatisfiable"
    conflict_plugins = {p["plugin"] for p in body["details"]["plugins"]}
    assert "b" in conflict_plugins
    # 无方案不保存任何方案
    assert client.get("/api/plans").json() == []


def test_cycle_explicit_error(client, publish):
    publish("a", "1.0.0", deps=[{"name": "b", "spec": "*"}])
    publish("b", "1.0.0", deps=[{"name": "a", "spec": "*"}])
    r = solve(client, [{"name": "a", "spec": "*"}])
    assert r.status_code == 409
    assert r.json()["error"] == "dependency_cycle"
    assert "cycle" in r.json()["details"]


def test_mutex_cannot_coexist(client, publish):
    publish("a", "1.0.0", conflicts=[{"name": "b", "spec": "*"}])
    publish("b", "1.0.0")
    r = solve(client, [{"name": "a", "spec": "*"}, {"name": "b", "spec": "*"}])
    assert r.status_code == 409
    assert r.json()["error"] == "unsatisfiable"


def test_revision_pinning_and_revoke(client, publish):
    publish("lib", "1.0.0")
    publish("app", "1.0.0", deps=[{"name": "lib", "spec": "*"}])
    r1 = solve(client, [{"name": "app", "spec": "*"}])
    assert r1.status_code == 200
    rev_before = r1.json()["catalog_revision"]

    # 撤销 lib 1.0.0
    rv = client.post("/api/plugins/lib/versions/1.0.0/revoke", json={"reason": "安全漏洞"})
    assert rv.status_code == 200
    rev_after = rv.json()["catalog_revision"]
    assert rev_after > rev_before

    # 后续求解在最新修订：lib 被撤销 -> 无解
    r2 = solve(client, [{"name": "app", "spec": "*"}])
    assert r2.status_code == 409
    assert r2.json()["error"] == "unsatisfiable"

    # 但钉住旧修订仍可得到原方案
    r3 = solve(client, [{"name": "app", "spec": "*"}], revision=rev_before)
    assert r3.status_code == 200
    assert r3.json()["catalog_revision"] == rev_before

    # 历史方案仍可查，并标记已撤销项
    plan = client.get(f"/api/plans/{r1.json()['plan_id']}").json()
    assert plan["has_revoked_items"] is True
    lib_entry = next(s for s in plan["selections"] if s["name"] == "lib")
    assert lib_entry["revoked"] is True


def test_same_request_same_plan_idempotent(client, publish):
    publish("x", "1.0.0")
    r1 = solve(client, [{"name": "x", "spec": "*"}])
    r2 = solve(client, [{"name": "x", "spec": ""}])  # 规范化后与 "*" 等价
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["plan_id"] == r2.json()["plan_id"]  # 同请求规范化 + 同修订 -> 同一方案
    assert r2.json()["created"] is False


def test_implicit_latest_equals_explicit_revision(client, publish):
    # 隐式“最新修订”与显式钉住同一修订语义等价，必须得到同一方案
    publish("w", "1.0.0")
    rev = client.get("/api/revisions").json()
    latest = max(r["id"] for r in rev)
    r1 = solve(client, [{"name": "w", "spec": ""}])
    r2 = solve(client, [{"name": "w", "spec": ""}], revision=latest)
    assert r1.json()["plan_id"] == r2.json()["plan_id"]
    assert r2.json()["created"] is False


def test_request_order_does_not_matter(client, publish):
    publish("a", "1.0.0")
    publish("b", "1.0.0")
    r1 = solve(client, [{"name": "a", "spec": "*"}, {"name": "b", "spec": "*"}])
    r2 = solve(client, [{"name": "b", "spec": "*"}, {"name": "a", "spec": "*"}])
    assert r1.json()["selections"] == r2.json()["selections"]
    assert r1.json()["plan_id"] == r2.json()["plan_id"]


def test_new_publish_creates_new_revision_and_distinct_plan(client, publish):
    publish("m", "1.0.0")
    r1 = solve(client, [{"name": "m", "spec": "*"}])
    publish("m", "2.0.0")
    r2 = solve(client, [{"name": "m", "spec": "*"}])
    assert r2.json()["catalog_revision"] > r1.json()["catalog_revision"]
    chosen = {s["name"]: s["version"] for s in r2.json()["selections"]}
    assert chosen["m"] == "2.0.0"
    # 旧方案仍可查
    old = client.get(f"/api/plans/{r1.json()['plan_id']}").json()
    assert next(s for s in old["selections"] if s["name"] == "m")["version"] == "1.0.0"


def test_unknown_revision_404(client, publish):
    publish("z", "1.0.0")
    r = solve(client, [{"name": "z", "spec": "*"}], revision=999999)
    assert r.status_code == 404


def test_revision_listing(client, publish):
    publish("q", "1.0.0")
    client.post("/api/plugins/q/versions/1.0.0/revoke", json={})
    revs = client.get("/api/revisions").json()
    actions = {(r["action"], r.get("plugin_name")) for r in revs}
    assert ("publish", "q") in actions
    assert ("revoke", "q") in actions
    assert ("init", None) in actions


def test_download_content_matches(client, publish):
    content = b"binary-blob"
    r = publish("dl", "1.0.0", content=content)
    url = r.json()["package"]["download_url"]
    got = client.get(url)
    assert got.status_code == 200
    assert got.content == content
    assert got.headers["x-checksum-sha256"] == hashlib.sha256(content).hexdigest()


def test_runtime_filter(client, publish):
    publish(
        "gpuplugin", "1.0.0",
        runtime=[{"name": "cuda", "spec": ">=12"}],
    )
    bad = solve(client, [{"name": "gpuplugin", "spec": "*"}], runtime={"cuda": "11.8"})
    assert bad.status_code == 409
    good = solve(client, [{"name": "gpuplugin", "spec": "*"}], runtime={"cuda": "12.2"})
    assert good.status_code == 200


def test_prerelease_not_auto_selected(client, publish):
    publish("pre", "1.0.0a1")
    publish("pre", "1.0.0")
    # 无显式预发布范围：存在正式版 1.0.0，默认不选预发布版
    r = solve(client, [{"name": "pre", "spec": ""}])
    assert r.status_code == 200
    assert r.json()["selections"][0]["version"] == "1.0.0"


def test_prerelease_explicit_required(client, publish):
    publish("pre", "1.0.0a1")
    r = solve(client, [{"name": "pre", "spec": ">=1.0.0a1"}])
    assert r.status_code == 200
    assert r.json()["selections"][0]["version"] == "1.0.0a1"


def test_prerelease_fallback_when_only_choice(client, publish):
    # 目录里只有预发布版，且它落在范围内：没有正式版可选时允许预发布版
    publish("pre", "2.0.0rc1")
    r = solve(client, [{"name": "pre", "spec": ">=1.9"}])
    assert r.status_code == 200
    assert r.json()["selections"][0]["version"] == "2.0.0rc1"
