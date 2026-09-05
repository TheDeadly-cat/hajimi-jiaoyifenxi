# Source Monitoring operations + Runtime convergence v2 runbook

## 范围与默认状态

本文件是 `source_monitoring_operations_v1` 持久化边界与 Runtime convergence v2 的运行、首次初始化、保留、迁移和回滚合同。它不扩大来源监控的权限：监控仍默认关闭、默认不自动启动、默认 dry-run；`execution_capability=none`、`live_trading_allowed=false`。本阶段没有新增信息源、Telegram/TDLib、ChatGPT 页面控制、任意 URL 抓取、Provider 基础监控、自动正式 round 或交易能力。

Runtime 构造本身零 I/O，并且不会创建或启用 adapter state。`official_only=1` 与 `allow_readonly_market=1` 可以同时存在；此时公开的 `SourceMonitoringRuntimeCoordinator` 保留两个独立 registry/scheduler，并让单个 worker 在全局 effective due 顺序中一次只运行一个 adapter。只有全局 `enabled=1`、`auto_start=1`，且某个代码注册 adapter 已通过本地操作员入口显式启用、config version 与初始化策略仍 current 时，worker 才可能轮询该 adapter。Adapter 开关可从 Source Inbox 的懒加载控制面或 owner-exclusive CLI 修改；打开面板、展开健康区或只读取 control 都不会轮询来源。

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
→ enabled && auto_start 时恢复 monitoring RUNNING rows 并初始化独立 pipeline
→ 启动单个非-daemon coordinator worker
→ startup ready
→ serve_forever
```

先绑定 loopback 端口、再恢复遗留任务，是现有数据库单实例合同的一部分。Coordinator 对两个 pipeline 的 effective due 做确定性全局排序；各 pipeline 的 checkpoint、持久化 due 与进程内 backoff 不合并。单个 adapter 失败只使 Runtime `degraded`，另一个 pipeline 仍可在下一串行循环运行；worker 顶层致命错误只留下有界错误码并投影为 `failed`。每次 poll 都获得绝对单调时钟 deadline 和共享 cancel event；官方默认 HTTPS 与 managed Futu Broker 都消费同一控制。

正常关闭顺序固定为：

```text
serve_forever 退出
→ startup ready=false
→ 设置宿主 shutdown event 与 Runtime cancel signal
→ server_close 排空 HTTP handler
→ 有界 join
→ 若首次停止失败，记录 critical 安全事件并执行第二次有界停止核验
→ 若仍未停止，fail-stop 并继续持有 owner；不得释放数据库单实例锁
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
| `from_time` | 还必须设置带时区 RFC3339 的 `AI_STUDIO_SOURCE_MONITOR_FROM_TIME`；该值现明确是 `initial_from_time`，时间标准化到 UTC 毫秒，只控制首次历史选择 | 首次成功后不再应用该 cutoff；后续默认正常处理新观测 |

`catch_up` 按 `occurred_at` UTC 降序、服务端 fingerprint 升序确定性选择；预览封印 adapter key、config version、起止 checkpoint hash、模式/上限、完整候选与选择后的 fingerprint 集。缺少确认哈希会在 worker 网络读取和 run receipt 之前失败；确认后来源证据漂移会在 Source Inbox 写入和 checkpoint 前失败。UI 授权会把精确 preview hash 与策略、起始 checkpoint 封印为 pending authorization；成功初始化在同一事务消费它，失败/degraded/dry-run 保留，disable 或 config migration 清除。环境与 UI 同时提供 catch-up hash 且不一致时失败关闭。

`seed_only` 与 `from_time` 的授权绑定模式、参数和起始 checkpoint，而不是延迟启动时的候选全集；首次成功运行分别以当时完整候选建立基线，或严格按已确认 cutoff 过滤。只读市场 adapter 首次只允许 `seed_only`，即使官方 pipeline 配置为 `catch_up` 或 `from_time` 也不会继承。Futu 的初始化 preview 只封印静态 broker/adapter policy、四股 allowlist、零执行边界和起始 checkpoint，不读取实时行情；确认启用同样执行 0 次 market/network call。Runtime 首次真正轮询可以看到不同的实时 snapshot，仍只建立 checkpoint、不导入历史信号，并在同一初始化 receipt 中同时记录已授权的静态 policy hash 和实际首轮 execution preview hash。

