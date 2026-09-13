# API Controlled Pilot v1（离线准备中）

基线为 `aceb04165d2b72564918762d6d9e46f42d9f33a9`。本任务在独立 `codex/api-controlled-pilot-20260913` 工作目录进行；已交付正文选段入口继续绑定原 aceb041 checkout。

## 已处理的执行合同

- 现有 Provider 的 generate / generate_json 增加可选 `max_output_tokens`。显式值必须是正整数，不接受布尔值、浮点或数字字符串；没有显式值时保留各适配器原默认值。
- 人工 API 审查从冻结包的输出预算取得逐次上限，并实际传给适配器。quick 为两次 900，standard 为三次 1,400，deep 为四次 2,000。预算不完整则在调用前拒绝；不自动提高上限。
- 新执行计划 `manual_chatgpt_review_plan_v2` 记录逐次/总输出上限、完整生成参数哈希，以及 instructions 加实际 review_input 的 UTF-8 长度。原 review_input v1 和旧已冻结计划/回执不改写。
- ProviderResponse 额外区分实际返回的模型与兼容显示所用的请求模型回退，并保留响应 ID、终态/finish_reason、拒绝和 incomplete 原因。OpenAI/豆包的未完成、失败、排队或拒绝响应，即使有文字也不算成功；Chat Completions 的非 stop 终态同样拒绝。
- 发现既有账本敏感字段过滤会丢弃 `prompt_tokens`。现在在新账本写入前，将标准 Token 计数映射为 input_tokens / output_tokens 和缓存输入计数；正文与秘密过滤规则、旧 usage 读取和哈希算法保持。缺少、冲突、无效用量不补零。

这些是请求和记账边界，不是模型质量或供应商账单验证。输出上限仍需结合选定模型及推理行为评估，不能用极小 probe 证明研究能力。

## 已执行的零费用检查

- 请求上限首次回归：缺少可选参数被明确复现。修复后相关适配器组 32 项通过。
- quick 实际传输回归先复现两次 3,200，而计划是两次 900；已修正传递并记录完整参数身份。
- 审查、操作摘要和只读 MCP 相关 70 项通过。首次运行的 3 项命令行输出测试发生父子进程编码不一致；为本次测试进程设置 PYTHONUTF8=1 后通过，原始失败日志保留。
- 加入终态与返回身份检查后，适配器/输出/策略组 41 项通过；账本组 14 项通过。各组有重叠，不合计为独立覆盖。
- 测试仅使用合成输入、fake transport 与临时数据库。真实 Provider 请求和来源抓取均为零。测试中的 synthetic result import 不是人工研究证据。

项目测试使用既有 run_backend_tests_isolated.py，当前复用基线 checkout 的受管 Python 解释器及相同依赖，执行的是新 checkout 的代码；没有启动正式 8770 服务。

```powershell
$env:PYTHONUTF8='1'
<managed-python> -X utf8 -B scripts/run_backend_tests_isolated.py tests.test_provider_limits tests.test_providers tests.test_provider_output tests.test_provider_policy --verbosity 1
<managed-python> -X utf8 -B scripts/run_backend_tests_isolated.py tests.test_manual_chatgpt tests.test_manual_chatgpt_operations tests.test_readonly_mcp_gateway --verbosity 1
<managed-python> -X utf8 -B scripts/run_backend_tests_isolated.py tests.test_provider_call_ledger --verbosity 1
```

## 后续仍须完成

1. 薄的独立试验入口与非秘密授权摘要：绑定候选、Provider、模型、官方端点、完整输入哈希/上限、实际输出参数、超时、唯一请求 ID、有效时间及费用依据；默认关闭。
2. 独立复制已有 Micron 证据，首次回答的结果格式、引用存在性、输入与结果保存、失败/重启不重复出网检查。
3. 用户选择已充值的服务商、具体模型及 Key 复用方式后，核对现行官方价格，形成可审阅的实际消费计划。Key 只经本机安全输入，不进入仓库、日志或聊天。
4. 具体方案获准后执行一次有意义的证据回答，记录真实响应、Usage、耗时和费用估算/供应商核对状态。任何失败、超时或额外探测均扣次数，不自动重试或换模型。
5. 当前尚无本轮真实人工返回；不能从 BUNDLE_READY 伪造 API_REVIEW。真实人工导入后，才在获准的总计最多三次范围内另做 quick 的两次审查；阶段 C 可按任务书延后。

当前没有实际付费授权、请求记录或完成结论。OpenAI 服务端硬禁用未解除；若用户选择 OpenAI，需完成明确、默认关闭的受控试验策略。本任务不开启来源轮询、交易、正式库迁移或发布。

官方合同核对（2026-09-13）：[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、[OpenAI Responses](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)。max_output_tokens 对 Responses 包括推理和可见输出，不能仅凭出现文本判定请求完整。
