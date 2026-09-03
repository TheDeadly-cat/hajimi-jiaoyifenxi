# SQLite 正式迁移硬门

正式服务启动不再运行 `StudioStore._initialize()`。启动顺序固定为：取得数据库 owner 锁、对现有 SQLite 做只读预检、在系统临时目录的副本上模拟当前代码的迁移、确认源库没有待迁移差异，然后通过仅供启动门使用的私有入口打开现有 schema。任一待迁移差异、未完成迁移意图、SQLite `-wal` / `-shm` / `-journal` sidecar、完整性错误、外键违规、文件漂移或缺失数据库都会使启动失败；启动不会建库、加列、建表、seed 或回填。

所有只读 SQLite 连接（迁移预检、足球/股票材料检查和 round trace）都使用
`mode=ro&immutable=1` 并再启用 `query_only`/外键检查；不得新增裸 `mode=ro`
连接，以免 WAL 数据库的只读探针产生共享内存副作用。

正式迁移分为四个独立阶段：

1. `preview`：源库只读，输出迁移清单；清单封印源文件 SHA-256、schema、迁移键、逐表行数与内容哈希、完整性、外键和 WAL 状态。
2. `prepare`：持有 owner 锁，重验清单，先创建与源库字节 SHA 相同的备份并在备份上再次运行 `integrity_check` 和 `foreign_key_check`；随后仅在独立候选副本上执行迁移，核验候选的完整性、外键、WAL、schema 和逐表逻辑哈希。到此仍未修改源库。
3. 用户显式授权：`prepare` 完成后才给出 `AUTHORIZE-MIGRATION-<prepared_sha256>`。该值绑定清单、源库、备份和精确候选库，不能用于其他文件或其他计划。
4. `apply`：再次取得 owner 锁并重验上述三个文件；Windows 上以不共享的文件句柄确认源库、备份和候选均已关闭，再把精确候选复制到源库同目录的 staging 文件。工具在发布 intent 前重新取得并保持 backup 与 candidate 的 SQLite 锁区租约；随后对旧 source 的 `PENDING_BYTE` 起始 512 字节锁区取得非阻塞独占租约，由 `ReplaceFileW` 换入候选并同时保留换入前镜像。换入后立即取得新 source 的同类租约。backup、candidate、旧镜像和新 source 四份精确镜像都保持受锁，直到 integrity、外键、sidecar、物理 SHA、逐表逻辑 SHA、不可覆盖 receipt 和 `complete` marker 全部落盘。DELETE 与 WAL 模式均使用同一门；源库上的旧式逐步迁移不会发生。

工具从不删除正式库或恢复镜像的 SQLite sidecar。发现任一 sidecar、活动 SQLite 锁、文件身份漂移、硬链接别名或替换竞态时都会关闭失败并保留现场。SQLite 锁区租约阻止标准 SQLite 客户端在提交窗口读写；不遵守 SQLite 文件锁的原始磁盘写程序仍必须由操作者事先停止。`ReplaceFileW` 提供同卷文件替换和旧镜像，但本文不把它声明为断电事务；intent 哈希链、两份已 flush 镜像和显式恢复命令共同承担崩溃后的可审计恢复。

所有路径必须显式给出。source、owner lock、manifest、backup、candidate、prepared、receipt 及各自 `-wal` / `-shm` / `-journal` 文件族必须按 Windows 大小写规则两两不相交；任一输出文件族已有主文件、sidecar、目录或链接都会在写入前失败。owner lock 本身也必须是独立的普通文件，不能是符号链接、reparse point 或 hardlink；锁文件打开前后都会重验物理身份。reserved marker/staging/rollback 名及其大小写变体也不能用作输出。所有 SQLite 主文件必须是 link count 为 1 的普通文件，且源库、备份、候选、staging 和恢复镜像的物理 file ID 必须互异；工具没有 hardlink 发布退路。备份和候选应保留到人工复核 receipt 和服务恢复完成之后。JSON 与 marker 都先写同目录临时文件、fsync，再以 fail-if-exists 方式完整发布，因此不会暴露截断的正式 receipt。迁移 shadow 只允许系统临时目录内已存在、非硬链接的副本。