若操作员确实需要永久时间过滤，必须单独设置 `AI_STUDIO_SOURCE_MONITOR_CONTINUOUS_EVENT_CUTOFF`。它与首次模式独立、默认关闭。普通事件按 `occurred_at` 判断；明确的宏观或公司 IR 修订按本轮 source poll 的观察时间判断，因此不会仅因原始参考期早于 cutoff 而被静默丢弃。导入 item 的 `occurred_at` 和完整 JSON 保持不变，观察时间仍由 run/import receipt 提供，以保留 Source Inbox 指纹和崩溃重放幂等性。

三种模式都先用完整 Source Inbox 合同验证全部候选。首次轮询只要包含 source error 或 rejected item，就不导入、不提交 checkpoint、也不写 initialization receipt。dry-run 优先于所有模式：只返回 `would_seed/would_import`，不会建立基线。成功 initialization receipt 会把模式、catch-up 上限/from-time cutoff、计数、时间界限、checkpoint hashes 和 preview hash 封印到 `source_adapter_runs`；重启时策略漂移会在 poll 前失败关闭。

## 本地操作员 CLI

### 2026-09-05 正确性与兼容补充

SEC 首次 `seed_only` 通过 `poll_seed_baseline` 获取所选公司和表单在当前
`submissions.filings.recent` 中的完整有界 ID 集合；`per_symbol_limit` 只限制内容投递，
不再截断首次基线。UI 预览计数仍是本批内容数量，完整基线 ID 数可能更多。
畸形/截断数组、缺少来源、无法证明完整性或超容量都不能完成基线；有效空 recent 可以。
这不覆盖 SEC archive files，也不保证轮询间隔内出现后又从 recent 消失的记录可发现。

checkpoint 为 `sec_filings_checkpoint_v2`。已保存 ID 不因临时消失而删除，防止再次出现时
冒充新增；已保存 ID 与当前候选的并集超过 1,000 时返回
`SEC_CHECKPOINT_CAPACITY_EXCEEDED` 并保留 checkpoint，不自动清理。操作者需要明确选择
下一步保留/基线策略；不能通过改版本号或自动清空绕过容量门。

旧 v1 checkpoint 在来源请求前返回 `SEC_BASELINE_UPGRADE_REQUIRED`；旧 config hash 也会
显示 migration required。兼容方案是停止来源、确认没有 RUNNING、保存精确旧 config/state/
checkpoint 与初始化证据，在独立批准的迁移中使用既有仓储 `migrate_config` 以新配置及
显式空 checkpoint 重新建立基线，然后重新预览/授权。该仓储路径已在临时库验证保留
历史收件箱、房间、材料和运行记录；它不是可直接对正式库执行的授权或一键升级命令。
重新基线的分界会移动到新的首轮成功快照，停机区间内容需另行审阅。

Futu 的请求开始时间仍作为 poll/packet 运行身份；请求返回后独立采样本机接收时间，
严格校验来源更新时间与快照时间。异常/回退本机时间失败关闭，不放宽未来容忍窗口。
异动 item 的 `occurred_at` 是既有 `09:30 ET` 会话锚点，不等于行情更新时间。

Scheduler 选择绑定 adapter/config/state，仓储在开启运行事务时再次核验；普通启停或
配置竞争安全跳过，下一周期重选。该事务之后的活跃运行仍由既有 RUNNING/CAS 门保护。
监控面板显示的是当前启动进程配置；初始化状态需手动读取接入设置。未取得完整收据及
近期成功检查时，不因线程有心跳而显示“监控正常”。网页关闭后不提供浏览器通知。

本轮离线证据、公开发布前未完成项和回滚范围见
[`monitoring_correctness_delivery_2026-09-05.md`](monitoring_correctness_delivery_2026-09-05.md)。

