# Source Monitoring operations + Runtime v1 runbook

## 范围与默认状态

本文件是 `source_monitoring_operations_v1` 与 `SourceMonitoringRuntime v1` 的运行、首次初始化、保留、迁移和回滚合同。它不扩大来源监控的权限：监控仍默认关闭、默认不自动启动、默认 dry-run；`execution_capability=none`、`live_trading_allowed=false`。本阶段没有新增信息源、Telegram/TDLib、ChatGPT 页面控制、任意 URL 抓取、Provider 基础监控、自动正式 round 或交易能力。

Runtime 构造本身零 I/O，并且不会创建或启用 adapter state。只有全局 `enabled=1`、`auto_start=1`，且某个代码注册 adapter 已通过本地操作员入口显式启用、config version 与初始化策略仍 current 时，worker 才可能轮询该 adapter。Adapter 开关可从 Source Inbox 的懒加载控制面或 owner-exclusive CLI 修改；打开面板、展开健康区或只读取 control 都不会轮询来源。

这些监控变量只从启动进程的真实环境读取，当前 `.env.local` 白名单不会导入 `AI_STUDIO_SOURCE_MONITOR_*`。PowerShell 操作员必须在启动同一进程前设置 `$env:...`；不要把凭据或监控开关提交进仓库。

## Managed Runtime 生命周期

宿主启动顺序固定为：

```text
取得匹配数据库的 OS owner lock
→ 只读数据库迁移门通过
→ 打开已验证 Store
→ 绑定 loopback HTTP 端口
→ 恢复宿主遗留工作
→ 构造 Runtime
→ enabled && auto_start 时恢复 monitoring RUNNING rows 并启动单个非-daemon worker
→ startup ready
→ serve_forever
```

先绑定 loopback 端口、再恢复遗留任务，是现有数据库单实例合同的一部分。Runtime 初版串行执行 adapter，通过 `threading.Event.wait()` 等待最近 effective due；effective due 同时考虑持久化 due 与 dry-run/边界失败的进程内 backoff，禁止忙循环。单个 adapter 失败只使 Runtime `degraded`，不会杀死 HTTP；worker 顶层致命错误只留下有界错误码并投影为 `failed`。

正常关闭顺序固定为：

```text
serve_forever 退出
→ startup ready=false
→ server_close 排空 HTTP handler
→ Runtime stop signal
→ 有界 join
→ 若超时，记录 critical 安全事件并继续持有 owner、等待线程真正退出
→ run_server 返回
→ server.py 释放 owner lock
```

`StudioStore` 没有长期连接型 `close()`；每次 SQLite 操作自行关闭连接。因此这里的 Store quiescence 证据是“handler 已排空且 Runtime 已确认 join”，绝不能在 worker 仍可能访问数据库时释放 owner。

## 首次初始化模式

`AI_STUDIO_SOURCE_MONITOR_INITIAL_MODE` 的允许值只有：

| 模式 | 首次成功轮询 | 后续轮询 |
| --- | --- | --- |
| `seed_only`（默认） | 完整验证所有候选，只把 adapter 原样 `next_checkpoint` 与 sealed initialization receipt 原子提交；不导入历史项、不通知 | 正常处理 checkpoint 后的新项 |
| `catch_up` | 还必须设置 `AI_STUDIO_SOURCE_MONITOR_CATCH_UP_MAX_ITEMS=1..50`，先 preview，再把 `AI_STUDIO_SOURCE_MONITOR_INITIAL_PREVIEW_SHA256` 设为精确预览哈希；只导入确定性排序后的最新 N 项，但提交完整 next checkpoint | 正常处理新项 |
| `from_time` | 还必须设置带时区 RFC3339 的 `AI_STUDIO_SOURCE_MONITOR_FROM_TIME`；时间标准化到 UTC 毫秒，只导入 `occurred_at >= cutoff` 的项 | 同一 cutoff 持续过滤，防止旧项延迟出现 |

`catch_up` 按 `occurred_at` UTC 降序、服务端 fingerprint 升序确定性选择；预览封印 adapter key、config version、起止 checkpoint hash、模式/上限、完整候选与选择后的 fingerprint 集。缺少确认哈希会在 worker 网络读取和 run receipt 之前失败；确认后来源证据漂移会在 Source Inbox 写入和 checkpoint 前失败。UI 授权会把精确 preview hash 与策略、起始 checkpoint 封印为 pending authorization；成功初始化在同一事务消费它，失败/degraded/dry-run 保留，disable 或 config migration 清除。环境与 UI 同时提供 catch-up hash 且不一致时失败关闭。

