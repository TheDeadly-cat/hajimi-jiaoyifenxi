# 确定性影响规则边界（阶段 5）

## 目标与非目标

阶段 5 实现代码封印的 `trading_impact_rules_v1`，把已经通过 Source
Inbox 规范化的 SEC、公司 IR、官方宏观事件和 Futu 异常信号投影为：

- 影响假设；
- 传导机制；
- 时间维度；
- 有明确语义的规则覆盖置信度；
- 反证状态。

引擎是纯函数路径：不调用 LLM、Provider、网络、行情、数据库、账户、
订单、资金或任何执行接口。它不预测涨跌、幅度、延续或反转，不生成盈利
、仓位、买卖、目标价或授权语义。Futu 信号仍不得自动归因于新闻。

## 不改写原始条目

规则输出不写回 `project_source_item_v1.impact_hypotheses`，也不改动：

- 原始 `packet_json` 与 `source_import_receipt_v1`；
- `source_inbox_items.item_json` 与 `item_sha256`；
- `server_fingerprint` 及其冲突检查；
- Adapter `config_version`、checkpoint、ETag 或 Last-Modified；
- 已有材料快照、附件哈希、round draft 或正式 round。

原因是现有 server fingerprint 不包含影响假设和 extensions，但完整 item
哈希包含它们。在原条目上就地加字段，会让旧事件在崩溃重放或跨版本切换时
产生同指纹不同完整哈希的显式冲突。阶段 5 因此使用独立、不可变、版本化
sidecar。

## 封闭来源与规则

引擎只接受下列精确绑定：

| Adapter | source class | channel | item/extension |
|---|---|---|---|
| `sec_filings` | `official_source` | `official_source_monitor` | `sec_filing` / `sec_v1` |
| `company_ir` | `official_source` | `official_source_monitor` | `company_ir_release` / `company_ir_v1` |
| `federal_reserve` | `official_source` | `official_source_monitor` | macro release / `macro_official_v1` |
| `bls_releases` | `official_source` | `official_source_monitor` | macro release / `macro_official_v1` |
| `treasury_releases` | `official_source` | `official_source_monitor` | macro release / `macro_official_v1` |
| `official_macro_calendar` | `official_source` | `official_source_monitor` | macro schedule / `macro_official_v1` |
| `futu_anomaly_signals` | `readonly_market` | `futu_anomaly_monitor` | `market_anomaly_signal` / `futu_anomaly_v1` |

v1 规则次序是合同的一部分：SEC 周期披露/当期披露，IR 修订/日程/披露，
宏观日程修订/日程/发布修订/发布，以及 Futu 价格上行、价格下行、日内
区间和市场活动观测。每个条目最多命中一条规则；修订规则优先于基础规则。

SEC、IR 和 Futu 只产生直接证券研究复核假设，不推断同行溢出。宏观条目
只投影三个代码固定研究板块，不把宏观观测直接外推为收入、利润或股价：

```text
sector:dram -> US.MU
sector:nand -> US.MU, US.SNDK
sector:hdd  -> US.WDC, US.STX
```

未知 SEC form、宏观 authority/family/phase 或 Futu rule ID 是 schema drift，必须
失败关闭。IR `other` 是唯一明确的 `NO_MATCH`；`NO_MATCH` 也会持久化，
从而区分“已评估无匹配”和“未启用/未评估”。

每个投影还封存 `trading_impact_source_semantics_v1` 父语义绑定：精确 Adapter、
命中规则、真实证据 source index、直接证券/表单/事件类型或宏观 authority/family/
phase/state、Futu 原始规则与 session date，以及时间锚点、语义和精度。投影验证
必须让规则、证券、模板文本、证据索引和时间维度逐项等于该绑定；sidecar 写入和
读取又会从不可变父条目只读重建该绑定并精确比较。该只读检查不生成新投影、
不回填、不写数据库，也不调用 Provider、模型、网络或行情。

`TradingImpactRulesV1` 实例没有可写实例状态且禁止继承；Supervisor 和 Source
Inbox 通过封印的类路径调用，不接受实例级 `project_item` 覆盖。

## 置信度、反证与信任边界

v1 假设的 `confidence=0.5` 只表示四项确定性覆盖检查中完成两项：

1. 来源绑定通过；
2. 封闭规则与影响范围映射通过；
3. 没有独立佐证；
4. 没有反证复核。

该数字不是真实性概率、结果概率、预期收益或市场方向信心。v1 不会伪造
反证；反证固定为 `unknown`，观测列表为空，并声明当前条目中没有独立或
相反证据。真实反证需要未来的新合同版本，不能在 v1 中静默增加。

原始条目和派生记录都保持 `external_unverified`。影响投影不改变 severity、
route、已读、附加、round-draft 权限或任何用户决策。

