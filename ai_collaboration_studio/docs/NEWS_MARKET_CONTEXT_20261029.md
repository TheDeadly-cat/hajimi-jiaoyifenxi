# 保存的市场上下文与独立影响任务

此合同复用已有研究房间检查点和资料版本，不创建第二套行情、共识或资料权威库，也不请求实时行情。解析函数接收调用方已有的 Store，使用 `get_round_checkpoint`、`validate_round_evidence_manifest`、`get_material_version` 和原有快照哈希；调用方保存解析后的冻结上下文。

## 时点和证据准入

`as_of` 和执行时的 `checked_at` 都须携带时区。新闻版本发现、研究检查点保存、材料版本的真实 `changed_at` 及行情捕获时点都不能晚于 `as_of`。用户填在材料里的发布时间不替代资料版本实际入库时间。

预期和旧指引须在公告发布前来源已知、系统已存。已公布实际值可以在公告后取得，但必须在截面前保存，指向同一事件 URL，来自该标的注册官方来源。所有材料均须在该轮已验证证据清单内，引用固定版本；内容或快照篡改会拒绝解析。自由文本没有下列结构化元数据时保留为未知，不由模型猜测共识。

现有 Store 的资料清理器现已接纳严格的 `market_context` 嵌套字段，新增和更新都检查引文属于实际保存正文、数值出现在引文内。`news_market_material.prepare_market_material_payload` 是纯构造帮助函数，只生成普通 `Store.add_material` 输入，不接受创建或入库时间；原有 Store 为每次写入生成真实版本时间。浏览器通过原房间资料保存接口显式保存手工记录，保存不发起网络核验、模型或轮次。

资料元数据 `metadata.market_context` 使用：

```json
{
  "kind": "consensus",
  "symbol": "US.MU",
  "metric": "revenue",
  "period": "FY2026-Q4",
  "unit": "million",
  "currency": "USD",
  "comparison_basis": "reported_revenue",
  "value": 12,
  "known_at": "2026-10-29T14:00:00Z",
  "quote": "FY2026-Q4 revenue consensus USD 12 million."
}
```

这段数值是合成合同示例，不是真实共识。`kind` 可为 `consensus`、`old_guidance` 或 `reported_actual`。后者另需 `source_type` 为 `company_ir` / `regulatory_filing`，以及与本消息一致的 `event_url`。资料本身的 `source_url`、正文和版本是原 Store 的字段；quote 必须逐字存在，数值必须出现在该引文内。结构化资料的语义和许可仍需来源质量审核，哈希不证明真实世界事实。

只有主体、指标、期间、单位、币种和比较口径全部相同，且实际值与共识各有唯一匹配记录时，系统用 Decimal 计算差额。多份冲突或不匹配的记录不自动选一个。`comparisons` 同时保留两份证据 ID、值、差额及中文句子。模型要展示预期对照，须逐字采用系统计算句子并引用两端，不能改写成自己计算的另一组数字或交易方向。

## 行情与请求边界

共同快照继续经过已有 `validate_storage_quote_snapshot`。保留原始 market_time、captured_at 和 freshness_basis。在上下文解析和实际提交两次检查新鲜度；执行时超过捕获时点 20 分钟，行情会从本次输入移除并显示未确认，保存的历史上下文不改写。一个快照只能支持当时价格/成交记录，不能证明涨跌因果或充分定价。

模型任务独立于原文事实阅读，输出 facts、new_information、direct_symbols、inferred_impacts、expectation_comparison、market_reaction、counter_evidence、unknowns、follow_up_conditions。事实和假设依据都有证据 ID 与逐字引文，假设另列渠道、期限及条件。引文匹配和有限数值检查不等于完整语义验证。

输入按 UTF-8 最多 8,500 字节选择完整证据记录，为现有 16,000 字节完整供应商请求留出合同空间。比较组三件材料（实际值、共识、确定性计算）一起选入；不足时整组不进入比较，记录 omitted_evidence_ids/count 与截断。未选材料不能支撑模型结论。

## 验证范围

离线验证覆盖真实临时 StudioStore 的新增/版本读回/更新、嵌套元数据保留、真实检查点与清单校验、历史截面、晚到预期、未来公告、已过时行情、匹配/不匹配对照、篡改、引用及数值拒绝、长中文证据选择。调用方伪造 created_at/changed_at 不会改变 Store 保存时间。另有实际新闻数据库和共享预约账本的模拟模型链路验证；浏览器呈现由集成验证记录补充。这些结果不冒充真实共识、真实行情或模型质量。