### IR 首次基线补充及已授权现场结果

2026-09-05 在线读取准备期间，同样复现了 IR 单轮额度截断基线的问题。IR 现在也通过
`poll_seed_baseline` 保存首份完整、有界元数据范围的全部身份与投影；投递额度不再截断基线。
畸形/被解析器过滤的条目、缺失源和不完整元数据阻止初始化；有效空范围可以建立空基线。
`company_ir_checkpoint_v2` / `company_ir_config_v3_*` 封印每个来源的格式、范围和投影语义，
累计最多保存 250 个身份，临时消失不淘汰；并集超过容量时不推进 checkpoint。
旧 v1 返回 `COMPANY_IR_BASELINE_UPGRADE_REQUIRED`，必须通过已说明的显式停用、配置迁移和
重新基线方案处理；不自动删除收件箱、房间或材料。

用户已授权临时库中 SEC（NVDA 8-K）及 Micron IR 的两轮读取，间隔 5 分钟。
Micron 现有 RSS 路径两次在当前本机请求中返回 HTTP 404；间隔为 300.036 秒，两个运行均
为 DEGRADED，checkpoint 保持空、初始化未完成，收件箱和 Provider/正式轮次增量均为 0。
不能把这次失败观察称为线上来源通过。

官网新闻页及其直接引用脚本明确给出公开 GET
`https://investors.micron.com/feed/PressRelease.svc/GetPressReleaseList`。
一次 pageSize=2、bodyType=0 的有界核验返回 HTTP 200 和 2 行元数据；无需模型 API 密钥。
列表时间无时区；现有适配只取每条公告 head 内、URL 与标题匹配的 NewsArticle 显式时区时间，
绝不推测列表时区。Micron 默认改用固定 recent-30 JSON + 最多 30 个 head 元数据读取，
最多 4 个并发请求；不读附件、媒体或采集正文，不分页，也不声称全部历史完整。
单次列表上限 1 MB，每个 head 上限 128 KiB，沿用 Runtime 的整体 deadline/cancel。
任一条的时间元数据缺失或不一致时，完整基线失败并保留原状态。
JSON 项用 `company_ir_v2` 和独立 metadata/projection 哈希，旧 RSS 项与规则保持原语义；
JSON 当前只形成 neutral/no_match 影响映射，不冒用 RSS 专用规则。旧配置须显式迁移，
不自动清空 checkpoint；已有收件箱、房间和材料保留。实际两轮观察结果见交付记录。
最终 Micron 临时库两轮观察在默认 20 秒时限内均成功（11.004 / 7.709 秒，实际间隔
300.012 秒），首轮完整记住 30 条，重启后第二轮跳过全部 30 条历史，checkpoint 不变。
两轮均无新公告入库、无草稿/正式轮次/Provider 调用；这只证明该有限范围的连接与
基线重放，不证明真实新增延迟或长期在线闭环。
用户提供联系型身份后，SEC 在独立测试子进程中完成 NVDA 8-K 两轮读取：2.098 / 1.570 秒，
间隔 300.022 秒，首轮建立 63 条历史基线，第二轮识别全部重复且 checkpoint 不变。
未将联系方式写入项目配置或回执；付费模型测试仍需提前说明用途及费用，当前未发生。
真实双版本回退检查发现，旧 `67fdb4ad` 明确拒绝新 SEC/IR checkpoint，且不认识 Q4
sidecar，会令新项详情和未过滤收件箱列表返回不支持格式引起的记录错误；房间、既有材料
和旧项仍可读，SQLite 本身完好。因此只能回退到认识新格式的版本，不能仅切换源码指针。

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

## 宏观来源生产路径一次性观察

正式迁移或 soak 前，可以先在不打开 Studio 数据库的独立进程中检查当前 Fed、BLS 与 Treasury 宏观端点：

```powershell
python scripts\run_official_source_live_preflight.py --help
python -I -B scripts\run_official_source_live_preflight.py `
  --confirm RUN_OFFICIAL_SOURCE_LIVE_PREFLIGHT_ONCE
