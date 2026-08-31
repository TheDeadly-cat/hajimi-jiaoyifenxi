# Source Monitoring operations runbook（阶段 8）

## 范围与默认状态

本文件是 `source_monitoring_operations_v1` 的运行、保留、迁移和回滚合同。它不扩大来源监控的权限：监控仍默认关闭、默认不自动启动、默认 dry-run；`execution_capability=none`、`live_trading_allowed=false`。本阶段没有新增 Telegram/TDLib、ChatGPT 页面控制、任意 URL 抓取、Provider 基础监控、自动正式 round 或交易能力。

Phase 8 没有获得 TTL、删除对象或法律保留期，因此 v1 采用最保守的版本化策略：

```text
source_monitoring_retention_policy_v1
mode = retain_all_evidence
automatic cleanup = false
scheduled cleanup = false
evidence deletion = false
```

这是一项明确的零删除政策，不是未完成的定时清理。任何未来物理删除都必须使用新的 policy version、重新定义候选边界，并取得新的显式用户授权。

## 结构化日志

监控生命周期继续使用宿主的单行 `studio_log_event_v1` JSONL，写到 stdout。Studio 不创建监控日志文件，也不在 SQLite 保存日志；launcher 或其他宿主若把 stdout 重定向到文件，轮转与文件保留由操作员管理。

允许的事件与业务字段如下；每条记录还带通用的 schema、UTC 时间与 severity：

| 事件 | 允许字段 |
| --- | --- |
| `source_monitoring_recovery_completed` | `recovered_run_count` |
| `source_monitoring_run_started` | `adapter_key`, `dry_run` |
| `source_monitoring_run_completed` | `adapter_key`, `status`, `dry_run`, `observed_count`, `accepted_count`, `duplicate_count`, `rejected_count`, `duration_ms`, `error_code`, `state_recorded` 与固定零执行安全字段 |
| `source_monitoring_run_failed` | 与 terminal event 相同的有界计数/状态字段 |
| `source_monitoring_run_recording_failed` | `adapter_key`, 权威持久化 `status`（恢复成功时为 `ABANDONED`）、`dry_run`, `error_code`, `recording_error_code`, `state_recorded`, `fallback_recovery_succeeded` 与固定零执行安全字段 |
| `source_monitoring_retention_previewed` | policy version/hash、`eligible_rows=0`, `deleted_rows=0` |
| `source_monitoring_retention_attested` | policy version/hash、`decision=RETAIN_ALL`、零删除/更新计数、幂等标记 |

`run_started` 只在 `source_adapter_runs` 的 RUNNING 事务成功后发出；terminal 和 attestation 日志只在权威数据库事务完成后发出。日志 sink 抛错会被隔离，不能改变导入、checkpoint、run status 或 receipt。

禁止进入日志的内容包括 URL、headline、summary、来源正文、packet、checkpoint、ETag、Last-Modified、原始错误文本、receipt/import/item/attachment/draft ID、数据库路径、请求正文/头、凭据、Provider/ChatGPT 内容和市场载荷。HTTP 元数据只把 `/api/monitoring/*` 分类为 `api:monitoring`，不保留动态路径或 query。

## 健康语义

`GET /api/monitoring/health` 仍是 `no-store`、只读、无探测接口。它使用稳定的 main/WAL 临时快照，不初始化 schema、不写 retention receipt、不轮询来源、不调用 Provider/市场。已有顶层 `source_monitoring_health_service_v1` 保持兼容，并增加版本化 `operations` 子对象：

- `schema_status=current`：表、索引、六个不可变/防 replace trigger 与 migration key 的精确 `sqlite_master` 定义全部匹配，且最新 receipt 可验证；
- `schema_status=migration_required`：监控 state schema 可读，但 Phase 8 additive schema 尚未授权迁移；
- `schema_status=unavailable`：数据库或基础 schema 不可用；
- 部分对象、弱化列或损坏 receipt 不会被自动修复，而是失败关闭。

`retention_receipt_count=0` 只表示操作员尚未追加政策证明，不把 adapter 判为 failed。`runtime_liveness_verified=false` 始终成立：持久化 `healthy` 只表示最新受验证状态/receipt，不证明 worker 进程在线、官方源当前可用、事件为事实、交易许可或执行权限。全局关闭仍覆盖遗留 RUNNING 证据，不能投影为实际运行中。

## 保留矩阵

| 对象 | v1 处置 | 原因 |
| --- | --- | --- |
| `source_adapter_states` | 保留 | 当前启用状态与 checkpoint；不得自动清空 |
| `source_adapter_runs` | append-only 保留 | 运行 receipt 与 SEC checkpoint 迁移 provenance；包含 dry-run/failed/abandoned |
| `source_inbox_imports` | 保留 | normalized packet、sealed receipt、幂等 import key |
| `source_inbox_import_items` | 保留 | import 与 item 的原子处置关系 |
| `source_inbox_items` | 保留 | 服务端指纹、不可变 item 与用户状态入口 |
| `source_inbox_state_events` | append-only 保留 | 用户状态哈希链 |
| `source_inbox_attachments` | 保留 | 显式用户挂接证据 |
| `source_inbox_round_drafts` | 保留 | 只读草稿证据；不是正式 round |
| `source_inbox_trading_impact_projections` | immutable 保留 | 确定性规则 sidecar 与 parent binding |
| `source_monitoring_retention_receipts` | append-only 保留 | 操作员政策证明 |
| stdout/launcher 日志文件 | Studio 不管理 | 宿主/操作员负责外部文件轮转与保留 |
| migration manifest/backup/candidate/prepared/receipt | 操作员管理 | 服务恢复并人工核对前必须保留；不受 DB retention API 清理 |

