# Futu 只读行情异常监控边界（阶段 4）

## 范围与来源身份

阶段 4 通过 `FutuReadOnlyBroker` 一次读取固定四股
`US.MU`、`US.SNDK`、`US.WDC`、`US.STX`。它不调用聚合的
`StorageResearchMarketService`，因此不会顺带读取日线、资金流、财报、SEC、IR、
FRED 或新闻。Broker worker 内部仍复用
`FutuUsMarketAdapter.quote_batch()` 的规范化和质量门，但 Studio 监控主进程不导入或运行
Futu SDK。

Futu 不是 Fed、BLS、Treasury、SEC 或公司 IR 一类官方披露源。适配器如实声明：

- `official_source=false`
- `source_class=readonly_market`
- `source_channel=futu_anomaly_monitor`
- `execution_capability=none`
- `live_trading_allowed=false`

`build_official_source_registry()` 仍精确包含原有六个官方适配器；阶段 4 使用独立的
`build_futu_anomaly_registry()`。生产 builder 只接受 `managed` 模式的精确
`FutuReadOnlyBroker`，且 host/port、四股、SDK 版本和操作白名单都由代码政策哈希封印。
构造 registry 不启动子进程、不导入 Futu SDK、不探测 socket、也不读取行情。

Futu 的首次 `seed_only` 授权使用 adapter 提供的静态
`initial_seed_policy()`。它绑定 config 哈希、Broker 政策哈希、四股白名单与永久零执行
边界，不包含实时 snapshot、candidate、行情时间或 next checkpoint。因此用户确认的
是静态初始化政策，不再要求两次实时行情哈希完全相同。