```

确认字符串在公共联网边界内再次精确校验；缺少、大小写错误或重复参数都在加载生产 transport 前失败。带确认的执行还强制要求 Python `-I` 隔离模式，并拒绝已经预载任何 `backend` 模块的进程；这是缩小路径注入面的进程卫生门，不是抗篡改证明。成功执行固定覆盖 `federal_reserve`、`bls_releases`、`treasury_releases` 与 `official_macro_calendar`，对八个固定端点各发起一次顶层 fetch，不做应用层重试、不覆盖系统代理、不关闭 TLS 或主机名验证。redirect/socket 层实际请求数没有被 transport 精确计量，因此回执只把 `endpoint_fetch_attempts_performed` 标为 exact，`network_requests_performed` 保持 `null/not_instrumented`。

输出是单行、闭集、最多 16 KiB 的 JSON，只含时间、计数、固定错误分类、source manifest/transport evidence profile 哈希和安全字段；不含 URL、正文、标题、路径、代理详情或原始异常。三个 SHA-256 只绑定预期配置与报告 profile，不证明响应来自哪个远端。当前代理若在 TLS ClientHello 后关闭连接会得到固定 `TLS_HANDSHAKE_EOF`，不得据此自动直连、切换端点或降级证书验证。

该命令显式标记 `scope=official_macro_only`、`sec_included=false`、`company_ir_included=false`。它不读取 `.env.local`，不导入 Futu 配置，不读写 SQLite/Source Inbox/checkpoint，不调用 Provider、模型或市场接口，也没有交易能力。一次 `passed` 是可信、未修改的本机隔离进程在该时间点经默认 HTTPS 路径获得可解析响应并完成四个 adapter 投影的观察，不是独立网络见证或密码学证明；因此回执固定为 `evidence_class=production_path_observation`、`transport_mode=default_official_https_path`、`live_network_attested=false`、`in_process_tamper_resistant=false`。它不证明 SEC/IR/Futu、24 小时连续性、官方内容真值、正式迁移、Provider、交易许可或发布验收。离线 injected fixture 回执使用不同 evidence class，正式 CLI 会拒绝将其当作生产路径观察。

SEC 与公司 IR 使用另一个同样无数据库的隔离入口。SEC User-Agent 必须由调用进程显式设置；入口在导入配置前强制跳过 `.env.local`，不会顺带加载 Provider/Futu 密钥：

```powershell
$env:SEC_USER_AGENT = 'AI Studio operator contact@example.com'
python -I -B scripts\run_sec_ir_live_preflight.py --help
python -I -B scripts\run_sec_ir_live_preflight.py `
  --confirm RUN_SEC_IR_LIVE_PREFLIGHT_ONCE
```

该观察固定执行一次 SEC ticker 表、由其中七个白名单标的 CIK 严格派生的七个 submissions 请求，以及四条代码固定公司 IR RSS feed；每个逻辑 endpoint 最多调用一次，不做应用层重试。此命令显式固定 `source_format=rss`，报告格式范围为 `explicit_legacy_rss_not_micron_json_metadata`，不覆盖新 Micron JSON 默认 Runtime。SEC JSON 与 IR bytes 分别受 2 MiB / 1 MiB 上限约束，错误只输出固定分类，绝不输出 User-Agent、URL、响应或异常正文。RSS transport 允许在代码固定 host 集内跳转，因此报告如实保留 `final_endpoint_identity_attested=false`，只给出保守 network request 上限；`passed` 仍只是 production-path observation，不是独立网络见证、来源内容真值或整体验收。

Futu 使用独立的无数据库、回环限定入口；官方来源宏观/SEC/IR 预检与官方 24 小时 soak
都不能替代它：

```powershell
python -I -B scripts\run_futu_live_preflight.py `
  --confirm RUN_FUTU_LIVE_PREFLIGHT_ONCE
