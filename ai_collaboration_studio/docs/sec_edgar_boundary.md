# SEC EDGAR 官方申报边界

## 目标

SEC 适配器只把官方监管申报作为共同证据源，为新闻与情绪、基本面和风控角色提供可审计的事件入口。它不解释交易方向，不替用户读取或接受 SEC 服务条款，也不连接任何交易账户。

## 固定官方端点

- `https://www.sec.gov/files/company_tickers.json`：ticker、CIK 与公司名映射。
- `https://data.sec.gov/submissions/CIK##########.json`：公司近期申报记录。
- 返回的原文链接只构造到 `https://www.sec.gov/Archives/edgar/data/...`。

适配器拒绝非 SEC 固定端点，不携带 Cookie、模型密钥或认证信息。每一次请求的重定向策略还绑定到本次请求的完整 URL，不能从一个公司的 submissions 路径跳到另一 CIK；响应根 `cik` 必须是与请求完全一致的 10 位字符串，否则整份响应不投影。SEC 接口不需要 API 密钥，但自动访问必须在本机 `SEC_USER_AGENT` 声明真实产品或组织名与联系邮箱；未声明时不会发送请求。

## 范围与数据质量

- 默认小规模监控池严格为 `US.MU`、`US.SNDK`、`US.WDC`、`US.STX`、`US.NVDA`、`US.MRVL`、`US.AMD`；存储产业原有调用仍可显式传入原四只标的，行为保持兼容。新标的只能通过代码配置的 `allowed_symbols` 注册，单次读取仍受该实例白名单约束，不能借请求扫描全市场。
- 表单严格限制为 `10-K`、`10-Q`、`8-K`、`20-F`、`40-F`、`6-K`。
- 保留 CIK、公司名、accession number、表单、提交日、报告期、接受时间、事项字段、主文档描述和官方原文链接。
- accession 前缀是提交者 CIK，可能属于第三方 filing agent，不要求与公司目录 CIK 相同；archive URL 仍必须由公司目录 CIK、完整 accession 和严格安全的单文件名逐段重建并精确匹配。
- 未来提交日、缺失主文档、路径异常和未在官方映射中出现的标的不会进入证据。
- 单个 JSON 响应上限 2 MB；单进程请求间隔至少 110 毫秒，并使用缓存，保持低于 SEC 公布的每秒 10 次访问上限。
- SEC 官方映射本身声明不保证完整性；缺失映射必须作为数据质量错误显示，不能手工猜测 CIK 后静默替代。

## 解释边界

EDGAR 表单是事件入口，不是事件影响结论。角色必须继续阅读表单事项与原文，区分定期报告、当前报告、修订和境外发行人文件；不能仅凭“出现 8-K”或提交时间推断利好、利空、胜率或仓位。

公司 IR 新闻稿与 EDGAR 申报如果发生在同日或相邻一日，系统只添加 `possible_sec_matches` 关联候选。它不会按日期静默删掉任何一条，也不会断言两份文件描述的是同一事件；最终仍需核对标题、事项和原文。

用户可在房间中把选中的申报冻结为版本化资料。服务端会重新查询当前 SEC 目录并精确匹配官方 URL，以 accession number 去重，再生成不含方向解释的索引资料；客户端提供的标题、来源层级或正文不会被信任。冻结后仍需打开 SEC 原文核对，表单类型不能替代内容分析。

## 持续监控投影

`source_monitoring.adapters.sec_filings` 复用本适配器，但不下载附件或正文。旧按需接口仍返回每标的 limit 窗口；监控专用接口会扫描 submissions 响应大小上限内的全部规范化 recent 条目，再让已见 accession 跳过而不占本轮新事件额度。它以严格格式的 accession number 作为外部身份；checkpoint 只保存最多 1,000 个已成功导入的 accession。当前响应超过 1,000 个唯一 accession 时整轮显式 `SEC_CHECKPOINT_CAPACITY_EXCEEDED`、不导入也不推进，不会通过截断 seen 列表静默丢失尾部。`sec_v1` 扩展记录表单、Item 编号、CIK、标的、提交日、接受时间、主文档名和本机发现毫秒时间。官方主文档正文未抓取时，`content_sha256` 必须保持空字符串。

Supervisor 先轮询和校验，再直接调用现有 `SourceInboxService`，只有导入成功后才提交 checkpoint。相同 accession 的后续轮询只增加 duplicate 计数。持久化的 ETag 与 Last-Modified 当前仅作为有界上下文原样透传；本适配器尚未声明已实现条件 GET。