Source Inbox 要求公网 HTTP(S) 人工复核链接，因此事件使用代码固定的
[Futu OpenAPI 行情快照文档](https://openapi.futunn.com/futu-api-doc/en/quote/get-market-snapshot.html)，
不会写入本机 OpenD 地址。`source.content_sha256` 的语义是稳定的
“美东市场日期 + 规则”信号内容，不是该网页正文哈希，也不是原始行情快照哈希。

## 确定性规则与实时门

v1 只包含机械阈值，不使用模型：

| 规则 | 进入阈值 | 保持阈值 |
|---|---:|---:|
| `price_up_5pct` | 涨幅 ≥ 5% | 涨幅 ≥ 4% |
| `price_down_5pct` | 跌幅 ≤ -5% | 跌幅 ≤ -4% |
| `amplitude_8pct` | 振幅 ≥ 8% | 振幅 ≥ 6% |
| `volume_ratio_3x` | 量比 ≥ 3 | 量比 ≥ 2.5 |

所有比较先转为规范 Decimal 字符串。单次 poll 必须是完整四股窗口，规范 JSON
不得超过 256 KiB；四行都必须通过既有 Futu 快照门，并额外满足：

- `quote_is_live=true`
- `freshness_basis=live_20m_window`
- `research_ready=true`
- 显式 `security_status=NORMAL`
- 显式 `suspended=false`

闭市、陈旧、未来时间、缺失/重复/额外标的、非有限指标、异常证券状态、上游错误、
危险账户/订单/资金字段或响应超限，都会整批返回零候选、显式 source error 和原始
checkpoint。不会从部分窗口产生信号。

## Episode、去重与崩溃恢复

每个 `(symbol, US/Eastern 市场日期, rule_id)` 每日最多产生一个事件。Source Inbox
事件使用当日 09:30 美东时间作为稳定 episode 锚点；它不是原始 tick 的首次越界时间。
事件事实只记录“完整实时快照满足了封印规则”，不会把每次变化的 metric、随机
`snapshot_id`、`captured_at` 或 poll 时间写入事件身份或内容。

精确行情时间和值仅参与 checkpoint 的规范 observation hash、倒序检查和同时间戳
语义冲突检查。这样即使 Source Inbox 导入后进程崩溃、下一次 OpenD tick 已变化，
同一日同一规则仍会重放为完全相同的完整 item 和 server fingerprint，现有 Source
Inbox 会返回 duplicate，而不是创建第二条事件。相同时间戳对应不同 observation hash
会显式失败，不能静默覆盖。

交付顺序继续是：poll → 校验/投影 → Source Inbox 导入 → 成功后提交 checkpoint。
`DEGRADED`、`FAILED`、dry-run 和崩溃都不提交 operational checkpoint；重启先将遗留
`RUNNING` 标记为 `ABANDONED`，再从最后一次成功状态继续。

## 禁止归因与能力边界

阶段 4 事件固定：

- `item_type=market_anomaly_signal`
- `severity=info`
- `recommended_route=notify_only`
- `impact_hypotheses=[]`
- `news_attribution_performed=false`
- `causal_attribution=none`
- `signal_only=true`

unknowns 明确声明原因未知、没有新闻/因果归因，也没有未来方向或交易含义。阶段 4
不会检索新闻、不会匹配 SEC/IR/宏观事件、不会调用 Provider、不会创建 round draft 或
正式 round，也没有账户、持仓、订单、资金、支付、钱包或执行能力。后续阶段 5 只能使用
[确定性影响规则 sidecar](./trading_impact_rules_phase5.md)，不得改写该原始信号或增加新闻归因。

## 默认状态、调用账本与数据库

默认仍为：

```env
AI_STUDIO_SOURCE_MONITOR_ENABLED=0
AI_STUDIO_SOURCE_MONITOR_AUTO_START=0
AI_STUDIO_SOURCE_MONITOR_OFFICIAL_ONLY=1
AI_STUDIO_SOURCE_MONITOR_ALLOW_READONLY_MARKET=0
AI_STUDIO_SOURCE_MONITOR_DRY_RUN=1
AI_STUDIO_SOURCE_MONITOR_MAX_ITEMS_PER_RUN=50
AI_STUDIO_SOURCE_MONITOR_TRADING_IMPACT_RULES_ENABLED=0
```

Futu 模式必须显式使用 `official_only=false` 与 `allow_readonly_market=true`，并在数据库
中按确定性 `config_version` 单独启用 adapter；设置环境变量本身不会注册、启动或自动
连接 OpenD。本阶段没有接入应用自动启动。

`AdapterPollResult`、Supervisor 和 Scheduler 会报告每轮真实的高层只读行情调用数；
正常 Futu poll 为 1。异常在返回 poll receipt 前发生时，回执使用 `unknown` 加封印上界，
而不是虚假写 0。Source Inbox receipt 中的 `market_calls_performed=0` 仅表示“导入内核
本身没有再读取行情”，不能当作端到端 Futu 调用账本。

阶段 4 复用现有 `source_adapter_states`、`source_adapter_runs` 和 Source Inbox 表，没有
数据库迁移。阈值、滞回、四股白名单、来源 URL、source channel、客户端模式和 poll
周期均进入 `config_version`；变化时必须在 adapter 禁用状态下显式迁移 checkpoint。

离线测试只使用本地 JSON fixtures、注入 quote client、临时时钟、临时 SQLite 和注入
的 monotonic/random source；没有连接 OpenD、Provider、正式端口或正式数据库。通过
这些测试不等同于真实 Futu 在线验收、行情事实确认、浏览器验收、交易许可或公开发布
授权。

## 一次性生产路径观察

在启用 Futu adapter 或安排独立 soak 前，可先运行不接触 Studio 数据库的一次性观察：

```powershell
python -I -B scripts\run_futu_live_preflight.py --help
python -I -B scripts\run_futu_live_preflight.py `
  --confirm RUN_FUTU_LIVE_PREFLIGHT_ONCE
```

公共入口只接受代码固定的 `127.0.0.1:11111` 和上述四个标的，不接受 host、port、
symbols、数据库路径或凭据参数。确认通过后，它在一个由父进程 15 秒 watchdog 约束的
隔离子进程中运行；子进程使用最小环境白名单、跳过 `.env.local`，并把配置导入所需的
runtime/数据库路径绑定到既有项目目录和一个必须不存在的哨兵文件。它不会启动或登录
OpenD，也不会导入 Store、Provider、Source Inbox 或监控 runtime。

锁定版 Futu SDK 在 Windows 导入时会强制创建日志。父进程因此在自己的单次临时目录内
创建专用 `APPDATA/LOCALAPPDATA` profile；worker 只接受与当前临时 cwd 精确绑定的该
profile，不继承真实用户目录。SDK 退出后父进程才回收整个临时目录；清理失败会成为
indeterminate 生命周期错误。SDK 导入期的普通异常或 `SystemExit` 只映射为固定
`FUTU_SDK_IMPORT_FAILED`，不泄露异常正文，也不能降级成可提升的浅层回执。第三方 SDK
实际写入、同用户路径竞态与进程后代仍不被冒充为已完整观测。SDK 导入和调用期间的
Python `stdout/stderr` 被定向到空设备，最终 JSON 在恢复后的独立协议时段输出；父进程
仍严格解析完整 stdout，不会用“最后一行”绕过额外输出。

worker 即使被直接调用，也会再次重建自己的最小环境，而且只能输出
`watchdog_worker_observation` 中间回执，其中 `watchdog_enforced=false`。只有公共父进程
确认 worker 在限时、限流边界内退出并严格重验中间回执后，才会升级并重封为
`production_path_observation`；公开的 worker token 只是父子关联标记，不被当作安全凭据。

预检使用同一 `FutuReadOnlyBroker` 的 `one_shot` 模式；正式 Runtime 使用其
`managed` 模式。两者使用同一有界请求协议和 worker 入口。worker 内复用
`FutuUsMarketAdapter.quote_batch(..., force=true)`，但给 SDK 只暴露
`OpenQuoteContext`、一次 `get_market_snapshot`、最多一次 `get_market_state` 和一次
`close`。这些高层调用有精确计数且每项上限为 1；SDK 内部 socket、线程、keepalive
或重连没有独立观测，所以 `network_requests_performed` 必须保持
`null/sdk_transport_not_instrumented`。该入口只接受精确锁文件当前固定的
`futu-api==10.10.7008`；`close` 未确认成功、依赖漂移、超时、SDK 版本不符、
OpenD 离线、四股不完整或任一快照质量门失败都不能产出 passed。

managed worker 每次请求仍重新打开并关闭 Quote Context；只复用受控的 SDK 进程和临时
Profile。worker 崩溃或协议失效时，当轮失败关闭且不暗中重试，下一调度周期才可重建。
Runtime 取消或截止时会回收在途 worker；直接 worker 被终止仍不是任意后代进程已终止的证明。

输出是闭集、最多 16 KiB 的单行 JSON，只含固定错误码、覆盖/质量计数、调用账本和密封
哈希；不含行情价格、OpenD 用户、异常原文、路径或凭据。`passed` 仅表示当时本机固定
回环路径返回了通过既有只读快照门的四股截面；它不是行情真值的独立见证，也不证明
24 小时连续性、Provider、正式迁移、交易许可、PR 合并或发布验收。系统始终保持
`execution_capability=none` 与 `live_trading_allowed=false`。