```

它固定连接 `127.0.0.1:11111`，且只读取 MU、SNDK、WDC、STX 同批次快照；不接受
环境中的 Futu host/port，也不读取 `.env.local`。父进程以 15 秒 watchdog 回收卡住的
SDK 子进程。真实用户 `APPDATA/LOCALAPPDATA` 不会传给 SDK；锁定版 SDK 必需的日志目录
会收到父进程单次临时目录中的专用 profile 路径，worker 退出后才尝试整体回收；该路径
只在导入时检查，第三方实际写入和同用户竞态不属于 attestation。导入失败只返回固定
`FUTU_SDK_IMPORT_FAILED`。SDK 生命周期内的 Python stdout/stderr 与最终 JSON 协议通道
隔离，额外输出仍会令父进程失败关闭。回执精确计量本方的
probe/context/snapshot/可选 market-state/close 高层
调用，但把 SDK 内部传输诚实标为 `not_instrumented`。预检只接受
`requirements-lock-win-py314.txt` 当前固定的 `futu-api==10.10.7008`；兼容范围安装到其他
版本会在网络 I/O 前失败关闭。命令不启动或登录 OpenD，不访问
账户、持仓、资金、订单或交易接口，不读写数据库，不调用 Provider。一次 `passed` 也只
是该时间点的本机只读 production-path observation；Futu 仍不在下述 official soak 的
六个官方 adapter 验收范围内。

内部 worker 只生成 `watchdog_worker_observation`，并在自身导入前再次清洗环境；它不能
把公开 token 或环境标记冒充为 watchdog 证明。父进程只有在 15 秒及 16 KiB 边界内收到
并严格验证该中间回执后，才写入 `watchdog_parent_promoted=true` 并重新计算最终 receipt。

## 官方来源 24 小时 soak 证据包

soak 使用独立 CLI；它不扩展一次性 operator CLI，也不接受数据库路径、持续时间或小时数覆盖。v1 公共入口只允许 `official`，固定持续 24 小时、每 5 秒采样，允许的最大样本间隙为 120 秒。Futu 的交易时段/闭市跳过合同尚未纳入这一版，不能用 `official` soak 冒充 Futu 验收。

先准备一个已存在、为空且不经过 symlink、reparse point 或 hardlink 的证据目录，再执行。该目录及所有父目录必须由操作者控制访问权限，不能允许其他本机主体在 soak 期间写入、重命名或替换；路径重验会拒绝已存在和可观察到的别名/身份漂移，但 Python 标准库不能把 Windows 父目录句柄固定为完整的 open-relative 防替换安全边界，因此具备目录替换权限的主动本机攻击者不在 v1 证据模型内：

```powershell
$bundle = 'C:\protected-evidence\official-soak-20260903'

python -m backend.source_monitoring_soak_cli preview `
  --mode official `
  --bundle $bundle

python -m backend.source_monitoring_soak_cli start `
  --bundle $bundle `
  --confirm START_24H_SOURCE_MONITORING_SOAK `
  --preview-sha256 '<preview 输出的精确 SHA-256>'

python -m backend.source_monitoring_soak_cli verify `
  --bundle $bundle
