# 隔离交付与发布生命周期验证（2026-08-24）

本文记录当前工作树在安全本地边界内的交付证据。它区分当前定向验证、较早完整
回归、真实浏览器验收和仍未覆盖的人工或生产门槛。测试通过不等于正式迁移、真实
Provider 就绪、历史版本兼容或生产发布授权。

## 当前结论

- 前端交互、响应式布局、构建和宿主静态交付已有可复核证据。
- 后端应用完整回归已有通过基线；本批新增的 release lifecycle 由当前定向测试覆盖。
- 源码可以闭合归档、离线校验、不可覆盖安装、原子激活、升级、失败注入和显式回滚。
- 演练不安装依赖、不启动应用、不迁移数据库、不读取正式 SQLite，也不连接外部网络。
- 项目仍不是 Git 仓库；本批没有初始化 Git，也没有 clean、reset、commit 或覆盖工作树。

## 前端证据

### 代码与构建

- 安全入口 `frontend/scripts/run-tests-safe.ps1`：105 个测试文件，`545/545` 通过。
- Vite 6.4.3 production build：`1747 modules`。
- 主 JavaScript bundle：`373.85 kB`，gzip `112.42 kB`。
- 讨论审计列表使用完整规范化身份与 occurrence 组成稳定键，精确重复项保持唯一。
- Composer 的可操作项使用原生 `<button>` 键盘语义；结构复核面板采用受控展开与渐进窗口。

### 真实浏览器

- `1440x900`、`320x568` 和 `720x450` 有效 200% CSS 视口均无横向溢出。
- 12 人提及菜单、`visualViewport`、safe-area、modal 焦点约束和关闭后焦点恢复已检查。
- 渐进窗口可从 `12/16/8/12` 展开到 `25/33/17/25`。
- 最终 Browser warning/error console 记录为 0。

这些证据不等于真实输入法组合事件、实际屏幕阅读器朗读顺序或所有浏览器原生 200%
缩放组合均已验收。上述三项仍需要人工辅助技术测试。

## 后端与交付证据

### 回归证据的时间边界

- AST 门禁修正后的当前工作树完整后端隔离回归为 `1266/1266`，`886.679s`；
  失败、错误和跳过均为 0。该结果替代此前 `1264/1264` 的变更前基线。
- AST 门禁已把限定调用 `sqlite3.connect` 与主动套接字探测区分开，同时负例继续拒绝
  `socket.create_connection`。当前 `delivery` 层仍为 `44/44`，`26.136s`；它是
  可单独重跑的分层子集，不与完整回归总数相加。
- `full` discovery 已包含 release lifecycle、dependency inventory verifier 和当前全部
  `tests/test_*.py`，本次实际执行 1266 项。
- 完整回归网络审计：350 次测试自建随机 loopback 连接，阻断尝试 0、子进程阻断尝试 0，
  模拟离线连接 0，非回环连接保持 fail-closed。
- 原始系统临时日志 `ai-studio-full-backend-84700235dd9741c7b8557b9ab6094adc.log`
  为 `76,045 B`，SHA-256
  `80d8a6ce584503e8a93b00ca4c379c599feeadd128424f3fdb546f0ca2414e08`。
- 持久化精简证据为
  [`docs/evidence/full_backend_regression_2026-08-24.json`](evidence/full_backend_regression_2026-08-24.json)，
  `2,200 B`，SHA-256 `5cd7112eb63f98d6568b767c79d92f37c4c274dd7bbf4a3a1aa6a8f3c015a7ac`。
- 当前 `delivery` 层以本节首段修正后的 `44/44` 为准；dependency inventory verifier
  定向合同历史结果为 `12/12`。
- 当前离线静态安全基线：`7/7`，高置信凭据 findings 0。

`1266/1266` 是当前工作树的完整后端证据；`44/44` 和 `12/12` 仍只是可单独重跑的
分层/定向子集，不能与完整总数相加。

### 宿主与干净源码交付

- `/api/readiness`、`/api/version` 与 JSON unknown-API 404 已有隔离运行证据。
- launcher 在应用启动前执行 readiness gate。
- Python 依赖使用精确版本和 SHA-256 锁；GitHub Actions 使用 40 字符 commit pin。
- bootstrap 和 fresh-source smoke 使用系统临时 runtime、显式临时 SQLite 和随机回环端口；
  fresh-source smoke 对 8770、11111、18787 使用 Windows IP Helper 被动 listener 查询，
  不向受保护端口发起 `connect` 或 `connect_ex`。