`source_inbox_items.expires_at` 不是物理删除许可。关闭监控也不会删除或改写上述记录。

## 保留预览与显式证明

两个本地端点都返回 `Cache-Control: no-store`：

1. `GET /api/monitoring/retention/preview`
   - 只读取受保护表计数，以及 normalized packet + receipt 的合计 UTF-8 字节数；
   - 不返回正文、URL、checkpoint 或错误详情；
   - 用 `policy_sha256`、`inventory_sha256` 与 `preview_sha256` 封印完整预览；
   - 固定 `eligible_rows=0`, `deleted_rows=0`, `source_rows_updated=0`。
2. `POST /api/monitoring/retention/attest`
   - 需要本地 UI token；body 必须只有完整 `preview` 与精确 `confirmation=RETAIN_ALL_EVIDENCE`；
   - `BEGIN IMMEDIATE` 内重新核对 policy、preview seal 与全部计数；任何漂移返回 `SOURCE_MONITORING_RETENTION_PREVIEW_STALE`；
   - 新 receipt 的 `attested_at_ms` 必须严格大于现有最新 receipt；时钟回拨或同毫秒的新证明失败关闭，幂等重放仍返回原 receipt；
   - 唯一写入是一个 `source_monitoring_retention_receipt_v1`；不删除/更新证据；
   - 同一 preview 的完整重放返回既有 receipt（HTTP 200），首次追加返回 HTTP 201。

不存在 cleanup/apply/delete route，也没有 scheduler、startup 或 background attestation hook。

## Additive schema

受控 initializer 在既有 monitoring schema 后增加：

- migration key：`source_monitoring_operations_v1`；
- table：`source_monitoring_retention_receipts`；
- index：`idx_source_monitoring_retention_receipts_time`；
- receipt UPDATE/DELETE 拒绝 trigger，以及阻止 `INSERT OR REPLACE` 身份/封印碰撞的 INSERT guard；
- migration marker UPDATE/DELETE 拒绝 trigger，以及阻止 same-key/rowid replace 的 INSERT guard。

receipt 固定 `record_version=source_monitoring_retention_receipt_v1`、`policy_version=source_monitoring_retention_policy_v1`、`decision=RETAIN_ALL`、`eligible_rows=deleted_rows=source_rows_updated=0`，并保存 canonical JSON 与 SHA-256。表不向 run/import/item 建立删除语义的外键，不增加既有表列，不 backfill，也不改写历史数据。initializer 使用迁移 manifest 注入的 `applied_at_ms`；不会在迁移中读取墙钟、启动 worker 或生成 receipt。

同名弱化表、同名空操作 trigger、索引/约束定义漂移、部分 schema objects 或 marker/object 不一致会失败关闭，不能被对象名称或 `CREATE TABLE IF NOT EXISTS` 静默接受。INSERT guards 不依赖 SQLite 的 `recursive_triggers` 设置，`INSERT OR REPLACE` 也不能旁路不可变边界。

## 正式迁移

正式数据库继续使用 [数据库迁移硬门](./database_migration_gate.md)：

1. 只读 `preview`，核对只新增上述 table/index/triggers/key；
2. `prepare` 生成逐字节 backup、授权 candidate 与 sealed prepared 文件；
3. 用户核对精确 authorization token；
4. `apply` 原子替换；
5. 核对 receipt、integrity、foreign keys、WAL/sidecar、physical/logical/table hashes。

Phase 8 专用系统临时测试还会保存代表性的 Source Inbox 与 adapter run/state，证明除空 receipt table 和 `schema_migrations` marker 外，既有受保护表 content hash 不变。`run_isolated_release_drill.py` 明确不执行数据库迁移，不能替代这里的证据。

## 回滚矩阵

| 状态 | 允许动作 | 禁止/注意 |
| --- | --- | --- |
| 尚未 ReplaceFileW，source 精确等于 before image | 原 operation 做 `reconcile inspect`，然后只允许 `abort` | 不得 finalize/rollback 或猜测状态 |
| 已替换为精确 candidate、receipt 尚未完成，old image 精确匹配 | 使用原 authorization token 显式 `finalize` 或 `rollback` | 错 token、未知镜像、部分匹配全部拒绝 |
| rollback 已替换回 before image、rollback receipt 未完成 | 用原 token 幂等续完 rollback receipt | 不再次替换或清理证据 |
| migration 已完整完成 | 首先关闭监控/回退代码；保留 inert additive schema | 没有自动 down-migration。恢复旧整库备份会丢迁移后其他数据，必须作为新的停服维护决策单独授权 |

代码回退与数据库回滚是两个不同动作。旧代码可以忽略 additive table；功能默认关闭、auto-start 关闭和 dry-run 默认开启仍是第一层失效保护。

## 验证边界

离线 fixture、迁移门、完整回归、静态扫描、fresh-source smoke 与 GitHub Actions 绿灯只能证明源码合同和隔离执行路径。它们不证明实时 Federal Reserve/BLS/Treasury/SEC/IR/Futu 可用性、runtime liveness、官方来源域真值、盈利能力、交易许可、正式数据库已经迁移或公开发布授权。
