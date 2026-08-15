# 离线验收证据索引

> 记录日期：2026-08-11。本文只索引已执行的隔离验证，不把静态检查或历史结果扩大解释为正式服务验收。后续代码变化后应重新执行相应命令。

## 2026-08-12 continuation addendum

The entries below are a newer isolated continuation record. They do not
rewrite the historical results above and do not authorize a formal migration,
Provider call, Futu/OpenD connection, or production-port startup.

### Current isolated backend and migration evidence

- Migration hard-gate/recovery targeted suite: `67/67 OK`, using a system
  temporary runtime and an explicit temporary SQLite database.
- Layered backend runs: migration `71/71 OK`, core `170/170 OK`, domains
  `106/106 OK`; plugin lifecycle `28/28 OK`; HTTP/domain round E2E `35/35 OK`.
- The prior complete backend baseline is `1204/1204 OK`. No production backend
  source was changed during this continuation; the browser run below used a
  separate temporary database.

### Current source-backup evidence

- A temporary versioned source archive was created and verified offline:
  `519` files, `25,341,661` bytes, aggregate SHA-256
  `e550f236fd38e45e545170aa591c25b593516ea59a164f8b09ac61af46f4a0b2`.
- The archive was intentionally kept under the system temporary directory.
  A durable external destination still requires an explicit user-selected
  path; this record is not a claim that a permanent backup exists.

### Current rendered browser evidence

- An isolated server on temporary port `18770` rendered the app as `AI 共创室`;
  the DOM was non-empty, screenshots were captured, and page console
  error/warning logs were empty.
- Football room creation exposed the versioned football pack and kept
  `开始一轮` disabled until contract inspection and explicit authorization.
- Stock room creation kept the submit button disabled until an explicit
  `stock_room_scope_v1` pool (`US:AAPL`, `US:MSFT`) was supplied; `{}` was
  rejected by the stock contract inspector.
- The temporary server was stopped after the run. Formal ports `8770` and
  `11111` had zero listeners at cleanup. The browser harness emitted one
  Statsig telemetry timeout, but the application page logs and isolated server
  logs remained empty; this is not counted as an application network call.

### Seeded positive-flow browser evidence

- A separate temporary database was seeded with exact local material versions
  for one football room and one stock room. The first attempt intentionally
  used a content hash including a trailing newline; the service rejected it as
  `材料内容哈希漂移`, confirming the material binding gate. The corrected
  fixture hashes the normalized stored content.
- The corrected run passed both positive flows: import the offline contract,
  execute the read-only inspection, and click `显式用于下一轮`. Both panels
  displayed the frozen-context authorization state and their non-executable
  safety disclosures.
- Switching from the authorized football room to the stock room cleared the
  previous authorization; the stock room returned to its explicit-inspection
  gate. Page console error/warning logs and server stdout/stderr were empty.
- This run used only temporary port `18772`, a system-temporary SQLite database,
  and local material fixtures. The server was stopped after verification; the
  formal ports remained unbound.

### Final isolated backend regression (2026-08-12)

- The current working tree completed the full discovery runner with `1204/1204`
  tests passing in `1132.038s`. The runner used system-temporary runtime
  `C:\Users\Administrator\AppData\Local\Temp\ai-collaboration-studio-tests-3y2iyvmp`
  and an explicit temporary SQLite database. No formal database, Provider,
  Futu/OpenD, or external network endpoint was used.
- The same working tree also passed the layered suites independently:
  migration `71/71`, core `170/170`, and domains `106/106`.

### Current source-backup refresh (2026-08-12)

- After the documentation and verification updates, a fresh temporary archive
  was created and verified offline: `519` files, `25,345,494` bytes, aggregate
  SHA-256
  `05128c49743be74ab5255ad70cd8ac0af81a60e9e08fcd3d7bb0042091e2bceb`,
  version `20260812T053338Z-05128c49743b`.
- The archive remained under the system temporary directory. No permanent
  backup destination was inferred or written without a user-selected path.
- After tightening the sensitive-file filter for deployment `.env.*`, common
  JSON/YAML credentials, tokens, and secret suffixes, the refreshed archive
  verified as `519` files, `25,346,837` bytes, aggregate SHA-256
  `7c1bbfbc8ef43c1a262b488bf29b10cfd99cfbb8a687456b57a4ac73d8d3296d`,
  version `20260812T053459Z-7c1bbfbc8ef4`. Source-backup tests remained
  `8/8`, and the complete core layer remained `170/170`.
- After that source-backup hardening, the full isolated backend discovery run
  again completed at `1204/1204 OK` in `903.131s`, using temporary runtime
  `C:\Users\Administrator\AppData\Local\Temp\ai-collaboration-studio-tests-jxmc_zsh`.
- The source-backup verifier now also reports the final ZIP `archive_size` and
  `archive_sha256`, in addition to the manifest content hash, so a future
  durable copy can be checked both semantically and byte-for-byte.