## sidecar 与事务语义

`source_inbox_trading_impact_projections` 对 `(item_id, ruleset_version)` 只允许一条
不可变记录。它封存原 item SHA/fingerprint、精确来源绑定、规则集版本/
哈希、投影哈希、回执哈希和 engine-only 调用账本。该账本固定报告：

- model/provider/network/market 调用各为 0；
- formal round 为 0；
- `execution_capability=none`；
- `live_trading_allowed=false`。

Futu poll 的端到端行情调用数仍由 Adapter/Supervisor 如实报告为 1；sidecar
中的 0 只说明影响引擎未额外读取行情，不能混为端到端账本。

对新 import，原始 import/item/link/event 与所有必需 sidecar 在同一 SQLite
`BEGIN IMMEDIATE` 事务内全部提交或全部回滚。引擎失败、规则集漂移或封存
冲突都不会留下半个 import，checkpoint 也不会推进。

对精确的旧 external-run 重放，系统只读取已存记录，不重算、不回填、不
写入。新 run 遇到相同完整 item 时，可以为尚未评估的条目创建 v1 sidecar；
已存精确 v1 只验证并复用。同一 v1 出现不同 manifest/projection/receipt
必须显式冲突，不覆盖。v2 将来使用新版本行，不重写 v1。

受影响的 SEC 生产者同时收紧了已有重放语义：`sec_v1.discovered_at_ms`
保留兼容字段名，但值改为官方事件时间的稳定 epoch 毫秒，不再使用每轮
poll 时钟。否则导入后崩溃的延迟重放会在同指纹下改变完整 item SHA。
本机首次发现时间仍由 Source Inbox `received_at` 如实保存。该生产者语义进入
`sec_filings_config_v2_*`，不会在旧启用状态上静默切换。

SEC 有 `accepted_at` 时，时间维度精确标为 `sec_acceptance_time/timestamp`；
缺少接受时间而只剩 `filing_date` 时，锚点是该日期的 UTC 午夜，但语义必须是
`sec_filing_date_anchor_not_exact_time/date_anchor`，不能伪装成盘中精确时间。

## 默认状态、迁移与回滚

新功能单独默认关闭：

```env
AI_STUDIO_SOURCE_MONITOR_TRADING_IMPACT_RULES_ENABLED=0
```

它不能自动打开监控、auto-start、Futu 访问或非 dry-run 交付。启用时必须由
Supervisor 注入精确 `TradingImpactRulesV1` 实例；标志与引擎不一致会在
构造时失败关闭。规则集版本不能通过环境变量任意指定。

数据库变化只有新 sidecar 表、索引、不可变触发器和 migration key；不回填、
不改写已有 Source Inbox、monitoring、Provider 或 round 数据。正式数据库仍必须通过
现有 preview/prepare/apply 门禁，启动不会自动迁移。回滚时关闭标志或回退代码；
附加表保持惰性可读，不执行 down migration，不删除已封存投影。

SEC v1 poll-time `discovered_at_ms` 到 v2 稳定事件时间的切换必须先保持 Adapter
禁用且无 `RUNNING` 记录，再调用只读
`preview_sec_filings_v1_to_v2_migration()`。预览把旧 checkpoint 与已持久化的
官方 SEC Source Inbox accession 取确定性并集，且逐条验证 item JSON、完整 SHA、
fingerprint 与 `sec_v1.accession_number`。预览使用 SQLite `mode=ro` 与
`query_only=ON` 连接，不调用会改变 journal mode 的普通写连接。
`migrate_config()` 只接受与该预览完全
相同的 replacement checkpoint；缺项、多项、陈旧 state version 或超过 1,000
都会零写入失败关闭。迁移不改写旧 Source Inbox 条目、回执、哈希或历史 sidecar。
预览只承认能由 `source_adapter_runs` 的真实 run ID、Adapter key、非 dry-run
终态及 receipt 规则绑定的历史 `CREATED`/`DUPLICATE` import link；成功/降级 run 的
精确 receipt 绑定优先于可回拨的本机墙钟，空 receipt 的失败/遗弃 run 仍要求时间窗口。
条目的首次 origin 不能取代后来真实 Worker observation 的 provenance。仅靠存量
`source_channel/source_key` 标签不能污染迁移 checkpoint。HTTP/本地用户手工导入
也不能声明 `official_source_monitor` 或 `futu_anomaly_monitor` 保留通道，这两个
通道只接受内部 `source_monitoring_worker`。

## 验证边界

测试使用本地 fixture、注入时钟、临时 SQLite 和禁网包装器；不连接 Futu/OpenD、
Provider、正式 8770/11111 端口或正式数据库。代码/单元测试通过不等于浏览器
验收、在线数据真实性、预测有效性、盈利能力、交易许可或公开发布授权。
