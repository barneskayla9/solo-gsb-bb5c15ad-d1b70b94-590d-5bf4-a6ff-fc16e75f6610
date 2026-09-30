# 离线实验室插件包发布与安装方案服务（Plugin Hub）

为**无法联网的实验室**提供插件版本发布、完整性校验与可复现安装方案求解。
技术栈：**Python 3.11 + FastAPI + SQLAlchemy 2 + PostgreSQL 16**，
全部镜像与依赖可在内网镜像站预先准备，运行期不访问任何外部地址。

## 核心能力

- **发布版本**：提交安装包、SHA-256、兼容运行环境、依赖版本范围、互斥项。
  - SHA-256 摘要不符 → **拒绝**（`400 digest_mismatch`）；
  - 版本号必须是合法 **PEP 440**，依赖/环境版本范围必须是合法 **PEP 440 SpecifierSet**（如 `>=1.2,<2`，空或 `*` 表示任意）→ 非法范围**拒绝**（`400 bad_metadata`）；
  - **同名同版本发布后不可改写**（数据库唯一约束 + 冲突响应 `409 version_already_exists`，并发发布同样拦截）。
- **方案求解**：提交运行环境（如 `{"python":"3.11.5","os":"linux"}`）、目标插件及版本范围，
  服务在**同一目录修订**的**未撤销版本**中求出满足**全部传递依赖**的完整方案：
  - 每个插件恰好选一个兼容版本，多方版本范围取交集；
  - 兼容运行环境（范围 `spec` 或精确 `equals`）逐版本校验；
  - **互斥项不得共存**（双向检查）；
  - 存在**依赖环**时明确报错（`409 dependency_cycle`，返回环路径）；
  - 预发布版本默认不自动选中，除非范围显式要求或无正式版本可选。
- **可复现**：目录采用单调递增的**修订（revision）**；相同请求 + 相同修订得到相同方案
  （规范化 JSON + SHA-256 去重，重复提交返回同一个 `plan_id`，`created=false`）。
- **无解**：返回**冲突插件及各自被拒版本/约束明细**（`409 unsatisfiable`），**不保存部分方案**。
- **撤销**：撤销只生成新修订、影响**后续**求解；**历史方案仍可查**，其中已撤销条目标记 `revoked: true`。
- **并发一致性**：求解在 PostgreSQL `REPEATABLE READ` 事务中读取修订与版本快照，
  一份方案**绝不混用不同目录修订**。

## 一键启动（含数据库初始化）

需要安装 Docker 24+ 与 Docker Compose v2。

```bash
cd plugin-hub
# 可选：复制配置并修改端口/密码
cp .env.example .env

# 构建并启动（首次会初始化 PostgreSQL 数据卷与应用表结构）
docker compose up -d --build

# 查看状态
docker compose ps
curl -fsS http://127.0.0.1:8000/health
```

启动后访问：

| 用途 | 地址 |
| --- | --- |
| 服务根路径 | http://127.0.0.1:8000/ |
| 健康检查 | http://127.0.0.1:8000/health |
| **Swagger 交互文档** | http://127.0.0.1:8000/docs |
| OpenAPI Schema | http://127.0.0.1:8000/openapi.json |

数据持久化：

- PostgreSQL 数据：Docker 卷 `pgdata`；
- 安装包文件：Docker 卷 `packages`（容器内 `/data/packages`，按 sha256 分桶存储、内容寻址去重）；
- 首次建库脚本：`deploy/initdb/01_init.sql`（仅空数据卷首次启动执行）；
- 业务表由应用启动时幂等创建（`CREATE TABLE IF NOT EXISTS`），无需迁移工具。

停止 / 清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据卷（清空数据库与安装包）
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg2://pluginhub:pluginhub@db:5432/pluginhub` | 数据库连接串（Compose 已自动拼接） |
| `POSTGRES_USER/PASSWORD/DB` | `pluginhub/pluginhub/pluginhub` | 数据库初始化账户 |
| `APP_PORT` | `8000` | 宿主机访问端口（容器内固定 8000） |
| `PACKAGE_DIR` | `/data/packages` | 安装包落盘目录 |
| `MAX_UPLOAD_BYTES` | `1073741824`（1 GiB） | 单次上传大小上限 |
| `SOLVER_NODE_LIMIT` | `200000` | 求解回溯节点上限（防御病态请求） |