- JSONL 宿主/HTTP 日志经过边界化与敏感值检查；这不是完整 SAST、CVE 审计或渗透测试。
- AST 误报修正后的当前离线静态安全基线为 `7/7`：扫描 548 个发布源文件和 540 个文本
  文件，高置信凭据/私钥发现 0，网络请求 0；被动端口门禁确认 fresh-source 3 次、release
  drill 2 次状态检查，并确认 release 主动套接字调用为 0。闭合报告为
  [`docs/evidence/static_security_passive_port_gate_2026-08-24.json`](evidence/static_security_passive_port_gate_2026-08-24.json)，
  `2,654 B`，SHA-256
  `a5d38341c1fea4d0bd3e5628286820820fe7e16fb1b82ea11ebac04b2ed98b1f`；报告继续明确
  `sast_complete=false`、`dependency_cve_audit=false` 和 `penetration_test=false`。
- 最新永久源码归档绑定的 551 文件干净投影已完成真实 fresh-source smoke：10 个 Python
  锁定分发项解析与 resolution SHA 完全匹配，npm 安装 106 包；后端定向合同 `6/6`，
  前端 105 文件 `545/545`。Vite `6.4.3` 转换 1747 modules，主 CSS 为
  `193.81 kB / gzip 36.02 kB`，主 JS 为 `373.85 kB / gzip 112.42 kB`，production
  build 为 `8.13s`。随机回环端口 64367 的 readiness/version/frontend/unknown-API
  分别返回 200/200/200/404，server stderr 0 B；临时工作目录已删除，正式库与
  8770/11111/18787 前中后不变。持久化摘要为
  [`docs/evidence/fresh_source_smoke_2026-08-24.json`](evidence/fresh_source_smoke_2026-08-24.json)，
  `5,891 B`，SHA-256 `19923909cf9326c3c59b92d8a5985f77474c3fe64a22dd282c8a5c1808a0ccf6`。该结果仍不证明真实
  Provider/市场连接、CVE 审计、人工浏览器辅助技术验收或生产授权。

### 离线依赖 inventory

- `dependency_inventory_v1` 从精确 Python hash lock 和 npm package-lock v3 确定性生成。
- 当前组件数为 165：Python 10、npm 155；npm 直接运行依赖 3、直接开发依赖 3、
  传递依赖 149。
- CI 与本地安全入口均执行 generate 后再 `--verify`；verifier 校验闭合结构、内部
  SHA-256，并从当前权威锁重建后逐字段比较。
- 当前 inventory SHA-256 为
  `d96f6f72248885df48df2df00ade41b3a7d76861c79301c8ee6b66f5ece81c01`；
  JSON 文件 SHA-256 为
  `bfe614af612b0166f65f29c1137e85f932dae2a8a1ac8cd7c7437e6fb5b76a72`。
- 报告不含绝对源码路径，不查询 registry，不安装依赖，不发起网络请求。
- `vulnerabilities_evaluated=false`、`licenses_evaluated=false`、
  `sbom_standard_conformance_claimed=false`；整体哈希也不是数字签名。

## Release lifecycle 演练

当前入口为 `scripts/run_isolated_release_drill.py`，CI 报告名为 `release-drill.json`。
受保护端口状态通过 Windows IP Helper API 被动读取 IPv4/IPv6 listener 表，不绑定端口，
也不使用 `connect` 或 `connect_ex` 探测。

独立演练结果：

- 安装两个 manifest 校验的源码版本，`release_count=2`。
- 初始激活 generation 1，升级 generation 2，显式回滚 generation 3。
- 重复安装被阻断，陈旧 generation 激活被阻断。
- 注入闭合的 `DRILL_INJECTED_NOT_READY` 失败 receipt 后，只允许回滚到精确上一版本。
- 最终 active release 回到合成 baseline。
- 外置临时 SQLite 主文件、WAL 和 SHM 的字节数及 SHA-256 前后完全一致。
- 8770、11111、18787 在演练前后均无 listener。
- `application_started=false`、`dependency_installation_performed=false`。
- `database_migration_executed=false`、`formal_database_opened=false`。
- `external_network_requests=0`、`autonomous_release_authority=false`。
- 默认合成演练仍为 `historical_upgrade_compatibility_proven=false`。

