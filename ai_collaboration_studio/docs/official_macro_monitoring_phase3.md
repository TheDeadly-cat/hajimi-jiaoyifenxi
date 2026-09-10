# 官方宏观监控边界（阶段 3）

## 固定来源

阶段 3 只增加四个只读适配器，所有网络请求都是无凭据 `GET`，初始 URL、每次跳转及最终 URL 都必须落在代码固定的 HTTPS endpoint 与精确 host 白名单中：

- Federal Reserve 发布：`www.federalreserve.gov/feeds/press_monetary.xml`；计划：`www.federalreserve.gov/monetarypolicy/fomccalendars.htm`。
- BLS 数据：`api.bls.gov/publicAPI/v2/timeseries/data/{series_id}`，series 仅为 `CUSR0000SA0`、`LNS14000000`、`CES0000000001`，每个系列只保留最新四期的完整规范投影；计划：`www.bls.gov/schedule/news_release/bls.ics`。
- Treasury 数据：Fiscal Data 的固定 Debt to the Penny endpoint，字段、排序与 `page[size]=10` 都是 allowlist 的一部分；计划：`api.fiscaldata.treasury.gov/services/calendar/release`，只接受代码固定的数据集。
- 官方宏观日历不是跨机构“真相合并器”。它分别验证 Fed、BLS、Treasury 三个计划源；任一子源失败、格式歧义、身份冲突或容量溢出时，整轮返回零候选且不推进 checkpoint。当前完整窗口固定为本机观察时刻前 7 天至后 31 天；合法页面在该窗口内没有某机构事件并不等于来源失败，页面本身缺少可识别的固定结构才失败关闭。

默认客户端构造时不访问网络，也不启动线程。响应状态、MIME、原始字节、解析后行数和请求频率都有上界。每个请求同时受 12 秒 socket 非活动超时和独立的 12 秒响应体单调时钟截止时间约束；只有显式抓取期间才创建短生命周期 watchdog，到期直接关闭当前 HTTPS socket，因此正文或 chunked framing 的慢速滴流都不能无限延长一次 poll。BLS ICS 的本地时间只接受 `America/New_York`、`US-Eastern`、`Eastern Standard Time` 三个封闭别名，并统一解释为 `America/New_York`；其他 `TZID` 整源失败关闭。BLS 不使用多系列 `POST`，因此阶段 3 不扩展现有 HTTP method/body 合同。当前环境曾收到 `www.bls.gov` 的反机器人拒绝页；这应成为明确的 fail-closed source error，而不是 HTML 伪装成 RSS/ICS 或紧密重试。离线 fixture 测试不等于 BLS 在线可用性验收。

## 生命周期与身份

每个候选只有以下一种状态：

- `scheduled`：只来自官方日历，表示将来计划，不表示数据已经发布；
- `released`：首次观察到官方发布或官方数据记录；
- `revised`：相同稳定自然身份的规范投影发生变化，或官方源明确标记修订。

稳定身份由 authority、固定 release family、官方自然参考期/稳定 ID 和 `subject_phase` 组成，不包含数值、计划时刻或发布时间。日历时间变化可形成 `subject_phase=schedule`、`revision_target=schedule_time` 的修订；相同 series/record period 的数值或脚注变化形成 `subject_phase=release`、`revision_target=data` 的修订。Treasury 计划记录若没有可证明跨改期稳定的官方 ID，则改期后的记录按新计划处理，禁止猜测合并。

BLS 的 `P`/preliminary 脚注本身不等于“已修订”；只有同一 `(series_id, year, period)` 的后续规范投影真的变化才自动标记 revised。Fed RSS 没有通用 revision 字段，Last-Modified 变化也不自动等于修订。

每个规范投影的 SHA-256 会写入对应 `source.content_sha256`，并明确声明这是 normalized official projection hash，不冒充网页正文哈希。这样 Source Inbox 可以把修订保存为同一 `external_item_id` 的第二条显式事件，而不是发生同指纹异内容冲突。

Source Inbox 强制要求 `occurred_at`。有官方精确发布时间/计划时间时直接使用；BLS/Treasury 数据没有通用发布时刻时，使用稳定的官方参考期日期锚点；FOMC 会议页只有日期时使用开始日期的 UTC 表示锚点。扩展中的 `occurrence_basis` 会明确标成 reference/date anchor，而不是把它冒充精确发布时间。该稳定锚点也保证“导入成功但 checkpoint 尚未提交”后的重放仍命中同一 Source Inbox 指纹。

## 不授权的能力

所有条目继续是 `external_unverified`、`AWAITING_USER`、`notify_only`，且 `impact_hypotheses=[]`。适配器不会调用 Provider、模型、Futu、账户、交易、支付或钱包接口，不创建房间、round draft 或正式 round，不修改前端，也不会自动启动 worker。

监控仍默认 `enabled=false`、`auto_start=false`、`official_only=true`、`dry_run=true`。阶段 3 的离线测试和固定 endpoint 验证不证明官方源持续在线，不证明宏观事实已经人工确认，也不构成影响判断、交易许可或公开发布授权。