```

`preview` 先取得与正式宿主相同的数据库 owner，复用只读迁移 readiness，在不初始化 schema 的前提下打开已验证 Store；它要求全局 enabled + auto-start、`dry_run=false`、impact rules 关闭、至少一个显式启用且 config current 的官方 adapter。随后只读封印 settings、registry、代码身份、数据库 startup/schema、完整 run baseline，以及每个 enabled adapter 的 config/state/checkpoint；非空 Source Inbox receipt 会从 canonical packet/receipt 与行级绑定字段独立重建，不能只信任库存储的 digest。SQLite main/WAL/SHM/journal 会逐个拒绝链接、reparse、非普通文件及身份漂移，并分别受 2 GiB / 512 MiB / 64 MiB / 512 MiB 上限约束；超限须先由单独维护流程处理，soak 不会静默复制。`preview` 不轮询来源、不写正式数据库，但会以 `O_EXCL` + 文件 fsync 新建 `baseline-inventory.json` 和最后发布的 `plan.json`，POSIX 还会同步父目录项。任一步失败都保留现场，不能覆盖或修补原 bundle。

`start` 在加载任何生产 runtime 前精确检查确认字符串与 preview SHA，再取得 owner 并重做 readiness、代码、settings、registry、adapter 和 baseline 绑定。只有重建计划逐字段相同才会创建 `ledger.jsonl`。生产 runtime 构造后会再次重算代码身份；worker 的首轮调度被 start gate 挡住，直到 `SESSION_STARTED` 已经 flush + fsync；之后 ledger 每条记录都有固定 session/runtime 身份、单调时钟、前序哈希和逐条 fsync。终态 run 还绑定完整 SQLite row SHA 与已独立重建的 Source Inbox receipt SHA。正常、失败或 Ctrl+C 关闭都先停止 runtime；有界 join 超时后仍继续持有 owner，线程真正退出前既不读取 final inventory，也不返回。runtime 确认静止后还会第三次重算代码身份，任何 24 小时内持续存在的源码漂移都会在写 `SESSION_ENDED` 前失败关闭。`final-inventory.json` 只能独占新建；账本缺少 `SESSION_ENDED` 时一律是 `INCOMPLETE_UNSEALED`，旧 session 永不续写。

`verify` 只读取四个固定 bundle 文件，不发现或打开数据库，不加载 settings，不连接来源。它重验计划、canonical inventory、账本结构/哈希链、精确 24h/5s/120s 策略、liveness、enabled adapter 覆盖、run row/receipt 和 baseline/final delta。即使 continuity、production binding 与 database 都通过，v1 仍固定输出 `source_acceptance_verdict=NOT_EVALUATED`、`overall_acceptance=NOT_CLAIMED`：这是同一可信本机进程生成的连续性/数据库证据，不是签名、独立网络见证、来源内容真值、SEC/IR/Fed/BLS/Treasury 分源语义验收、Provider/交易许可、PR 合并或公开发布证明。本功能加入代码与离线快进测试不代表已经执行过真实 24 小时 soak。

在 v1 `verify` 通过后，可对同一四件套运行独立的六源 operational acceptance：

```powershell
python -I -B scripts\run_source_monitoring_acceptance.py verify `
  --bundle $bundle
```

v2 只接受本机文件系统上的 bundle，并会在任何目录或文件检查前拒绝 UNC、Windows 设备命名空间与远程映射盘；不要把网络共享路径交给该命令。它要求 plan 中精确且仅有 `sec_filings`、`company_ir`、`federal_reserve`、`bls_releases`、`treasury_releases`、`official_macro_calendar` 六个 adapter；每个来源至少一条带数据库 row 绑定的 `SUCCEEDED`，且整个 session 中不能出现 `DEGRADED`、`FAILED`、`DRY_RUN*`、`ABANDONED`、rejected item、非空 source error、config drift 或市场调用。若 accepted item 非零，还必须有绑定的 Source Inbox import receipt。v1 与 v2 在同一遍已验证 ledger 流中完成检查，不按 pathname 再开第二遍。只有这些条件和 v1 continuity/production/database 三门都通过时才输出 `source_acceptance_verdict=PASS`；该 PASS 明确为 `operational_only=true`，仍固定 `content_truth_attested=false`、`independent_network_witness=false`、`overall_acceptance=NOT_CLAIMED`，不覆盖 Futu、Provider、正式迁移授权、交易、合并或发布。

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

`GET /api/monitoring/health` 仍是 `no-store`、只读、无探测接口。它使用稳定的 main/WAL 临时快照，不初始化 schema、不写 retention receipt、不轮询来源、不调用 Provider/市场。顶层合同升级为 `source_monitoring_health_service_v3`，保留 `operations` 子对象并新增进程内 `source_monitoring_runtime_health_v1`。初始化 receipt 或 pending authorization 与当前策略漂移时，持久化开关仍如实展示，但不会投影为 effective enabled：

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

控制面仅绑定当前 loopback 宿主、同一数据库 owner 与当前 Runtime registry catalog：

