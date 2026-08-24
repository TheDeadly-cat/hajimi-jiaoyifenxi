# AI 共创室新聊天交接（2026-08-21）

本文给“交易分析”工作区中的新 Codex 任务使用。它整理当前代码、历史验证、正式环境冻结项和下一步，不是正式数据库迁移授权，也不是 Provider、Futu/OpenD 或外部数据连接授权。

## 产品位置与不可越过的边界

- 本项目是 local-first 的多 AI 群聊研究与决策记录工作台；证据、版本、治理、用户决定和行动记录彼此分层。
- 足球与股票能力均为版本化只读研究包。系统不投注、不接钱包、不下单、不执行交易，也不替代用户决定。
- 没有真实匹配校准时，足球包不生成未来胜率；赔率只能作为带时间戳的代理证据。
- 通用股票包使用房间显式股票池，并逐项检查 Futu、SEC、IR、复权与公司行动；它不扩写 `storage_research_readonly` v1，也不能进入存储股票 candidate experiment。
- 通用 orchestrator 只消费 versioned round-context provider，不包含足球、股票或 TradingAgents 领域逻辑。
- 项目不是 Git 仓库。不要初始化 Git、清理用户文件、reset、commit，或创建遗漏当前文件的干净 worktree。

## 已实现的主目标

### 1. 正式 SQLite 迁移硬门

- `server.py` 先取得 owner lock，再调用只读启动门；启动不会运行 `StudioStore._initialize()`，不会静默建表、加列、seed 或回填。
- `backend/database_migration.py` 实现 `preview -> prepare -> apply -> reconcile`：只读预检、闭合迁移清单、精确备份与候选库、prepared SHA 授权、迁移后 integrity / foreign key / sidecar / 物理 SHA / 逐表逻辑 SHA 核验。
- `backend/database_migration_commit.py` 提供 Windows ReplaceFileW、SQLite 锁区租约、hash-chain intent 与崩溃恢复协议。
- 正式 `apply` 从未执行。永久源码备份目录已经选为 `Z:\ai_collaboration_studio_backups`，但它不是正式 migration backup 目标，也不构成迁移授权；只有用户另行选择该次 migration backup 的精确目标、审阅 manifest/prepared/backup 哈希并显式给出授权 token 后，才可讨论执行。

### 2. `football_research_readonly`

- v1.0.0 manifest v2、domain adapter、host-owned UI contribution、HTTP inspect 与正式轮 context 已接通。
- 合同封印联赛/赛季、比赛 ID、开球 UTC、场地、精确赛程/密度/旅行/主客序列、阵容/伤停/停赛及发布时间、战术、近期表现和数据截止时间。
- 证据严格区分 `official_fact / media_report / model_inference / odds_proxy`；材料版本、正文与快照哈希进入只读核验。
- `probability_state=withheld_no_calibration`，未来概率不可见，赔率明确为 proxy-only；无投注、钱包、下注或自动决定能力。

### 3. `stock_research_readonly`

- 独立 v1.0.0 只读包；房间持久化并封印 `stock_room_scope_v1`。
- 对每个标的核验 Futu、SEC、IR、price adjustment、corporate actions 与统一 cutoff。
- 复用宿主证据图、治理、Action Desk 与用户决定链，但不声明 storage candidate/simulation capability。
- candidate experiment 同时检查当前房间与冻结 artifact 的领域包，football/stock 混合快照不能绕门。

### 4. 宿主与工程能力

- 重型前端页面使用 `React.lazy + Suspense`，CSS 按闭合组件所有权拆分；通用 primitives 仍 eager。
- 弹窗/抽屉使用共享 modal focus 合同：初始焦点、双向 Tab、Escape、busy fail-closed、精确触发器恢复与嵌套 top-surface 仲裁。
- Room Inspector 已按“准入 -> 收敛 -> 首要阻塞 -> 下一步 -> 用户决定边界 -> 配置细节”重排；普通等待/离线/未配置使用中性或注意色，红色保留给完整性和只读边界故障。
- `frontend/src/visualViewport.js` 发布完整 visual viewport 几何与缩放变量；移动抽屉、dialog、Composer、12 人提及菜单、safe-area footer 与 44px 触控目标已覆盖代码级合同。
- 前端测试统一由 `frontend/scripts/run-tests-safe.ps1` 按文件、单并发、内存/输出/超时/进程树守卫执行。不要直接运行 `node --test`；旧的顶层 Vite/JSDOM 未决 lazy gate 曾导致 Windows/Node 24 极端内存膨胀，现已改成无副作用静态契约。
- 后端有 `migration / core / domains / full` 四层隔离测试；源码可用闭合 manifest 的无 Git 版本化 ZIP 工具备份。

