# 官方来源持续监控边界（阶段 1～2）

## 默认状态与注册范围

监控内核默认关闭、默认不自动启动、仅允许官方源并默认 dry-run：

```env
AI_STUDIO_SOURCE_MONITOR_ENABLED=0
AI_STUDIO_SOURCE_MONITOR_AUTO_START=0
AI_STUDIO_SOURCE_MONITOR_OFFICIAL_ONLY=1
AI_STUDIO_SOURCE_MONITOR_DRY_RUN=1
AI_STUDIO_SOURCE_MONITOR_MAX_ITEMS_PER_RUN=50
```

`build_official_source_registry()` 没有依赖注入参数，只能构造代码固定的真实 `SecFilingsSourceAdapter` 与 `CompanyIrSourceAdapter`；它不会创建线程、启动服务或发起网络请求。外层适配器同时封印构造时 inner client 的实例身份、精确类型和 transport callable；SEC 还封印声明的 User-Agent。运行前任一项被替换都会在网络访问前失败，生产 builder 另断言两者都使用默认 HTTPS transport。测试替身只能直接使用普通 `SourceAdapterRegistry`，不能通过生产官方 builder 获得官方来源 provenance。注册前还会闭合验证 `poll(checkpoint, *, observed_at_ms, etag='', last_modified='', max_items=50)` 签名，因此旧协议适配器不会在创建 `RUNNING` 记录后才失败。每个适配器还必须在数据库中按其确定性 `config_version` 显式启用；该版本封印白名单、表单或 feed、checkpoint/投影版本、每源上限、候选上界、inner 类型、default/injected transport 模式、轮询周期和 force 模式。配置变化时旧状态不能被静默复用，必须在 disabled 状态下显式迁移。

## 交付顺序与恢复

每次 live 运行遵循唯一顺序：

1. 创建 `source_adapter_runs` 的 `RUNNING` 记录并冻结起始 checkpoint；
2. 使用固定官方适配器轮询，产生现有 `project_source_item_v1`；
3. 通过真实 Source Inbox JSON 入口验证 `source_import_packet_v1`；
4. 直接调用 `SourceInboxService`，不向本机 HTTP 接口发请求；
5. 将不可变导入 receipt 绑定到 run；
6. 只有 `SUCCEEDED` 才提交 next checkpoint。

因此导入后崩溃可能重放同一事件，但现有 Source Inbox 指纹会把精确重放记为 duplicate。来源错误或任何 rejected item 使 run 进入 `DEGRADED`，checkpoint 保持不变并进入退避；异常进入 `FAILED`。进程重启会把遗留 `RUNNING` 标记为 `ABANDONED`，之后从最后一次成功 checkpoint 继续。

Dry-run 只验证候选 packet 并保存终态 run receipt，不写 Source Inbox，也不修改适配器的 operational state、checkpoint、成功时间、失败计数或下一运行时间。

## 数据与能力边界

- 复用现有 Source Inbox；不建立第二套事件表。
- Source Inbox 的 `received_at` 是本机发现时间，事件自身保留官方发布时间；SEC 扩展另保存 `discovered_at_ms`。
- ETag 与 Last-Modified 有严格长度和控制字符边界，但阶段 2 的现有 SEC/IR 客户端尚未实现条件 GET，只会原样保留上下文。
- 每个适配器声明并封印本轮可输出的 `max_candidates_per_poll`（当前默认 SEC 为 42、IR 为 32）。全局 `AI_STUDIO_SOURCE_MONITOR_MAX_ITEMS_PER_RUN` 小于任一输出上界时，Supervisor 在写 state/run 前拒绝构造；适配器被直接调用时也在抓取前拒绝。监控专用解析路径仍扫描响应大小上限内的全部规范化 SEC recent/IR RSS 条目，已在 checkpoint 中的精确重复不占每源新事件槽，因此同一固定窗口中的未见条目可在后续轮询继续排空；旧按需接口保持原有每源 limit 行为。
- Checkpoint 必须能完整表示当前官方窗口：SEC 最多 1,000 个唯一 accession，IR 最多 250 个唯一身份。当前有效窗口一旦超过容量，整轮返回显式 capacity source error、零候选、零导入并保持 started checkpoint；Supervisor 记录 `DEGRADED`。容量内更新会先丢弃已离开当前窗口的 stale 身份，再加入新身份，禁止通过尾部截断造成循环重放或尾部饥饿。更大的官方窗口需要后续版本化水位/回填游标，不能在本版本静默降级。
- 所有候选保持 `external_unverified`，已阅不代表事实确认。
- Worker 不调用 Provider、Futu/市场读取或模型，不创建房间、材料、round draft 或正式 round。
- `execution_capability=none`、`live_trading_allowed=false`；没有订单、账户、资金、支付或钱包能力。
- 关闭监控不会删除已存监控数据，也不改变原有房间、材料、Manual ChatGPT 或 Source Inbox 状态机。

宏观官方源、Futu 异动、零 Token 影响规则、前端通知、GPT 导入增强和自动启动仍属于阶段 3～8，不在本次实现中。
