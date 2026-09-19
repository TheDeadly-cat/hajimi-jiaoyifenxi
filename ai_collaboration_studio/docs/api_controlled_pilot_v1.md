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

## 独立试验入口

`scripts/run_api_pilot.py` 复用数据库 owner、既有启动检查、schema 只读打开和关闭 checkpoint；只接受 `APIPilot…/data/studio.sqlite3` 的独立副本。它不开 HTTP 服务、采集器或调度器，既有网页的回环/会话保护保持原样。

- 默认模式只导出已存在冻结包的完整选段与指令，没有模型默认值，也不申请密钥。
- `--prepare` 根据明确模型与官方计价生成非秘密授权摘要：候选、数据库、实际 HTTP body 与哈希、官方端点、输入字节上限、输出 Token 上限、适配器实际网络超时、有效窗口和消费计划。
- `--execute --approve-plan-sha256 …` 先核对干净提交和精确摘要，再在可隐藏输入的交互终端读取 Key。Key 只用于该子进程，所有其他 Provider 禁用；拒绝任何回显降级，退出前清除进程环境。
- 首次证据回答的账本上限固定为 **1 次**，不预授权另外两次审查。scope 为 api_controlled_pilot_v1，沿用现有永久预约及 client_request_id 幂等。独立数据库另有 **3 次累计上限**，在现有预约事务内检查，失败/超时/未完成调用也占次数，不同计划并发不能穿透。失败、输出无效或记录丢失后不能重新出网。
- 真正出网时再次校验请求 URL、完整 body 哈希、字节数和超时参数，拒绝重定向，并限制同一次授权只执行一个 HTTP POST。超时值是现有 urllib 网络超时参数，不宣称是端到端硬时钟或账单保证。
- 请求与响应分别保存在该数据库旁的 `pilot-receipts/<pilot_id>/request.json` 和 `response.json`，不覆盖以前的记录。记录输出文本、结构化结果与完整回执哈希；重放还需匹配计划、attempt ID、终态和账本 Usage。账本 COMPLETED 只表示次数已结算到终态；是否回答成功须看 response.status。
- 首答使用单独的 api_evidence_answer_v1 合同，区分事实/推断/未知，验证 evidence ID 与逐字引文，保留选段范围与附件未读声明。结果标为未核验模型输出，不能当作人工 ChatGPT 返回或自动更改 BUNDLE_READY。
- 实际返回模型必须属于获准身份；缺少 Usage 记录为 unknown，费用为空而非零。原始计数超过输出上限或消费计划时不接受为成功。供应商账单核对和内容质量检查均单列，不由 JSON 校验冒充。

入口与相关回归合并定向检查为 **87 项通过**，9 次允许的回环连接，外网及子进程外网尝试为零。首轮新夹具有名称冲突、不可变触发器预期和回执路径问题，账本结算终态也曾未正确衔接；原日志保留，修正后通过。临时目录中六份合成早期回执核对后归档，没有当作真实模型证据。

补充全库并发额度、其他已启用适配器的完整请求匹配和回执篡改检查后，最终相关定向组为 **91 项通过**，9 次允许的回环连接，外网/子进程外网尝试为零；静态基线 **7 / 7** 通过。这些与 87、70 等组重叠，不累计覆盖。原始材料与两个旧冻结任务没有被模型输出覆盖。

在 `4e75425ba15dc0bea743d559312163d1491ef8af` 的真实 CLI 只读准备中，独立复制旧 Micron 快照，导出两份 2,481 字符摘录；指令 1,039 UTF-8 字节，结构化 input_text 8,399 字节（尚不含具体供应商 HTTP 封装）。数据库 SHA-256 前后相同，Provider/来源执行/正式 round 计数均零，42 个原文件指纹不变。这个结果只证明输入准备，不是 API 连接成功；后续候选须使用对应版本配置再验证入口。