- The refreshed current-tree archive after that verifier change passed offline
  verification with `519` files, source bytes `25,348,444`, archive bytes
  `17,754,991`, manifest SHA-256
  `de28df0b03736d6cfc9245935962b1da841e99028a924ee724df87ef83800545`, and
  archive SHA-256
  `601344aab9d0730839c5667137cafb3dcc8df20f38748084de13c09aef5dec9e`.
  Source-backup and core validation remained `8/8` and `170/170`.
- The database owner-lock hardening now rejects symlink/reparse/hardlink lock
  paths without writing their targets. Its targeted test passed `5/5`; the
  migration layer passed `72/72` and core remained `170/170`.
- After that owner-lock change, the full isolated backend discovery run passed
  `1205/1205 OK` in `902.208s`, using temporary runtime
  `C:\Users\Administrator\AppData\Local\Temp\ai-collaboration-studio-tests-f3ym4lcr`.
- The current frontend tree then passed `317/317` Node tests and a Vite
  production build with `1673` transformed modules; the build emitted no
  large-chunk warning. The main entry remained about `481.39 kB` raw (`145.68
  kB` gzip), with heavy host surfaces emitted as separate chunks.
- The source-backup `preflight` command was exercised against a missing nested
  system-temporary destination: it reported the exact future version and
  `ready:true` while creating neither the destination nor its parent. An
  existing exact archive reports `ready:false` without overwriting it.
- A fresh current-tree archive after the destination-chain and immutable
  read-only URI hardening verified as `520` files, source bytes `25,365,957`,
  archive bytes `17,761,009`, manifest SHA
  `757c20ec0c6e3c3614dd882b719ad82a9b1ed6199f23905573283ed768d9967f`, and
  archive SHA
  `67ffb7198870706b7c663fbd3aad4b756b2a90bce571e23afdff5f6560a00ba7`.
  The verified archive was created only in a system-temporary destination;
  no permanent backup location was inferred.
- Source-backup tests passed `10/10`; core passed `172/172` after the
  destination-chain hardening. The preflight now rejects a normal-file
  component in an otherwise missing destination chain without modifying that
  file.
- After replacing the remaining domain/trace `mode=ro` opens with
  `mode=ro&immutable=1`, the targeted football/stock/trace/audit suite passed
  `32/32`; the refreshed core and domains layers passed `172/172` and
  `106/106` respectively.
- The migration preflight now rejects every existing SQLite sidecar,
  including zero-byte WAL, SHM, rollback-journal, directory, and symlink
  variants, without deleting the evidence. The migration layer passed
  `75/75` after this hardening; core remained `172/172`.
- The verified-startup resolver now independently rejects a supplied identity
  containing WAL/SHM/journal files, closing the direct configuration bypass;
  the migration layer passed `76/76` afterward.

### Current source-backup refresh after sidecar hardening

- A fresh temporary archive containing the sidecar hardening verified as `520`
  files, source bytes `25,372,859`, archive bytes `17,762,772`, manifest SHA
  `061d9d22c369f73a98780fe65d0e836b46d04e72bc6f2e83e784354905c9e287`, and
  archive SHA
  `2f6d7e2fdd0967dff4953772c70a982be4c3fe0af7e5a5742ff0155db2aa5be2`.
- After synchronizing the README and capability-pack catalog with the actual
  football/stock whitelist, a fresh temporary archive verified as `520` files,
  source bytes `25,374,483`, archive bytes `17,763,542`, manifest SHA
  `71de4f093eaf41a5ef455c7dfaa218b4c5968849618836eb9da2f368224fb709`, and
  archive SHA
  `8ed0de7b81b4dabd46f7903a6cdabce0202db9650ecc05ea5c8d97033463f670`.
- After the final immutable-read-only regression assertion, the current tree's
  temporary archive verified as `520` files, source bytes `25,375,455`, archive
  bytes `17,763,882`, manifest SHA
  `16de92d0e8cf90bc1b2207dbefa16b0df0f19c8655209cb05694ec494a36461b`, and
  archive SHA
  `bf6ee8d5d68da8f2f9e420b3b8afc1d700d6c62911cfbcc249e51423c904a7df`.
- After the hardlink startup identity hardening and its audit update, the
  latest temporary archive verified as `520` files, source bytes `25,376,957`,
  archive bytes `17,764,218`, manifest SHA
  `6289e9701bed46bdf433cf5476e8ee1e0c33b985ea376c033bc25cfe4b21da6d`, and
  archive SHA
  `6d88e4905cf12e8fda490bfb8ed86b0a8ac1d201ba73652276d46c95bb6d3711`.
- After the symlink/reparse source-path hardening, the latest temporary archive
  verified as `520` files, source bytes `25,380,750`, archive bytes `17,765,090`,
  manifest SHA
  `17f34ed5a9f523b390e12ffce0f81be458964dc5e44a1c231584ae01042bbb2f`, and
  archive SHA
  `442ab9f330ace5688b4ddfa226eefcd8900fbff79d5bd1d83d309abb370b58ae`.

### Final post-hardening full regression