## 2026-08-21 当前只读运行证据

- 正式主库：`runtime/collaboration_studio.sqlite3`
- main 大小：`5,062,656` bytes
- main mtime UTC：`2026-08-11T16:33:59.1145347Z`
- main SHA-256：`B32E88A0C0BE5DB2D052904221C6C85D1B1C7862FD76F45EB8DF08B7EC41CC05`
- WAL：存在，`0` bytes；SHM：存在，`32,768` bytes。未删除、修改或 checkpoint。
- `8770`、`11111`、旧隔离 QA 端口 `18787` 均无 listener。
- 无项目相关 Node/Python 测试或 QA 残留进程。
- `.git` 不存在，`git rev-parse` 返回 not a repository。
- 本次核验未连接 SQLite、未启动或停止服务、未访问 Provider/Futu/SEC/IR。

## 验证证据应如何表述

- 2026-08-12 的历史完整后端隔离基线见 `docs/completion_audit_2026-08-12.md` 与 `docs/offline_regression_evidence_2026-08-12.md`；它们是 dated snapshot，不是 2026-08-21 当前重跑。
- 最近一次在本线程确认的前端完整回归为 `416/416`，Vite 为 `1688 modules`；之后又加入前端 safe runner hardening，因此新任务必须用受控入口刷新当前数字，不能把旧数字直接写成 current。
- 2026-08-21 已重新定向验证 `tests/testRunnerSafety.test.js`：`2/2` 通过，受控 runner 成功回收进程并返回。
- `frontend/dist` 中现存 main JS `495,708` bytes、eager CSS `143,101` bytes、RoomInspector JS `111,270` bytes；这是已有构建产物，不替代新任务的 production build。
- 代码级响应式合同与 320px/等效 200% 浏览器证据已经存在，但原生浏览器 200% 页面缩放、真实手机软键盘/IME 和真实屏幕阅读器仍未得到充分证明。

## 第一批工作执行状态（2026-08-24）

1. 前端受控全量通过 `105` 个测试文件、`545/545`；production build 为 Vite 6.4.3、
   `1747 modules`，主 JS `373.85 kB`、gzip `112.42 kB`。未恢复旧 Vite/JSDOM
   顶层 harness 或未决 lazy gate。
2. 隔离浏览器验收已覆盖 1440x900、320x568 和有效 200% 的 720x450 CSS 视口；横向
   溢出、12 人点名菜单、`visualViewport`/safe-area、modal focus 均通过，最终 console
   warning/error 为 0。
3. `docs/frontend_continued_ux_performance_2026-08-12.md` 已区分代码/浏览器证据与尚未
   覆盖的真实 IME、屏幕阅读器和通用原生缩放边界。
4. AST 门禁修正后的当前完整后端隔离回归为 `1266/1266`、`886.679s`，失败、错误和
   跳过均为 0；网络审计为 350 次测试自建随机 loopback，阻断和子进程阻断均为 0。当前
   delivery 层为 `44/44`，离线静态安全基线为 `7/7`；AST 门禁允许限定调用
   `sqlite3.connect`，但负例继续拒绝 `socket.create_connection`。release lifecycle
   已覆盖 7 份归档的 6 个相邻 source 过渡；当前外部 bootstrap 对 8 份归档的静态预检
   为通过 3、因旧归档缺少全哈希 Python lock/当前项目标记而拒绝 5。最新永久归档绑定的
   551 文件干净源投影还完成了真实 fresh-source smoke：10 个 Python 包解析匹配锁，
   npm 安装 106 包，后端定向合同 `6/6`、前端 `545/545`，Vite `6.4.3` 转换 1747
   modules 并完成 production build；随机回环 startup/readiness/version/frontend/404
   合同通过，临时工作目录已删除，正式库和受保护端口前中后不变。依赖 inventory 也已
   纳入证据，但上述结果仍不证明真实 Provider/市场连接、数据库升级兼容、人工辅助技术
   验收、正式迁移或生产发布授权。