调用形态（项目目录；配置和输出都在独立试验数据目录）：

```powershell
<managed-python> -X utf8 -B scripts/run_api_pilot.py --config <evidence-config.json> --output <evidence-preview.json>
<managed-python> -X utf8 -B scripts/run_api_pilot.py --config <authorized-model-config.json> --prepare --output <request-preview.json>
```

默认证据配置仅有 candidate_sha、database_path、room_id、session_id 四个字段。模型配置字段见 ControlledAPIPilot.prepare 的严格白名单，不含 Key。实际付费执行命令只在模型、官方价格、完整输入与消费计划获准后使用。

## 后续仍须完成（2026-09-13 时点记录）

1. 用户选择已充值的服务商、具体模型及 Key 复用方式后，核对现行官方价格，形成可审阅的实际消费计划。Key 只经本机安全输入，不进入仓库、日志或聊天。
2. 具体方案获准后执行一次有意义的证据回答，记录真实响应、Usage、耗时和费用估算/供应商核对状态。任何失败、超时或额外探测均扣次数，不自动重试或换模型。
3. 当前尚无本轮真实人工返回；不能从 BUNDLE_READY 伪造 API_REVIEW。真实人工导入后，才在获准的总计最多三次范围内另做 quick 的两次审查；阶段 C 可按任务书延后。

截至上述 2026-09-13 时点，没有实际请求记录或完成结论。OpenAI 服务端硬禁用未解除；若用户选择 OpenAI，需完成明确、默认关闭的受控试验策略。本任务不开启来源轮询、交易、正式库迁移或发布。

官方合同核对（2026-09-13）：[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、[OpenAI Responses](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)。max_output_tokens 对 Responses 包括推理和可见输出，不能仅凭出现文本判定请求完整。

## 2026-09-20 后续入口

豆包已完成一项真实请求，详见 [本次记录](doubao_pilot_result_20260920.md)。此后用户明确：千问仍为编程套餐，不连接项目后端；智谱使用火山方舟；DeepSeek 使用官网 API；GPT 使用既有 ChatGPT 协作导出/真实返回导入流程，保持 OpenAI 直接 API 硬禁用。

智谱模型配置可显式指定 `glm_platform: "volcengine_ark"`。注册表保留 `glm` 模型身份，只在明确选择时使用方舟官方 Responses 端点及 `ARK_API_KEY`；未指定时继续使用智谱官网及其密钥。默认服务不读取新的平台环境开关，也不会自动迁移既有成员。方舟 GLM 声明 prompt-JSON 模式，不发送未经声明的原生 JSON Object 参数，不发额外 probe。完整计划绑定平台、协议、端点、请求体和固定模型 ID，按 Responses 完成状态验收。

新计划明确要求中文事实/推断，逐字引文保留原语言；输入增加按正文版本和段落 ID 去重的阅读范围。旧冻结包、旧输入和旧回执不会重新签名，也不把提示修改后的结果当成严格同条件模型比较。

真实执行支持 `--password-dialog`，使用本机圆点密码框，支持 Ctrl+V；取消或过期不继续执行，控制字符及多行内容在本机拒绝。窗口只保存无密钥的阶段记录，密钥仅在当前进程中。默认仍支持控制台隐藏输入，两个入口都必须先通过候选、计划哈希和有效期检查。Windows 以桌面会话中的 PowerShell 7 启动；不要在不可见的沙箱窗口里要求用户粘贴，也不要把“按任意键关闭”当作密钥入口。

新一轮 GLM/DeepSeek 的具体模型、完整输入、上限、价格和有效期仍需形成新的消费计划。单项最多一次，原独立库总上限仍为三次，已经完成的豆包调用计入总数。ChatGPT 人工协作使用另一份独立数据副本，避免手工导出状态改变尚待执行的 API 证据包；没有真实用户返回时，不能伪造 RESULT_IMPORTED 或 API_REVIEW。