这只证明当前 release lifecycle 原语在合成双版本场景中失败关闭。它没有使用真实历史发布包，
也没有证明任何正式数据库 schema 可以升级或回滚。

### 连续真实源码归档 exact-pair 演练

在默认合成演练之外，另以两份连续、已离线校验的真实源码归档执行了一次系统临时目录
演练：

- baseline release：`20260823T184901Z-a2a5dcf8f198`，源码总 SHA-256
  `a2a5dcf8f198a58cd2b7c714321732f20964c29e28ef86bec8446e540ab0a94b`。
- current release：`20260823T185400Z-60716cd41378`，源码总 SHA-256
  `60716cd41378188cd612be8b61abd3a4bf0cb82c7e39e58705a5343607234bb8`。
- 两个 manifest 均经 install 重新校验，随后完成 baseline generation 1、current generation 2、
  baseline rollback generation 3。
- Python 的 `connect`、`connect_ex`、`create_connection` 和 DNS 解析入口均被失败关闭探针包围；
  实际调用次数为 0。
- 显式系统临时 SQLite 主文件、WAL、SHM、journal 创建数为 0。
- 应用未启动，依赖未安装，数据库迁移未执行，正式数据库未打开。
- 报告 `historical-source-release-drill.json` 为 `2,081 B`，SHA-256
  `f9228311ea1e6a43b6840442a06ced490b60b96892c3a0d042446b79378b9b2f`。

该证据只把结论提升为：上述精确源码归档对的 source release lifecycle 安装、激活、升级和
回滚已经通过。它不证明其他历史版本、应用运行行为、依赖兼容、数据库 schema 升级或正式
生产回滚兼容。

### 当前可取得归档的相邻历史矩阵

在用户授权的只读 manifest 对比边界内，2026-08-24 又对 C、Z 两处现存的全部 7 份
版本化源码归档完成离线验真，并按 `backup_version` 覆盖全部 6 个相邻过渡：

| 序号 | baseline → current | manifest 新增 / 修改 / 删除 |
| --- | --- | --- |
| 1 | `20260812T135952Z-b47eb401a23b` → `20260812T140543Z-307418d42388` | `0 / 5 / 0` |
| 2 | `20260812T140543Z-307418d42388` → `20260812T140631Z-48b029a585ac` | `0 / 1 / 0` |
| 3 | `20260812T140631Z-48b029a585ac` → `20260812T141544Z-d075f0771cc2` | `2 / 2 / 0` |
| 4 | `20260812T141544Z-d075f0771cc2` → `20260812T144214Z-a85889aafc3d` | `3 / 4 / 0` |
| 5 | `20260812T144214Z-a85889aafc3d` → `20260823T201640Z-3574206b065b` | `147 / 107 / 136` |
| 6 | `20260823T201640Z-3574206b065b` → `20260823T202121Z-de0086296fed` | `0 / 2 / 0` |

每个过渡都重新校验两份 archive/manifest 和安装后逐文件哈希，随后完成 generation
`1 → 2 → 3` 的初始激活、升级和精确上一版回滚；重复安装与陈旧 expected-active 激活均被
阻断，最终 pointer 恢复到该过渡的精确 baseline 源码哈希。六组使用互相隔离的系统临时
release root；临时 SQLite 文件创建数、socket 联网调用和子进程启动数均为 0，应用、依赖
安装、数据库迁移和正式数据库均未启动或打开，8770、11111、18787 前后无 listener。

闭合报告为
[`docs/evidence/historical_source_release_matrix_2026-08-24.json`](evidence/historical_source_release_matrix_2026-08-24.json)，
`125,560 B`，SHA-256
`e1a1a2ddefb7a6f8d445a30c97a0a49d0c840a9409ba8dd4ad33e89a22ba5e1d`。报告保存每份
archive/source 身份、完整 manifest 变更集及其哈希、每次 lifecycle receipt 结果和闭合边界，
不包含绝对磁盘路径或凭据赋值。

这把证据提升为“当前可取得七份归档的相邻 source release lifecycle 矩阵通过”。它仍不证明
更早或缺失版本、应用运行、依赖环境、数据库 schema 升级、正式数据回滚或生产发布兼容。

### 历史 bootstrap 与依赖锁静态预检

