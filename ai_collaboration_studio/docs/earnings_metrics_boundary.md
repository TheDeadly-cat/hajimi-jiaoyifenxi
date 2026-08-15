# 官方业绩指标边界

更新时间：2026-07-20

`official_earnings_pack_v1.metrics` 是从具体官方演示或补充财务资料中人工核验的一小批结构化索引，不是自动生成的交易信号。

每条指标必须包含：

- `symbol / fiscal_period / metric_name / value_text`；
- `fact_or_guidance`：只能是历史事实或公司指引；
- `technology`：DRAM、NAND 或 HDD；
- `source_url / source_locator`：必须能回到具体官方文件和 PDF 页码/表名；
- `claim_status=company_statement`：官方公司材料仍属于公司自述；
- `verification_method / verified_at`：说明核验方法与日期；
- `execution_capability=none / live_trading_allowed=false`。

当前只覆盖 MU、SNDK、WDC、STX 的 FY2026-Q3。未知季度返回空列表，不外推、不补值。`direction` 只描述原始指标相对比较期的方向，不能直接转译为股价方向、多空评分、胜率或仓位。

SNDK 与 WDC 的跨期比较必须同时带 2025-02-21 分拆口径断点。管理层市场展望与历史已实现数据必须在界面和智能体提示词中保持不同标签。