5. 永久源码目录已选为 `Z:\ai_collaboration_studio_backups`，并已通过版本化归档器和
   `source_backup_manifest_v1` verifier 离线校验。后续每批代码或文档变化仍需新建而非
   覆盖归档。

下一批工作继续以 `docs/release_delivery_verification_2026-08-24.md` 的“尚未完成或未授权”
为边界。不得自行执行正式 migration、连接真实 Provider/Futu/OpenD/SEC/IR、实现空 P28，
或把源码备份位置解释为 migration backup/授权 token。

前端受控回归入口：

```powershell
cd C:\Users\Administrator\Documents\交易分析\ai_collaboration_studio
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run build
```

后端如需验证，统一使用：

```powershell
python scripts\run_backend_tests_isolated.py --layer migration --verbosity 2
python scripts\run_backend_tests_isolated.py --layer core --verbosity 2
python scripts\run_backend_tests_isolated.py --layer domains --verbosity 2
python scripts\run_backend_tests_isolated.py --layer delivery --verbosity 2
```

## 权威入口

- 迁移：`server.py`、`backend/database_migration.py`、`backend/database_migration_commit.py`、`scripts/run_database_migration_gate.py`、`docs/database_migration_gate.md`
- 领域包：`backend/capability_packs.py`、`backend/football_research.py`、`backend/football_research_service.py`、`backend/stock_research.py`、`backend/stock_research_service.py`、`backend/domain_adapters.py`、`backend/round_contexts.py`
- 前端：`frontend/src/App.jsx`、`frontend/src/components/RoomInspector.jsx`、`frontend/src/components/FootballResearchPanel.jsx`、`frontend/src/components/StockResearchPanel.jsx`、`frontend/src/visualViewport.js`、`frontend/src/capabilityContributions.js`
- 测试：`frontend/scripts/run-tests-safe.ps1`、`scripts/run_backend_tests_isolated.py`、`scripts/backend_test_layers.json`
- 证据：`docs/completion_audit_2026-08-12.md`、`docs/offline_acceptance_evidence.md`、`docs/frontend_continued_ux_performance_2026-08-12.md`、`docs/source_backup.md`

## 2026-08-24 追加核验：只读依赖 CVE 审计

1. Python 锁文件经 pip-audit 2.10.1 对 PyPI 与 OSV 双源查询，均覆盖 10 个锁定分发项并报告
   0 个已知漏洞。npm 锁文件经 npm 11.13.0 audit --package-lock-only --ignore-scripts
   查询，报告 nanoid 3.3.16 的 high 告警 GHSA-2v37-7h3g-55p8 与
   postcss 8.5.20 的 moderate 告警 GHSA-fxqj-rqcc-2cmp。
2. 两项均为 Vite 6.4.3 -> postcss 8.5.20 -> nanoid 3.3.16 下的 dev-only 传递依赖。
   定向源码搜索无相关 API 或配置引用；未证明 Nano ID 攻击输入可进入应用运行时，PostCSS
   仅在构建流程摄入不可信 CSS 且未设置 from 时保持条件可达。锁文件告警仍成立，不按误报关闭。
3. 本轮未执行 npm audit fix，未修改 manifest/lock，扫描器与原始报告只位于系统临时目录。
   建议另行授权把 PostCSS 升到至少 8.5.23、Nano ID 3.x 升到至少 3.3.18，并完整复验。
4. 持久化证据为
   [docs/evidence/dependency_cve_audit_2026-08-24.json](evidence/dependency_cve_audit_2026-08-24.json)，
   8659 B，SHA-256 3edca35b0410fc5b00acb04a9f0cb2491b24899b7f6f11dd0a2185f9d92d1a5f。结果受审计时间与漏洞数据库覆盖范围约束，不代表
   未知漏洞不存在、完整安全审计、SBOM 认证、真实市场连接或生产发布授权。