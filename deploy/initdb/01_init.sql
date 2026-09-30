-- 仅在 PostgreSQL 数据卷首次创建时执行一次（docker-entrypoint-initdb.d）。
-- 业务表由应用启动时幂等创建（CREATE TABLE IF NOT EXISTS），无需在此重复定义，
-- 这样升级镜像或修改表结构时不会与官方入口脚本冲突。

\echo '[pluginhub] database initialization start'

-- 并发安全设计（由表约束与事务隔离级别共同保证）：
--   1. 唯一约束 uq_plugin_version(plugin_name, version) 防止同名同版本被改写；
--   2. 每次发布/撤销追加一条单调递增的 catalog_revisions；
--   3. 求解事务使用 REPEATABLE READ，保证一份方案不混用不同目录修订。

\echo '[pluginhub] database initialization done'