source lifecycle 矩阵持久化并生成新归档后，可取得归档数增加为 8。为避免直接运行旧源码，
2026-08-24 使用当前已审阅 bootstrap 作为外部评估器，对全部 8 份 archive 重新验真并安全
解包到相互隔离的系统临时目录，只执行项目标记、Python/npm 锁和 check-only receipt 解析。
进程内同时阻断 socket 与子进程入口；归档自身的 bootstrap、server、测试或其他 Python/JS
代码均未执行。

- 5 份 `20260812T*` 归档均缺少 `requirements-lock-win-py314.txt`，也没有当前
  bootstrap、fresh-source smoke、release drill 和受控前端测试入口；它们保留
  `run_backend_tests_isolated.py` 和含 116 个组件的 npm lock，但被当前 bootstrap 以
  `PROJECT_MARKERS_INCOMPLETE` 诚实拒绝。
- 3 份 `20260823T*` 归档具备全部当前项目标记和上述入口；全哈希 Python lock 为 10 个
  组件，npm lock 为 155 个组件，外部 check-only 为 `3/3` 通过。三份之间 Python/npm lock
  哈希均未变化。
- 从 `20260812T144214Z-a85889aafc3d` 到
  `20260823T201640Z-3574206b065b` 的过渡首次出现当前全哈希 Python lock，npm 组件数由
  116 变为 155；这是一条静态依赖合同分界，不是依赖解析或运行兼容证明。
- 整个预检没有下载或安装依赖、创建 runtime/SQLite、启动应用、迁移数据库、打开正式库、
  发起网络请求或启动子进程；受保护端口前后均无 listener。

闭合报告为
[`docs/evidence/historical_bootstrap_preflight_matrix_2026-08-24.json`](evidence/historical_bootstrap_preflight_matrix_2026-08-24.json)，
`28,012 B`，SHA-256
`7a7fc48c769be2893c8eba2621244d2f6dfa9577fbff2885cfa2e5dc316db0be`。它证明的是当前
外部评估器对后三份归档的 lock parse/check-only 兼容，以及对前五份缺失合同的失败关闭；
它没有执行历史 bootstrap，也不证明依赖能安装、测试能通过、应用能启动或数据库能升级。

## 源码归档证据

写入本文前的代码状态归档：

- 文件数：`542`。
- 源码总大小：`13,110,871 B`。
- 源码总 SHA-256：`753429f548ee1cf3ad2a9d2187463e8034dc873f8f099284e388bd6aebb9efad`。
- ZIP 大小：`4,260,571 B`。
- ZIP SHA-256：`aa3567094a51baab050496f8a3c63cc542d88a724f37f920f742b91a1a315914`。
- 离线 verify：通过。

与上一份 540 文件干净归档的只读 manifest 对比为新增 2、修改 4、删除 0：

- 新增 `scripts/run_isolated_release_drill.py`、`tests/test_release_drill.py`。
- 修改 `.github/workflows/isolated-validation.yml`、`README.md`、
  `scripts/run_static_security_checks.py`、`tests/test_ci_delivery_contract.py`。

该归档仍位于系统临时目录，不是用户选择的永久备份位置。本文本身写入后需要再创建一份
新的闭合归档，最终归档身份应以外部离线 verifier 输出为准，不能自引用写入 ZIP 内部。

## 正式资产只读复核

- 正式主库：`5,062,656 B`，SHA-256
  `B32E88A0C0BE5DB2D052904221C6C85D1B1C7862FD76F45EB8DF08B7EC41CC05`。
- WAL：`0 B`，SHA-256
  `E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855`。
- SHM：`32,768 B`，SHA-256
  `FD4C9FDA9CD3F9AE7C962B0DDF37232294D55580E1AA165AA06129B8549389EB`。
- 8770、11111、18787 无 listener。
- 无项目 Python/Node QA 残留进程。

这些操作只读取文件元数据、SHA-256 和系统 listener/process 状态；没有打开 SQLite 连接。

## 永久源码保存（2026-08-24）

- 用户已明确选择 `Z:\ai_collaboration_studio_backups` 作为永久源码备份目录。
- 文档同步前基线归档为
  `ai_collaboration_studio-source-20260823T201640Z-3574206b065b.zip`：545 个文件，
  源码总大小 `13,148,334 B`，源码总 SHA-256
  `3574206b065bae2a7b19f589a219b7bb5ac0fb4a6500ebca072a5f0d360a2652`；ZIP 大小
  `4,273,859 B`，ZIP SHA-256
  `0b8fb56c06bd1637cb8a52511cd9812c435f013cdcfc9e86af5ac4c11a1533bb`。
