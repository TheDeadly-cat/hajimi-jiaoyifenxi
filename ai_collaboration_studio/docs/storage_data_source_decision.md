# 存储产业专用数据源决策

更新时间：2026-07-20

目标不是堆更多网页，而是补足 DRAM、NAND、HDD 三条子周期的可复现证据。每个数据源必须明确来源、时间、口径、许可和缺陷；它们不直接生成交易信号。

## 结论

“官方季度业绩材料包”、具体材料链接发现和 FY2026-Q3 首批页码/表名指标已经实现；FRED 官方月度行业代理也已进入共同快照。下一步是扩展最近 8 个季度，并在合法许可和稳定接口成立后补充真正区分 DRAM、NAND、HDD 的专用价格/供给数据。Census 贸易适配器仍需本机 key 与年度商品代码冻结；TrendForce / DRAMeXchange 继续禁止未授权抓取。

| 优先级 | 数据源 | 可补充内容 | 接入条件 | 结论 |
| --- | --- | --- | --- | --- |
| A1 | MU、SNDK、WDC、STX 官方 IR + SEC | DRAM/NAND bit shipment、ASP 方向、终端收入、HDD exabyte/单位/ASP、HAMR 进度、公司指引 | 公共页面；保留来源链接、时间和原文位置，不复制整份版权材料 | 立即做 |
| A2 | U.S. Census International Trade API | 月度存储器集成电路和 HDD 贸易金额/数量，按国家拆分 | 当前 API 查询需要本机 `CENSUS_API_KEY`；必须冻结 HS/Schedule B 年度版本 | 准备适配器，拿到 key 后启用 |
| B1 | 日本 e-Stat / METI Current Production Survey | 日本生产、出货、库存和部分电子/存储设备品类 | 注册并取得不可转让的应用 ID；先确认当前表仍保留目标品类 | 做小型可用性验证后再决定 |
| B2 | UN Comtrade | 全球按商品和贸易伙伴的官方贸易流 | 遵守免费层限额和再分发条款；口径比 Census 更粗 | Census 不足时补充 |
| C | TrendForce / DRAMeXchange | DRAM/NAND spot、contract price 和供需预测 | 需要用户自有且允许机器使用/保存的授权 | 禁止静默爬取；只做可选持牌适配器 |

## A1：官方季度业绩材料包

四家公司都公开季度结果和支持材料：

- Micron quarterly results：<https://investors.micron.com/quarterly-results>
- Sandisk presentations：<https://investor.sandisk.com/news-events/presentations>
- Western Digital presentations：<https://investor.wdc.com/investor-events/presentations>
- Seagate investor news：<https://investors.seagate.com/news/>
- SEC public APIs：<https://www.sec.gov/search-filings/edgar-application-programming-interfaces>

建议的新结构不是全文镜像，而是 `official_earnings_pack_v1`：

- `symbol / fiscal_period / published_at / source_url / source_kind`；
- `technology`：DRAM、NAND、HDD；
- `metric_name / value_text / direction / comparison_period`；
- `source_locator`：页码、表名或段落标题；
- `fact_or_guidance`：历史事实与公司指引必须分开；
- `company_claim=true`：公司自述不等于独立验证；
- 原始文件只按需短时解析，房间材料保存少量必要摘录、哈希和官方链接。

当前实现状态（第一阶段）：

- 已区分 `earnings_release`、`earnings_schedule` 和普通 IR 事件，预告不会被误当成已披露业绩；
- 已从标题归一化财政年度/季度，无法可靠识别时显式标记 `UNRESOLVED / unknown`；
- 已绑定新闻稿、presentation 官方入口、SEC 日期候选、技术范围与分拆口径断点；
- MU、SNDK、WDC 最近季度的 presentation、MU prepared remarks、STX supplemental financial information 已建立具体官方链接；
- 实时 HTML 发现与带 `verified_at` 的人工核验目录分开标记；Akamai 验证页、SSL 中断和站点超时都保留为来源错误，不伪装成实时成功；
- 没有具体文件时仍使用 `hub_only`，只说明官方材料入口存在；
- 已完成 FY2026-Q3 第一批逐页定位：MU 的 DRAM/NAND bit shipment 与 ASP、SNDK 的终端收入、WDC/STX 的 HDD exabyte 和各公司少量指引；未知季度不补值。
- HAMR 里程碑和最近 8 个季度仍待扩展，自动 PDF 抽取尚未启用。

需要特别处理 2025 年 Sandisk 从 Western Digital 分拆：分拆前 WDC 的 Flash 历史不能无说明地拼接到分拆后 SNDK 序列。

## A2：美国贸易数据

官方入口：

- Census International Trade data：<https://www.census.gov/foreign-trade/data/index.html>
- Census API catalog：<https://api.census.gov/data/timeseries.html>
- 2026 Schedule B Chapter 85：<https://www.census.gov/foreign-trade/schedules/b/2026/c85.pdf>
- HTS search：<https://hts.usitc.gov/>

已确认 Schedule B 把 `8542.32` 定义为 memories，并继续细分动态随机存取存储器、EEPROM 等。适配器仍必须把限制写进数据：

- 贸易值不是 DRAM/NAND 合约价；
- EEPROM/其他 memory 分类不能无条件等同于 NAND Flash；
- HDD 代码要按当年 HTS/Schedule B 分别确认，不能跨年硬编码；
- 跨国集团内部转移价、产地变化和提前备货会扭曲月度信号；
- 只用于滞后的供需代理，不生成短期方向分数。

## B1：日本 e-Stat

官方 API：<https://www.e-stat.go.jp/api/index.php/en>

e-Stat 内容条款允许包括商业用途在内的再利用，并与 CC BY 4.0 兼容，但要求来源标注；API 本身需要注册应用 ID，ID 不得转让。当前 Production Survey 的机器可读目录包含生产、出货和库存维度，但目标 memory/HDD 子类是否仍持续发布必须先用元数据接口验证，不能依据十多年前的表直接接入。

## C：为什么不直接抓 TrendForce

TrendForce 公共页面能看到部分最新 DRAM 价格和新闻摘要，但历史下载、合约价明细和月度数据属于会员产品，材料标注 All Rights Reserved。当前没有找到允许我们批量抓取、长期保存和再分发这些数据的公开授权。因此：

- 公共新闻稿可以作为带时间、链接和“二手研究机构观点”标签的共享材料；
- 不把公开价格页做成后台定时爬虫；
- 如果用户购买授权，再根据合同范围实现本机凭证适配器，密钥不进入前端或数据库；
- 任何付费数据都不能被导出到超出授权范围的会议产物。

官方入口：<https://www.trendforce.com/price>、<https://www.trendforce.com/research/dram>

## 实施顺序

1. 已完成：扩展现有 SEC/IR 适配器，识别季度结果并建立 `official_earnings_pack_v1` 索引。
2. 已完成第一批：发现并核验 FY2026-Q3 的 presentation / prepared remarks / supplemental 文件；实时入口失败时显式降级到带核验日期的官方目录。
3. 已完成第一批：FY2026-Q3 逐公司字段映射和来源定位；继续扩展最近 8 个季度。
4. 已完成第一层：在同轮证据截面中显示历史事实、管理层指引和独立官方统计三种不同标签。
5. 增加 Census 适配器骨架，只有检测到本机 key 才启用；先保存原始商品代码和描述，再计算同比/环比。
6. 用 e-Stat 元数据做一次目标品类连续性验证；验证失败则不进入正式数据层。
7. TrendForce 维持“用户持牌后可选”，不阻塞公开数据主线。