Phase 8 的 `source_monitoring_operations_v1` 也只能走本硬门。它只 additive 增加 retain-all policy receipt 表、一个索引、receipt/marker 不可变 triggers 和 migration key；不加旧表列、不 backfill、不删除或改写 Source Inbox、adapter state/checkpoint/run receipt。专用迁移测试用代表性旧库核对逐表 content hash，并注入 candidate 已替换但 receipt 未发布的故障，证明原 token rollback 恢复迁移前 physical/logical SHA。完整对象与 completed-migration 无自动 down-migration 的边界见 [Source Monitoring operations runbook](./source_monitoring_operations_runbook.md)。

Adapter 操作员控制面的 `source_monitoring_pending_initialization_authorization_v1` 同样只能走本硬门：它只向 `source_adapter_states` additive 增加 pending authorization JSON/SHA-256 两列、三个 marker guards 和 migration key；不启用 adapter、不生成授权、不轮询来源，也不回填或改写既有 state/checkpoint/run/Source Inbox。部分对象或 seal 异常失败关闭，正式 apply 后没有自动 down-migration。

```powershell
$db = 'C:\path\to\collaboration_studio.sqlite3'
$evidence = 'C:\path\to\migration-evidence\2026-08-12'

python scripts\run_database_migration_gate.py preview `
  --database $db `
  --manifest "$evidence\manifest.json"

python scripts\run_database_migration_gate.py prepare `
  --database $db `
  --manifest "$evidence\manifest.json" `
  --backup "$evidence\before.sqlite3" `
  --candidate "$evidence\candidate.sqlite3" `
  --prepared "$evidence\prepared.json"

# 人工审阅 manifest、备份/候选哈希和 prepared 后，逐字复制 prepare 输出的 token。
python scripts\run_database_migration_gate.py apply `
  --database $db `
  --prepared "$evidence\prepared.json" `
  --receipt "$evidence\receipt.json" `
  --authorize 'AUTHORIZE-MIGRATION-<prepared_sha256>'
```

若 `apply` 在 intent 与 `complete` 之间中断，后续启动会保持关闭。先只读检查精确 operation（即 `prepared_sha256`），再由用户明确选择：源库尚未替换时只能 `abort`；源库已是精确候选且旧镜像也精确匹配时，可用原 token `finalize` 或 `rollback`；若 rollback 已换回但 receipt 尚未完整提交，则只允许用原 token 幂等续完 rollback。未知状态不会自动猜测或清理。普通 journal API 不能发布 verified、receipt 或 terminal 事件。

```powershell
python scripts\run_database_migration_gate.py reconcile `
  --database $db `
  --operation '<prepared_sha256>' `
  --action inspect

python scripts\run_database_migration_gate.py reconcile `
  --database $db `
  --operation '<prepared_sha256>' `
  --action finalize `
  --authorize 'AUTHORIZE-MIGRATION-<prepared_sha256>'
```

开发和自动测试不得把项目 `runtime` 当作默认回退。统一入口会在导入应用前设置 `AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime 和显式临时 SQLite，并清除继承的 Provider 密钥：

```powershell
python scripts\run_backend_tests_isolated.py --verbosity 1
python scripts\run_backend_tests_isolated.py tests.test_database_migration tests.test_database_migration_recovery --verbosity 2
python scripts\run_backend_tests_isolated.py tests.test_source_monitoring_operations_migration --verbosity 2
```

迁移工具不启动 HTTP 服务，不访问 8770，不连接 Futu/OpenD，不调用模型 Provider，也不拥有投注、钱包、订单或任何外部执行能力。