- `GET /api/monitoring/adapters/control`：懒加载、`no-store`，只读稳定数据库快照且零数据库写入、零外部来源/Provider/市场调用；返回代码 config、持久化开关、effective 状态、初始化状态与阻断码。
- `POST /api/monitoring/adapters/{adapter_key}/initialization-preview`：需要同源本机会话 token、严格 JSON 和精确 config/state；只允许未初始化且关闭的注册 adapter。官方来源可执行有 deadline/cancel 的固定来源预览；readonly-market 只返回静态 seed policy，行情与网络调用均为 0。两者都不写 state/checkpoint/Inbox。
- `POST /api/monitoring/adapters/{adapter_key}/enablement`：同样需要 token、严格 CAS 与精确确认。首次 enable 会重新 preview 后原子写入 pending authorization + enabled；disable 保留 checkpoint、run、initialization receipt 与 Source Inbox，仅清除未消费的 pending authorization。

没有 run-now/run-once HTTP route。control/preview/enablement 回执都固定声明 Provider/model/formal round 为零、`execution_capability=none`、`live_trading_allowed=false`；前端拒绝额外字段、非零禁区证据、与请求不绑定的回执，并在 mutation 后重新读取 control 与 health。`auto_start=true` 时，成功 enable 会令 adapter 到期，随后由独立 scheduler 正常执行；这不是 enablement handler 内部导入。

## Additive schema

受控 initializer 在既有 monitoring schema 后增加 operations 证明对象，并为 initialization receipt 增加：

- migration key：`source_monitoring_initialization_receipt_v1`；
- `source_adapter_runs` 的五个 additive 字段：`initialization_mode`、`initialization_config_version`、`initialization_preview_sha256`、`initialization_receipt_json`、`initialization_receipt_sha256`；
- initialization time/unique seal indexes、receipt 不可变 triggers 与 migration marker guards。

显式首次授权再增加一个独立 additive schema 单元：

- migration key：`source_monitoring_pending_initialization_authorization_v1`；
- `source_adapter_states` 的两个 additive 字段：`pending_initialization_authorization_json`、`pending_initialization_authorization_sha256`；
- 三个 migration marker UPDATE/DELETE/replace guard triggers。

pending JSON 是严格闭集；v1 含 adapter/config、初始化模式及参数、起始 checkpoint hash、preview hash 与确认时间，v2 静态 seed 另外绑定 authorization kind 与 source policy hash。读取时必须重新核对 canonical SHA-256、adapter/config/checkpoint 身份及 enabled invariant。Runtime convergence v2 的 pending authorization 与 initialization receipt 版本只复用上述现有 JSON/SHA 字段，没有新增列、表、索引、trigger 或 migration key。部分列、部分 trigger、marker/object 不一致或 seal 损坏全部失败关闭。

随后保留 Phase 8 operations 对象：

- migration key：`source_monitoring_operations_v1`；
- table：`source_monitoring_retention_receipts`；
- index：`idx_source_monitoring_retention_receipts_time`；
- receipt UPDATE/DELETE 拒绝 trigger，以及阻止 `INSERT OR REPLACE` 身份/封印碰撞的 INSERT guard；
- migration marker UPDATE/DELETE 拒绝 trigger，以及阻止 same-key/rowid replace 的 INSERT guard。

retention receipt 固定 `record_version=source_monitoring_retention_receipt_v1`、`policy_version=source_monitoring_retention_policy_v1`、`decision=RETAIN_ALL`、`eligible_rows=deleted_rows=source_rows_updated=0`，并保存 canonical JSON 与 SHA-256。retention 表不向 run/import/item 建立删除语义的外键；既有 Runtime schema 只 additive 增加上述 run 列，不 backfill、删除或改写历史行。initializer 使用迁移 manifest 注入的 `applied_at_ms`；不会在迁移中读取墙钟、启动 worker 或生成 receipt。每个版本化 schema 单元都在 SAVEPOINT 中逐条创建和精确验证，不使用会隐式提交调用方事务的 `executescript()`；调用方 rollback 会同时撤销 pending 业务写入与整个 schema 单元。

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