> 生产环境请通过 `.env` 修改 `POSTGRES_PASSWORD` 与 `APP_PORT`。

## HTTP API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/plugins/{name}/versions` | 发布版本（multipart：`metadata`、`sha256`、`file`） |
| `GET` | `/api/plugins` | 插件列表 |
| `GET` | `/api/plugins/{name}/versions` | 版本列表（`?include_revoked=true` 含已撤销） |
| `GET` | `/api/plugins/{name}/versions/{version}` | 版本详情 |
| `GET` | `/api/plugins/{name}/versions/{version}/download` | 下载安装包（响应头带 `X-Checksum-Sha256`） |
| `POST` | `/api/plugins/{name}/versions/{version}/revoke` | 撤销版本（生成新修订） |
| `GET` | `/api/revisions[?limit=]` | 目录修订列表 |
| `GET` | `/api/revisions/{id}` | 修订详情 |
| **`POST`** | **`/api/solve`** | **求解完整安装方案**（可带 `revision` 钉住历史修订） |
| `GET` | `/api/plans[?limit=]` | 历史方案列表 |
| `GET` | `/api/plans/{id}` | 历史方案详情（已撤销项实时标记） |

错误响应统一为：

```json
{ "error": "unsatisfiable", "message": "...", "details": { } }
```

## 调用示例

### 1. 发布版本

```bash
SHA=$(sha256sum ./labcore-1.5.0.pkg | cut -d' ' -f1)

curl -X POST http://127.0.0.1:8000/api/plugins/labcore/versions \
  -F 'metadata={
        "name": "labcore",
        "version": "1.5.0",
        "dependencies": [],
        "conflicts": [],
        "runtime_constraints": [
          {"name": "python", "spec": ">=3.10"},
          {"name": "os", "equals": "linux"}
        ]
      };type=application/json' \
  -F "sha256=$SHA" \
  -F "file=@./labcore-1.5.0.pkg;type=application/octet-stream"
```

字段说明（`metadata` JSON）：

- `dependencies`：`[{"name": "...", "spec": ">=1.0,<2"}]`，`spec` 留空或 `*` 表示任意版本；
- `conflicts`：`[{"name": "...", "spec": ""}]`，与目标插件命中范围的版本互斥；
- `runtime_constraints`：每项必须且只能给 `spec`（版本范围）或 `equals`（精确值）。

### 2. 求解完整方案（含传递依赖）

```bash
curl -X POST http://127.0.0.1:8000/api/solve \
  -H 'Content-Type: application/json' \
  -d '{
        "runtime": {"python": "3.11.5", "os": "linux"},
        "plugins": [{"name": "analyzer", "spec": ">=3,<4"}]
      }'
```

成功响应（`selections` 为完整方案，按**拓扑序**排列，被依赖者在前）：

```json
{
  "plan_id": 3,
  "catalog_revision": 2,
  "created": true,
  "request_hash": "9f2c…",
  "selections": [
    {
      "name": "labcore", "version": "1.5.0", "revoked": false,
      "dependencies": [],
      "package": {"filename": "labcore-1.5.0.pkg", "sha256": "…", "size": 23,
                  "download_url": "/api/plugins/labcore/versions/1.5.0/download"}
    },
    {
      "name": "analyzer", "version": "3.0.0", "revoked": false,
      "dependencies": [{"name": "labcore", "spec": ">=1.3,<2"}],
      "package": { }
    }
  ]
}
```

钉住历史修订求解（可复现历史结果）：

```bash
curl -X POST http://127.0.0.1:8000/api/solve \
  -H 'Content-Type: application/json' \
  -d '{"runtime": {...}, "plugins": [...], "revision": 2}'
```

