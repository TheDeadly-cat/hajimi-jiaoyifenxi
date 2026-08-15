# 当前目标完成审计（2026-08-12）

本文把原始交付目标映射到当前工作树的实现和验证证据。它不是正式数据库迁移授权，也不替代用户对永久备份位置或真实 Provider 的明确选择。

| 目标 | 当前证据 | 状态 |
| --- | --- | --- |
| 启动不再静默迁移 | `server.py` 先取得 owner；`assert_database_ready_for_startup()` 只读预检；`StudioStore._open_existing_schema()` 只打开已核验 schema | 已实现 |
| 迁移清单、备份、授权、提交后核验 | `backend/database_migration.py` 的 `preview → prepare → apply → reconcile`；manifest/prepared/receipt v2；integrity、外键、WAL、物理 SHA、逐表逻辑 SHA | 已实现 |
| 迁移崩溃恢复与 owner 安全 | `database_migration_commit.py` 的锁区租约、ReplaceFileW、intent 链；owner lock 拒绝 symlink/reparse/hardlink | 已实现 |
| 足球只读能力包 | `football_research_readonly`、v2 adapter/port/UI、闭合合同、材料绑定、四类证据、赛程/旅行/阵容/伤停/战术/截止时间 | 已实现 |
| 足球概率与执行边界 | `withheld_no_calibration`、`future_probability_available=false`、赔率仅代理；无投注、钱包、下单、自动决定 | 已实现 |
| 股票只读能力包 | `stock_research_readonly`、显式 `stock_room_scope_v1`、Futu/SEC/IR/复权/公司行动五项 preflight、同事务材料重验 | 已实现 |
| 领域能力不进入通用 orchestrator | `round_contexts.py` provider registry；orchestrator 只处理通用授权、冻结、哈希、恢复 | 已实现 |
| 股票/足球不进入 storage candidate experiment | 当前房间与冻结 artifact 双重 domain gate；对应负测在 `tests/test_candidate_experiment.py` | 已实现 |
| 宿主前端静态分割 | `React.lazy` + `Suspense`；`frontend/tests/codeSplitting.test.js`；此前 Vite 构建无大块警告 | 已实现 |
| 后端测试分层 | `scripts/backend_test_layers.json` 与 `run_backend_tests_isolated.py` | 已实现 |
| 版本化源码备份 | `scripts/create_versioned_source_backup.py`；闭合 manifest、逐文件/总哈希、ZIP archive SHA、敏感文件/链接排除、无覆盖发布 | 已实现 |

## 当前验证数字

- 最近一次全量后端隔离回归（含 sidecar、verified-startup 绕过、
  dangling-link、hardlink 和 symlink/reparse 入口修复）：`1213/1213 OK`（909.471s；系统临时 runtime
  `C:\Users\Administrator\AppData\Local\Temp\ai-collaboration-studio-tests-e4evkmt3`）。
- migration 层（含最新 sidecar、verified-startup 绕过、dangling-link、hardlink 和 symlink/reparse 入口回归）：`78/78 OK`；core 层（包含最新备份工具与所有权锁回归）：
  `172/172 OK`；domains 层此前 `106/106 OK`。
- owner-lock 定向负测：`5/5 OK`；源码备份：`10/10 OK`；core 层最新为
  `172/172 OK`。只读目标预检不创建目录、已存在版本返回 `ready:false`，
  且目标链中的普通文件组件会在预检阶段 fail-closed。
- 最新只读 URI hardening 定向回归：足球/股票材料服务、round trace、讨论
  审计 `32/32 OK`；之后 core/domains 分层仍为 `172/172` 与 `106/106 OK`。
- 最新迁移 sidecar hardening 覆盖零字节 WAL、SHM、journal、目录和 symlink
  侧车，均不删除现场并 fail-closed。
- `STORE.configure_verified_startup()` 现在也拒绝带 sidecar 的伪造
  `startup_identity`，不能绕过只读预检直接打开不干净的正式文件族。
- startup identity 对 dangling symlink/链接目标消失也 fail-closed，不把损坏
  的 sidecar 当作不存在。
- startup identity 现在也拒绝硬链接主库；直接调用
  `configure_verified_startup()` 不能绕过正式迁移预检的物理文件身份门。
- 迁移源路径和底层文件身份采样现在也拒绝 symlink/reparse alias，不能通过
  先 `resolve()` 再比较哈希的方式把别名伪装成独立 SQLite 文件。
- README 与 `docs/capability_packs.md` 已同步列出实际白名单中的足球/股票只读包，
  并明确授权、截止时间和无执行边界；最新源码归档已包含这些文档更新。
- 当前前端测试：`317/317`；Vite production build：`1673 modules`，无大块 chunk 警告。
- 浏览器临时正向流程：足球、股票检查并显式授权均通过；切换房间会清除旧授权。

## 明确未执行事项

1. 正式数据库尚未执行 `apply`；只有用户审阅精确 prepared SHA 并提供授权 token 后才能执行。
2. 永久源码备份目标路径尚未推断或写入；当前归档只放在系统临时目录。
3. 未连接真实 Provider、Futu/OpenD、SEC、IR 或外部市场服务。
4. 正式库当前 SHA 仍为 `B32E88A0C0BE5DB2D052904221C6C85D1B1C7862FD76F45EB8DF08B7EC41CC05`；8770 与 11111 未监听；未初始化 Git。
5. 空 P28/plugin migration ledger 仍不实现；只有出现真实 plugin-owned mutable schema 与用户用例后才重新立项。

## 2026-08-12 最终路径链复核

- 新增 `backend/path_identity.py`，在 owner 锁和迁移 source 解析前逐级检查原始路径链，拒绝父目录 symlink、junction/reparse alias；新增 owner/source parent-chain 负测。
- owner + migration 定向回归 `29/29 OK`；migration layer `87/87 OK`。
- 最终完整后端隔离回归 `1222/1222 OK`，`1149.455s`，runtime 为系统临时目录。此前一次全量运行出现过单次 Windows `ConnectionAbortedError`，独立 HTTP 安全模块 `7/7 OK`，后续完整运行全部通过。
- manifest、backup、candidate、prepared、receipt 以及底层 commit 原语现在都在 `resolve()` 前拒绝父目录 symlink/junction/reparse alias；相应负测已纳入 migration 层。
- 最新版本化源码归档已在系统临时目录创建并验证（522 文件、源总字节 `25,399,260`、manifest SHA `8d8b65ada97b921e79fcea6184a435698aac44cb8004eb0322ce05bd6f7445aa`、ZIP SHA `6c121b31972eea98f722bf873868bad6b3e274a756cd3a6460a0a852ceb9875e`）。