`seed_only` 与 `from_time` 的 UI 确认绑定模式、参数和起始 checkpoint，而不是延迟启动时的候选全集；首次成功运行分别以当时完整候选建立基线，或严格按已确认 cutoff 过滤。界面会明确提示 seed 候选数可能变化。只读市场 adapter 首次只允许 `seed_only`；其 preview 会执行有界行情读取，确认启用时服务端会再读一次并核对状态，界面展示实际 market-call 计数，但这两次读取都不创建交易上下文或订单。

三种模式都先用完整 Source Inbox 合同验证全部候选。首次轮询只要包含 source error 或 rejected item，就不导入、不提交 checkpoint、也不写 initialization receipt。dry-run 优先于所有模式：只返回 `would_seed/would_import`，不会建立基线。成功 initialization receipt 会把模式、catch-up 上限/from-time cutoff、计数、时间界限、checkpoint hashes 和 preview hash 封印到 `source_adapter_runs`；重启时策略漂移会在 poll 前失败关闭。

## 本地操作员 CLI

```powershell
python -m backend.source_monitoring_cli status
python -m backend.source_monitoring_cli preview sec_filings
python -m backend.source_monitoring_cli preview sec_filings `
  --expected-config-version '<control 中的 config_version>' `
  --expected-state-version '<control 中的 state_version>'
python -m backend.source_monitoring_cli enable sec_filings `
  --expected-config-version '<control 中的 config_version>' `
  --expected-state-version '<control 中的 state_version>' `
  --preview-sha256 '<首次预览 SHA-256>' `
  --confirm ENABLE_SOURCE_MONITORING_ADAPTER
python -m backend.source_monitoring_cli disable sec_filings `
  --expected-config-version '<control 中的 config_version>' `
  --expected-state-version '<control 中的 state_version>' `
  --confirm DISABLE_SOURCE_MONITORING_ADAPTER
