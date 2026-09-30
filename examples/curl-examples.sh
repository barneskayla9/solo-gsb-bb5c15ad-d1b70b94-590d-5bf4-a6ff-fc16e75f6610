#!/usr/bin/env bash
# curl 调用示例（离线环境同样适用）。
# 依赖：bash、curl、sha256sum（或 openssl）、python3（仅用于生成测试包摘要）。
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"

echo "== 1) 健康检查 =="
curl -fsS "$BASE_URL/health"; echo

echo "== 2) 准备安装包并计算 SHA-256 =="
mkdir -p /tmp/pluginhub-demo
printf 'labcore binary payload\n' > /tmp/pluginhub-demo/labcore-1.5.0.pkg
SHA=$(sha256sum /tmp/pluginhub-demo/labcore-1.5.0.pkg | cut -d' ' -f1)
echo "sha256=$SHA"

echo "== 3) 发布插件版本（multipart：metadata + sha256 + file） =="
curl -fsS -X POST "$BASE_URL/api/plugins/labcore/versions" \
  -F 'metadata={"name":"labcore","version":"1.5.0",
      "dependencies":[],
      "conflicts":[],
      "runtime_constraints":[{"name":"python","spec":">=3.10"},
                             {"name":"os","equals":"linux"}]};type=application/json' \
  -F "sha256=$SHA" \
  -F "file=@/tmp/pluginhub-demo/labcore-1.5.0.pkg;type=application/octet-stream"; echo

echo "== 4) 故意提交错误摘要（应返回 400 digest_mismatch） =="
curl -sS -X POST "$BASE_URL/api/plugins/labcore/versions" \
  -F 'metadata={"name":"labcore","version":"1.5.1"};type=application/json' \
  -F "sha256=0000000000000000000000000000000000000000000000000000000000000000" \
  -F "file=@/tmp/pluginhub-demo/labcore-1.5.0.pkg;type=application/octet-stream"; echo

echo "== 5) 重复发布同名同版本（应返回 409 version_already_exists） =="
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE_URL/api/plugins/labcore/versions" \
  -F 'metadata={"name":"labcore","version":"1.5.0"};type=application/json' \
  -F "sha256=$SHA" \
  -F "file=@/tmp/pluginhub-demo/labcore-1.5.0.pkg;type=application/octet-stream"

echo "== 6) 发布依赖 labcore 的应用 =="
printf 'analyzer binary payload\n' > /tmp/pluginhub-demo/analyzer-3.0.0.pkg
ASHA=$(sha256sum /tmp/pluginhub-demo/analyzer-3.0.0.pkg | cut -d' ' -f1)
curl -fsS -X POST "$BASE_URL/api/plugins/analyzer/versions" \
  -F 'metadata={"name":"analyzer","version":"3.0.0",
      "dependencies":[{"name":"labcore","spec":">=1.3,<2"}],
      "conflicts":[{"name":"oldreport","spec":""}],
      "runtime_constraints":[{"name":"python","spec":">=3.10"}]};type=application/json' \
  -F "sha256=$ASHA" \
  -F "file=@/tmp/pluginhub-demo/analyzer-3.0.0.pkg;type=application/octet-stream"; echo

echo "== 7) 求解完整安装方案（传递依赖 + 运行环境） =="
curl -fsS -X POST "$BASE_URL/api/solve" \
  -H 'Content-Type: application/json' \
  -d '{"runtime":{"python":"3.11.5","os":"linux"},
       "plugins":[{"name":"analyzer","spec":">=3,<4"}]}'; echo

echo "== 8) 相同请求再次求解（created=false，方案幂等） =="
curl -fsS -X POST "$BASE_URL/api/solve" \
  -H 'Content-Type: application/json' \
  -d '{"runtime":{"python":"3.11.5","os":"linux"},
       "plugins":[{"name":"analyzer","spec":">=3,<4"}]}'; echo

echo "== 9) 撤销版本 =="
curl -fsS -X POST "$BASE_URL/api/plugins/labcore/versions/1.5.0/revoke" \
  -H 'Content-Type: application/json' -d '{"reason":"安全通告"}'; echo

echo "== 10) 撤销后求解 -> 409 unsatisfiable，返回冲突插件与约束 =="
curl -sS -X POST "$BASE_URL/api/solve" \
  -H 'Content-Type: application/json' \
  -d '{"runtime":{"python":"3.11.5","os":"linux"},
       "plugins":[{"name":"analyzer","spec":""}]}'; echo

echo "== 11) 查看历史方案（撤销项会被标记 revoked）与目录修订 =="
curl -fsS "$BASE_URL/api/plans?limit=5"; echo
curl -fsS "$BASE_URL/api/revisions?limit=5"; echo

echo "完成。"