- 项目 verifier 已离线重建并逐项核验 `source_backup_manifest_v1`；独立二次读取的 ZIP
  SHA-256 一致。归档位于 Disk 1 的 USB `WDC WD80 02FZBX-00C9HA0`（NTFS 卷
  `8T仓库`），与系统盘物理分离。
- 归档继续排除 runtime、数据库、环境文件、凭据、`node_modules`、`dist` 和
  `.npm-cache`。目录采用不可覆盖的版本化 ZIP；任何后续代码或文档变化都必须生成并
  离线校验新版本。
- 为避免源码文档对“包含自身更新的源码总哈希”形成自引用，最新归档身份以 Z 盘 ZIP
  内 manifest 和当次 verifier 输出为权威证据，而不是在源码中声称固定的“最终”哈希。

永久源码保存已经落地，但它不是正式数据库 migration backup，也不构成 migration
`apply`、Provider 连接或生产发布授权。

## 尚未完成或未授权

1. 正式数据库 migration `apply` 尚未执行，仍需用户审阅精确 prepared SHA、选择该次
   migration backup 的精确目标并提供显式授权 token。
2. 未连接真实 Provider、Futu/OpenD、SEC、IR 或外部市场服务。
3. 7 份归档的 6 个相邻 source lifecycle 过渡已经覆盖；静态 bootstrap 预检覆盖 8 份归档，
   其中当前合同通过 3、因缺少全哈希 Python lock/项目标记拒绝 5。更早或缺失版本以及依赖
   解析、应用运行、数据库升级和生产回滚兼容性仍未证明。
4. 真实 IME、屏幕阅读器和完整原生缩放矩阵仍需人工验收。
5. 静态安全基线不是完整 SAST、依赖 CVE 审计或渗透测试。
6. P28/plugin migration ledger 仍依赖首个真实 plugin-owned mutable schema v1→v2 用例，
   不使用空表或虚构需求提前实现。
7. 没有生产发布授权、自动发布权限或任何真实交易能力。

## 2026-08-24 只读依赖 CVE 审计

- 使用系统临时 pip-audit 2.10.1 分别查询 PyPI Advisory Database 与 OSV，并以
  --require-hashes --no-deps 审计 Windows/Python 3.14 锁文件；两次均覆盖 10 个锁定分发项，
  结果均为 0 个已知漏洞。
- 使用 npm 11.13.0 audit --package-lock-only --ignore-scripts --json --audit-level=low
  查询 npm registry；共报告 2 项：开发期传递依赖 nanoid 3.3.16 的
  GHSA-2v37-7h3g-55p8（上游 high，修复于 3.3.18）以及 postcss 8.5.20 的
  GHSA-fxqj-rqcc-2cmp（上游 moderate，修复于 8.5.23）。
- 锁图为 Vite 6.4.3 -> postcss 8.5.20 -> nanoid 3.3.16，两项均标记为 dev-only。
  对前端源码、脚本与配置的定向搜索未发现 nanoid、customAlphabet、customRandom、
  postcss 或 sourceMappingURL 的直接引用。Nano ID 的攻击者可控 size=0 运行时路径
  未被证明可达；PostCSS 仍保留“构建流程摄入不可信 CSS 且未设置 from”时的条件性文件读取风险。
  这不是误报撤销：两项锁文件漏洞成立，只是当前项目可达性低于上游通用场景。
- 未执行 npm audit fix，未修改 manifest/lock，未在项目目录安装扫描器，也未执行无限循环或
  任意文件读取 PoC。后续应另行授权将 PostCSS 升至至少 8.5.23、Nano ID 3.x 升至至少
  3.3.18，再重跑审计、安全前端回归、production build 与浏览器验收。
- 机器可读证据：
  [docs/evidence/dependency_cve_audit_2026-08-24.json](evidence/dependency_cve_audit_2026-08-24.json)，
  8659 B，SHA-256 3edca35b0410fc5b00acb04a9f0cb2491b24899b7f6f11dd0a2185f9d92d1a5f。该结论仅代表审计时漏洞数据库与已锁定依赖；
  不等于未知漏洞不存在、仓库级安全扫描、SBOM 认证、真实 Provider/市场核验或生产授权。
- 本节已替代上文 fresh-source smoke 段落中“CVE 审计尚未覆盖”的单项边界；该段其余边界不变。