- The current working tree completed the full isolated backend discovery run
  with `1213/1213 OK` in `909.471s`, using system-temporary runtime
  `C:\Users\Administrator\AppData\Local\Temp\ai-collaboration-studio-tests-e4evkmt3`.
  This includes the destination-chain and sidecar preflight negative tests. No formal
  database, Provider, Futu/OpenD, or external network endpoint was used.

## 运行边界

- 开发与测试使用 `AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime 和显式临时 SQLite。
- 未启动正式 8770；未连接 Futu/OpenD；未调用 Provider、市场数据或外部网络。
- 正式数据库仅使用 SQLite `mode=ro` 做完整性核对；没有写事务。
- 本次只读核验返回 `integrity_check=ok`、`foreign_key_check=0`，主库 SHA/大小/时间
  未变。Windows 对 WAL 模式数据库的只读打开可能留下 0 字节 `-wal` 与 32 KiB
  `-shm` 共享内存侧车；这不代表有待提交 WAL 数据，也不代表发生了迁移写入。
  后续核验应优先使用隔离副本或在确认无持有者后清理这类临时侧车。
- 固定安全边界继续为：`execution_capability=none`、`external_write=false`、`can_autonomously_decide=false`、`can_replace_user_decision=false`、`ranking_produced=false`、`winner_claim=false`、`user_final_decision_required=true`。

## 历史命令证据（2026-08-11）

### 后端

在项目根目录执行：

```powershell
python scripts/run_backend_tests_isolated.py --verbosity 1
```

该日版本完整运行结果：`1060 tests`，`OK`，耗时约 20 分钟。该结果覆盖 P23 candidate experiment、P24 registry artifacts、P25 lifecycle、P26 readiness、P27 round focus、Action Desk 核心/延续/总览、artifact/evidence 封印和 HTTP 安全。

Action Desk 相关的上一轮短回归为 `57/57`；本轮核心/延续/总览/HTTP 安全组合为 `40/40`，最新 `tests.test_action_desk_overview` 为 `10/10`，隔离 QA 脚本合同为 `3/3`。

### 前端（2026-08-11）

在 `frontend` 目录执行：

```powershell
npm.cmd test
npm.cmd run build
```

该日结果：前端 `287/287`；Vite `1667 modules` 构建成功。构建保留既有单 bundle 大于 500 kB 的 warning，不影响构建成功。

Action Desk 总览前后端共享 fixture 为 `tests/fixtures/action_desk_overview_v1.json`；后端值级校验与前端解析均直接使用该 fixture，避免字段名或聚合计数漂移。

## 隔离浏览器证据

使用 `scripts/run_isolated_action_desk_qa.py` 启动临时端口并通过应用内 Browser 验证：

- 打开“跨房间行动总览”，显示两个房间的已采纳行动；
- 文本筛选后列表收敛到目标房间；
- 从总览返回对应房间并定位 `inspector-action-desk`；
- 1280×760、760×760、390×844 三个视口均能保持总览可见；
- 控制台错误/警告为空。

该次隔离状态计数为：`action_desk_http_reads=1`、`action_overview_http_reads=1`、`action_transition_http_requests=0`、`provider_calls=0`、`market_reads=0`、`outbound_connect_attempts=0`。

## P23 候选实验浏览器补充验收

使用 `scripts/run_isolated_candidate_experiment_qa.py` 启动一次性临时服务，仅使用临时 SQLite、假市场和隔离治理快照。浏览器中打开 P23 产物，按顺序选择 MU 与 WDC 两个候选，勾选历史比较授权并运行一次实验；页面显示 `原子 cohort 完整性已通过`，同时显示共同数据封印、共同实验规格、520 个共同交易日、三档摩擦和两列并列历史指标。该操作未触发 Provider 调用，市场读取恰好 1 次。

同一页面在 1280×760、760×760、390×844 三个视口均保持实验结果与第三层用户最终决定区域可见；标题为“AI 共创室”，控制台 error/warn 为空。临时服务进程、浏览器标签页和临时目录已在验收后清理，51064 与 8770 均无监听。

ready-file 隔离边界与候选脚本回归：`tests.test_isolated_qa_scripts tests.test_candidate_experiment tests.test_candidate_experiment_http` 为 `15/15`，并通过 `py_compile scripts/run_isolated_candidate_experiment_qa.py`；当前版本后端全量为 `1060/1060`。本段涉及的改动仅为 QA 脚本、测试与证据文档，未改变产品运行时代码。

## 正式边界快照

只读核对结果：

- `collaboration_studio.sqlite3` SHA-256：`26E09547BDF350511C5D061D8671A1AC524839705522EFD0123002DF9A72C2E7`；
- WAL 大小：`0`；SQLite `integrity_check=ok`；外键违规：`0`；
- `provider_call_attempts`：`28`（DeepSeek `23`、豆包 `5`），本轮没有新增调用；
- 8770 监听：`0`。

## 下一阶段边界

跨房间总览先观察真实使用。P28 迁移账本只有在出现真实 plugin-owned mutable schema 的 v1→v2 用户用例后才实施；没有该用例时不创建空迁移框架，也不新增第三个 adapter port。TradingAgents 继续保持可选领域插件，不进入通用 orchestrator。