### 3. 无解响应（冲突插件 + 约束）

```json
{
  "error": "unsatisfiable",
  "message": "不存在满足全部传递依赖、互斥与运行环境要求的安装方案",
  "details": {
    "message": "...",
    "plugins": [
      {
        "plugin": "labcore",
        "plugin_not_found": false,
        "versions": {
          "1.2.0": ["不满足 analyzer@3.0.0 要求的 labcore >=1.3,<2"]
        }
      }
    ],
    "constraints": [{"plugin": "analyzer", "required_by": "request", "spec": ""}]
  }
}
```

### 4. 撤销与历史方案

```bash
curl -X POST http://127.0.0.1:8000/api/plugins/labcore/versions/1.5.0/revoke \
  -H 'Content-Type: application/json' -d '{"reason": "安全通告"}'

curl http://127.0.0.1:8000/api/plans/3     # 历史方案保留；labcore 条目 revoked=true
```

### 5. 一键演示脚本

```bash
# 纯 Python 标准库实现（无需安装任何客户端）：
python examples/demo.py
BASE_URL=http://10.0.0.5:8000 python examples/demo.py

# 或 curl 版本：
bash examples/curl-examples.sh
```

## 离线部署说明

在内网有网的构建机上准备镜像，再导入隔离网络：

```bash
# 构建机
docker compose build
docker save pluginhub:1.0.0 postgres:16-alpine | gzip > pluginhub-images.tar.gz

# 实验室主机
gunzip -c pluginhub-images.tar.gz | docker load
docker compose up -d        # 使用本仓库里的 compose 文件
```

Python 依赖固定版本见 `requirements.txt`；如 Dockerfile 构建也无法联网，
可将 wheel 包提前下载到内网源并在 Dockerfile 中改用
`pip install --index-url http://内网镜像/simple --trusted-host 内网镜像`。

## 本地开发 / 测试（无需 Docker 与 PostgreSQL）

测试使用 SQLite 内存库：

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

直接以 PostgreSQL 本地运行：

```bash
export DATABASE_URL='postgresql+psycopg2://pluginhub:pluginhub@127.0.0.1:5432/pluginhub'
export PACKAGE_DIR=./data/packages
uvicorn app.main:app --reload
```

## 目录结构

```
app/
  config.py            # 环境变量配置
  database.py          # 引擎/会话/建表初始化
  models.py            # 修订、版本、方案三张表
  schemas.py           # 请求/响应模型与发布强校验（PEP 440）
  solver.py            # 纯逻辑求解器：回溯、MRV、互斥、环检测、确定性排序
  service.py           # 目录快照、请求规范化/哈希、序列化
  packages.py          # 安装包落盘（sha256 内容寻址、事务后提交）
  errors.py            # 统一错误
  routers/catalog.py   # 发布/撤销/查询/下载
  routers/plans.py     # 求解/历史方案
deploy/initdb/         # PostgreSQL 首次初始化 SQL
examples/              # demo.py（零依赖）、curl-examples.sh
tests/                 # 求解器单测 + API 集成测试（SQLite）
docker-compose.yml     # 一键启动（含数据库初始化与健康检查）
Dockerfile
```

## 设计要点备忘

- **目录修订（Catalog Revision）**：发布与撤销都在同一数据库事务中“写版本/标记 + 追加修订”后提交；
  求解以 `published_revision_id <= R` 且（未撤销或 `revoked_revision_id > R`）定义 R 时刻的快照。
- **快照事务**：求解使用 `REPEATABLE READ`，求解期间发生的发布/撤销对本次不可见，杜绝跨修订混用。
- **求解器**：基于约束传播的 CSP 回溯；候选版本降序、插件按 MRV 与名称排序，保证结果唯一确定；
  完整赋值后做一次环检测，有环直接报错而不是悄悄选出非法方案。
- **文件与事务**：安装包先写临时文件并校验摘要；数据库事务提交成功后才原子重命名到最终路径，
  失败即清理，保证“元数据成功”与“文件存在”一致。