python -m backend.source_monitoring_cli run-once sec_filings --confirm RUN_ONCE
```

这些命令都先竞争与正式宿主相同的 OS owner lock；宿主或另一个 CLI 正在使用数据库时返回 `SOURCE_MONITORING_INSTANCE_ACTIVE`，且不解析 Store、不迁移、不轮询、不输出数据库路径。CLI 不接受 `--database`，也不启动 HTTP listener。

- `status`：只读迁移门和 health snapshot；不恢复、不轮询、不写库。
- `preview`（不带 expected 参数）：要求全局 enabled、adapter 已显式 enabled 且 config current；允许访问该固定来源，但不创建 run、import 或 checkpoint，只输出有界计数、时间和 hashes。
- `preview --expected-*`：只用于尚未初始化且已关闭的 adapter，按 control 中的 config/state 做 CAS 绑定；同样零数据库写入。当前 HTTP 请求计数未被 adapter 合同精确计量，所以会诚实显示 `null/not_instrumented`，不会伪报为零。
- `enable/disable`：要求精确 config/state 和确认字符串。首次 enable 还要求本次 preview hash，服务端会重读固定来源；普通 re-enable 与 disable 必须传空 preview。disable 即使遇到代码 config bump 也能用当前 control 身份关闭旧持久状态，但不能借此重新启用或迁移 config。
- `run-once`：精确确认字符串是写门；忽略 auto-start 与 due-time，但不绕过全局/adapter/config/首次模式门。它只调用 monitoring supervisor 的遗留 RUNNING 恢复，不调用宿主通用恢复。

CLI 输出不包含外部 item、URL、headline、summary、checkpoint/ETag、数据库路径、异常文本或 secret；始终声明 Provider 调用为 0、执行能力为 none、真实交易为 false。退出码：0 为成功/dry-run/seed，2 为配置/合同/非成功结果，3 为 owner 冲突，1 为已脱敏的意外内部错误。

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
| `source_monitoring_runtime_stop_timeout` | `database_owner_retained=true` 与固定零执行安全字段；线程真正退出前宿主不返回 |
| `source_monitoring_retention_previewed` | policy version/hash、`eligible_rows=0`, `deleted_rows=0` |
| `source_monitoring_retention_attested` | policy version/hash、`decision=RETAIN_ALL`、零删除/更新计数、幂等标记 |
| `source_monitoring_operator_unavailable` | 固定 `phase`、异常类型名与零执行安全字段；不含 adapter、路径或异常文本 |

`run_started` 只在 `source_adapter_runs` 的 RUNNING 事务成功后发出；terminal 和 attestation 日志只在权威数据库事务完成后发出。日志 sink 抛错会被隔离，不能改变导入、checkpoint、run status 或 receipt。

禁止进入日志的内容包括 URL、headline、summary、来源正文、packet、checkpoint、ETag、Last-Modified、原始错误文本、receipt/import/item/attachment/draft ID、数据库路径、请求正文/头、凭据、Provider/ChatGPT 内容和市场载荷。HTTP 元数据只把 `/api/monitoring/*` 分类为 `api:monitoring`，不保留动态路径或 query。

## 健康语义

`GET /api/monitoring/health` 仍是 `no-store`、只读、无探测接口。它使用稳定的 main/WAL 临时快照，不初始化 schema、不写 retention receipt、不轮询来源、不调用 Provider/市场。顶层合同升级为 `source_monitoring_health_service_v2`，保留 `operations` 子对象并新增进程内 `source_monitoring_runtime_health_v1`。初始化 receipt 或 pending authorization 与当前策略漂移时，持久化开关仍如实展示，但不会投影为 effective enabled：

- `schema_status=current`：表、索引、六个不可变/防 replace trigger 与 migration key 的精确 `sqlite_master` 定义全部匹配，且最新 receipt 可验证；
- `schema_status=migration_required`：监控 state schema 可读，但 operations、Runtime initialization receipt 或 pending authorization additive schema 尚未授权迁移；
- `schema_status=unavailable`：数据库或基础 schema 不可用；
- 部分对象、弱化列或损坏 receipt 不会被自动修复，而是失败关闭。

Runtime 状态闭集为 `disabled/stopped/starting/running/degraded/stalled/failed/stopping`。健康对象只暴露 opaque runtime id、epoch 毫秒时间、active adapter、next due、线程布尔值、heartbeat age/阈值与有界 fatal code；不暴露 thread/PID/hostname/路径/异常堆栈。stalled 使用 monotonic clock，只有 `heartbeat_age_ms > stall_after_ms` 才成立，等于阈值仍是新鲜边界。

持久化 RUNNING row 不再证明 worker 在线；只有当前 HTTPServer 实例持有的 Runtime、新鲜 heartbeat 与实时 `thread.is_alive()` 才能令 `runtime_liveness_verified=true`。`retention_receipt_count=0` 只表示操作员尚未追加政策证明，不把 adapter 判为 failed。即使 liveness 已核验，也只证明本机 worker 有进展，不证明官方源当前可用、内容为事实、交易许可或执行权限。

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

## Adapter 本地控制面

控制面仅绑定当前 loopback 宿主、同一数据库 owner 与当前 Runtime registry：

- `GET /api/monitoring/adapters/control`：懒加载、`no-store`，只读稳定数据库快照且零数据库写入、零外部来源/Provider/市场调用；返回代码 config、持久化开关、effective 状态、初始化状态与阻断码。
- `POST /api/monitoring/adapters/{adapter_key}/initialization-preview`：需要同源本机会话 token、严格 JSON 和精确 config/state；只允许未初始化且关闭的注册 adapter。它可读取固定官方源或有界 readonly-market 来源，但不写 state/checkpoint/Inbox。
- `POST /api/monitoring/adapters/{adapter_key}/enablement`：同样需要 token、严格 CAS 与精确确认。首次 enable 会重新 preview 后原子写入 pending authorization + enabled；disable 保留 checkpoint、run、initialization receipt 与 Source Inbox，仅清除未消费的 pending authorization。

没有 run-now/run-once HTTP route。control/preview/enablement 回执都固定声明 Provider/model/formal round 为零、`execution_capability=none`、`live_trading_allowed=false`；前端拒绝额外字段、非零禁区证据、与请求不绑定的回执，并在 mutation 后重新读取 control 与 health。`auto_start=true` 时，成功 enable 会令 adapter 到期，随后由独立 scheduler 正常执行；这不是 enablement handler 内部导入。

## Additive schema

受控 initializer 在既有 monitoring schema 后增加 operations 证明对象，并为 Runtime v1 initialization receipt 增加：

- migration key：`source_monitoring_initialization_receipt_v1`；
- `source_adapter_runs` 的五个 additive 字段：`initialization_mode`、`initialization_config_version`、`initialization_preview_sha256`、`initialization_receipt_json`、`initialization_receipt_sha256`；
- initialization time/unique seal indexes、receipt 不可变 triggers 与 migration marker guards。

显式首次授权再增加一个独立 additive schema 单元：

- migration key：`source_monitoring_pending_initialization_authorization_v1`；
- `source_adapter_states` 的两个 additive 字段：`pending_initialization_authorization_json`、`pending_initialization_authorization_sha256`；
- 三个 migration marker UPDATE/DELETE/replace guard triggers。

pending JSON 是严格闭集，只含 adapter/config、初始化模式及参数、起始 checkpoint hash、preview hash 与确认时间；读取时必须重新核对 canonical SHA-256、adapter/config/checkpoint 身份及 enabled invariant。部分列、部分 trigger、marker/object 不一致或 seal 损坏全部失败关闭。

随后保留 Phase 8 operations 对象：

- migration key：`source_monitoring_operations_v1`；
- table：`source_monitoring_retention_receipts`；
- index：`idx_source_monitoring_retention_receipts_time`；
- receipt UPDATE/DELETE 拒绝 trigger，以及阻止 `INSERT OR REPLACE` 身份/封印碰撞的 INSERT guard；
- migration marker UPDATE/DELETE 拒绝 trigger，以及阻止 same-key/rowid replace 的 INSERT guard。

retention receipt 固定 `record_version=source_monitoring_retention_receipt_v1`、`policy_version=source_monitoring_retention_policy_v1`、`decision=RETAIN_ALL`、`eligible_rows=deleted_rows=source_rows_updated=0`，并保存 canonical JSON 与 SHA-256。retention 表不向 run/import/item 建立删除语义的外键；Runtime v1 只 additive 增加上述 run 列，不 backfill、删除或改写历史行。initializer 使用迁移 manifest 注入的 `applied_at_ms`；不会在迁移中读取墙钟、启动 worker 或生成 receipt。每个版本化 schema 单元都在 SAVEPOINT 中逐条创建和精确验证，不使用会隐式提交调用方事务的 `executescript()`；调用方 rollback 会同时撤销 pending 业务写入与整个 schema 单元。

同名弱化表、同名空操作 trigger、索引/约束定义漂移、部分 schema objects 或 marker/object 不一致会失败关闭，不能被对象名称或 `CREATE TABLE IF NOT EXISTS` 静默接受。INSERT guards 不依赖 SQLite 的 `recursive_triggers` 设置，`INSERT OR REPLACE` 也不能旁路不可变边界。

## 正式迁移

正式数据库继续使用 [数据库迁移硬门](./database_migration_gate.md)：

1. 只读 `preview`，核对只新增上述 initialization/pending columns、retention table、indexes、triggers 与 migration keys；
2. `prepare` 生成逐字节 backup、授权 candidate 与 sealed prepared 文件；
3. 用户核对精确 authorization token；
4. `apply` 原子替换；
5. 核对 receipt、integrity、foreign keys、WAL/sidecar、physical/logical/table hashes。

系统临时迁移测试会保存代表性的 Source Inbox 与 adapter run/state，并验证 legacy run 行只获得空 initialization/pending authorization 默认值；除 additive columns、空 retention receipt table、schema objects 和 `schema_migrations` markers 外，不允许既有业务内容漂移。`run_isolated_release_drill.py` 明确不执行正式数据库迁移，不能替代这里的证据。

## 回滚矩阵

| 状态 | 允许动作 | 禁止/注意 |
| --- | --- | --- |
| 尚未 ReplaceFileW，source 精确等于 before image | 原 operation 做 `reconcile inspect`，然后只允许 `abort` | 不得 finalize/rollback 或猜测状态 |
| 已替换为精确 candidate、receipt 尚未完成，old image 精确匹配 | 使用原 authorization token 显式 `finalize` 或 `rollback` | 错 token、未知镜像、部分匹配全部拒绝 |
| rollback 已替换回 before image、rollback receipt 未完成 | 用原 token 幂等续完 rollback receipt | 不再次替换或清理证据 |
| migration 已完整完成 | 首先关闭监控/回退代码；保留 inert additive schema | 没有自动 down-migration。恢复旧整库备份会丢迁移后其他数据，必须作为新的停服维护决策单独授权 |

代码回退与数据库回滚是两个不同动作。旧代码可以忽略 additive table/columns；功能默认关闭、auto-start 关闭、seed-only 和 dry-run 默认开启仍是第一层失效保护。初始化列没有自动 down-migration；若代码回退，不得手工删列或改写 receipt。

## 验证边界

离线 FakeAdapter、临时数据库、注入时钟、迁移门、完整回归、静态扫描、fresh-source smoke 与 GitHub Actions 绿灯只能证明源码合同和隔离执行路径。线程测试中的新鲜 heartbeat 可以证明测试 Runtime 的本机活性，但不证明真实 Federal Reserve/BLS/Treasury/SEC/IR/Futu 可用性、24 小时稳定运行、官方来源域真值、盈利能力、交易许可、正式数据库已经迁移或公开发布授权。本轮不连接真实来源，不修改正式用户数据库。
