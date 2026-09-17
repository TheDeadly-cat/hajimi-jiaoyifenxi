# 模型服务接入

本轮优先豆包、千问、智谱；DeepSeek 是手动选择的备选。调整的是新增成员的默认选择和界面顺序，不迁移既有成员、历史身份、冻结研究包或已批准轮次。服务失败后不会自动重试或换供应商。

| 服务 | 项目 Provider ID | 密钥入口 | 接口 |
| --- | --- | --- | --- |
| 豆包 / 火山方舟 | `doubao` | `ARK_API_KEY` | `https://ark.cn-beijing.volces.com/api/v3/responses` |
| 千问 / 百炼通用 API | `qwen` | `QWEN_API_KEY`，兼容 `DASHSCOPE_API_KEY` | `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions` |
| 智谱 | `glm` | `GLM_API_KEY`，兼容 `ZHIPUAI_API_KEY` | `https://open.bigmodel.cn/api/paas/v4/chat/completions` |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` | `https://api.deepseek.com/chat/completions` |

模型 ID 以账户实际开通的模型为准。现有豆包、智谱和 DeepSeek 的兼容默认值不是新一轮真实调用授权；受控试验必须明确模型。千问没有静默默认模型；使用支持 JSON Object 与 `max_completion_tokens` 的 Qwen 模型，例如当前官方文档列出的 Qwen3.8-Max。正常适配器也允许北京业务空间专属域名，受控试验 v1 目前固定使用表中北京通用端点。

## 账户与套餐

- 豆包体验额度按账户、具体模型和有效期核对。“有体验包”不等于所有模型都可用，也不等于超出额度后必定停止扣费。需要核对控制台扣费设置，不能将估算写成实际零费用。
- 阿里云 Token Plan 个人版 Standard 的官方现行计量为 Credits，不能把一个宣传 Token 数当作所有模型统一可用的余额。它用于指定工具内交互式使用，不能直接用于自定义应用后端。本项目千问适配器使用通用按量 API，并在本地拒绝套餐端点；不会自动转用编程工具的套餐密钥。
- 智谱需要通用 API 的密钥及实际模型权限；编程套餐和普通 API 要分别核对。
- 密钥由用户在本机交互终端隐藏输入，只留在当前子进程中。不要发送到模型聊天，也不要把密钥放进命令参数、仓库、截图或日志。

## 页面操作

房间右栏的“模型路由与本机配置”现在提供四家执行器的批量切换按钮；未配置的按钮保持不可点击。“添加 AI 成员”的默认优先顺序为豆包、千问、智谱、DeepSeek；编辑历史成员保留原模型与执行器。成员可以手动选择执行器并填写具体模型 ID。

“本机配置检查”只说明本机配置，不证明账号余额、模型权限或真实网络连通。首次真实接入继续用独立数据副本及 `scripts/run_api_pilot.py`，先预览完整请求，再批准单次模型、输入、输出上限、时间与消费计划，最后隐藏输入密钥；不会额外执行多个探测请求。

千问 `generate_json` 映射 JSON Object；输出上限使用 `max_completion_tokens`，覆盖思考与回答。官方提示实际输出可能有最多 10 Token 的误差，不能把请求参数当作供应商侧账单硬上限。本地受控验收仍会将超过批准上限的结果记为不合格，并保留实际 Usage；不补发请求。豆包保持 Responses 参数、智谱保持已有 prompt-JSON 合同，严格验证终态及引用，不假装三家传输协议完全相同。

所有离线入口和 CI 会清除千问的两个密钥别名，原来源试用入口仍禁用全部 Provider。API 接入不会自动开启官网轮询、Futu、正式轮次、交易或人工结果导入。2026-09-13 的 DeepSeek 单次授权已经过期，不能复用。

官方接口核对日期：2026-09-17。[阿里云 Chat 参数](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)、[阿里云结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)、[Token Plan 个人版规则](https://help.aliyun.com/zh/model-studio/token-plan-personal-overview)、[智谱对话接口](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)、[火山方舟接入示例](https://www.volcengine.com/docs/82379/1795150)。
