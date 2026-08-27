# Manual ChatGPT 只读运营摘要与 A/B 回放

## 边界

`backend.manual_chatgpt_operations` 是报告进程，不是新的模型席位。它只读取显式指定的已有 SQLite 文件；连接固定为 `mode=ro` 和 `PRAGMA query_only=ON`，并复用路径身份、reparse-point 和会话完整性检查。它不初始化 schema、不导入 ChatGPT 结果、不调用 Provider/市场接口，也不修改正式数据库。

ChatGPT Scheduled Tasks、桌面 Work、Codex 自动化和 Windows 任务计划程序是不同的调度/权限层。OpenAI 官方 [Scheduled tasks 文档](https://learn.chatgpt.com/docs/automations) 说明：桌面端任务可以使用本地项目，并在项目目录或隔离工作树运行，但需要电脑和桌面应用保持运行；Web 任务可以使用上传上下文与连接工具，却不能直接访问电脑上的本地目录。报告命令仍不自行推断权限，只有调度器被明确授予项目和数据库读取权限后才可执行；否则任务只应发送运营提醒。系统不写死 Pro 任务数量或模型限制。报告 JSON 输出到 stdout，由调用方决定是否展示或保存。

Secure MCP Tunnel 的本地预检由 `backend.secure_mcp_tunnel_preflight` 单独完成。输入必须是 `secure_mcp_tunnel_preflight_evidence_v1` 闭合 JSON，只记录布尔状态，不保存密钥、Token 或 `tunnel_id` 原文。预检分开报告 Platform `Tunnels Read/Use/Manage`、Tunnel 身份、运行时 API Key、目标 ChatGPT workspace 关联、developer mode、`tunnel-client`、出站 HTTPS、本地 MCP 可达性和 `doctor` 运行态；其中 Manage 只用于创建或编辑，不是使用已有 Tunnel 的必要条件。所有输入仍属于用户声明，因此即使全部为真，报告也只写 `runtime_connection_verified_by_declared_evidence=true`，不会把它升级为外部权限真实性证明。命令不连接 OpenAI、不启动服务，也不读写数据库。

```json
{
  "version": "secure_mcp_tunnel_preflight_evidence_v1",
  "tunnel_id_available": false,
  "runtime_api_key_available": false,
  "platform_permissions": {"read": false, "use": false, "manage": false},
  "target_chatgpt_workspace_associated": false,
  "chatgpt_developer_mode_enabled": false,
  "outbound_https_available": false,
  "tunnel_client_available": false,
  "local_mcp_reachable": false,
  "doctor": {"executed": false, "healthy": false, "ready": false, "connected": false}
}
```

## 日报口径

`daily-summary` 以指定时区的前一自然日为 Provider 使用窗口，并输出：

- 每个房间最新且完整性通过的非 `FROZEN` manual_chatgpt 任务；最新记录损坏时不会回退到旧记录。
- 全部 `WAITING_FOR_CHATGPT` 与 `CONTEXT_STALE` 任务。
- 超过 `waiting_expiry_hours` 的非终态任务。该标签只是运营年龄提醒，不改变持久化状态，也不等于上下文已经失效。
- `artifact_evidence` 中 `unreviewed / disputed` 的引用提醒。日报不重放完整 artifact evidence review chain，因此明确标记为运营提醒而非完整性证明。
- 昨日 Provider 调用数、状态、账本中已记录的 Token 与耗时。`usage_sha256` 失败的使用量不计入聚合。
- `cost_usd` 汇总为已记录美元费用；旧的无币种 `cost` 单独汇总并标记单位未知；没有费用字段时为 `unavailable`，不是零。
- 昨日新建 Manual ChatGPT Bundle 中的独立 API 审查计划估算。它排除人工 ChatGPT 订阅，并始终标记 `not_actual_spend=true`。

`report_sha256` 只用于发现报告内容变化，不是签名或外部公证。

CLI 默认输出 JSON。若调度器需要直接展示人类可读日报，可加入 `--format markdown`。Markdown 渲染器会重新校验 `report_sha256` 和只读自动化边界，转义动态文本，并明确区分已记录美元费用、单位未知的旧费用、缺失费用和非实付计划估算。该格式不创建任务、不导入结果，也不授予本地文件或数据库访问权限。

### Scheduled Tasks 交付契约

先生成不含数据库路径的确定性契约，供用户在普通对话中检查提示，再决定是否在 ChatGPT 桌面端创建 Scheduled Task：

```powershell
$env:AI_STUDIO_SKIP_LOCAL_ENV = "1"
python -m backend.manual_chatgpt_operations scheduled-task-contract `
  --timezone Asia/Shanghai `
  --local-time 09:00 `
  --waiting-expiry-hours 24 `
  --max-items 50
```

输出的 `manual_chatgpt_scheduled_task_contract_v1` 包含建议日程、可复制任务提示、精确命令参数、所需环境变量名、只读边界和 `contract_sha256`。它始终标记 `external_task_created=false`，不包含数据库路径，不假定账户任务数量或模型可用性，也不创建、更新或授权外部 Scheduled Task。

真正的调度运行入口不接受命令行数据库路径，只读取操作员在任务环境中单独绑定的 `AI_STUDIO_OPERATIONS_DATABASE`。它还要求 `AI_STUDIO_SKIP_LOCAL_ENV=1`；任一条件缺失都会在数据库连接前失败关闭：

```powershell
$env:AI_STUDIO_SKIP_LOCAL_ENV = "1"
$env:AI_STUDIO_OPERATIONS_DATABASE = "<operator-approved-existing.sqlite3>"
python -m backend.manual_chatgpt_operations scheduled-daily-summary `
  --timezone Asia/Shanghai `
  --waiting-expiry-hours 24 `
  --max-items 50
```

以上命令是安装前测试与未来授权运行说明，不代表当前 workspace 已启用 Scheduled Tasks、本地项目或数据库访问已获授权，也不代表首轮运行已经人工复核。根据 OpenAI 官方 Scheduled Tasks 文档，桌面端本地项目任务需要电脑与应用保持运行；Web 任务不能直接访问电脑上的本地目录。任务创建和前几次运行复核仍由用户在 ChatGPT 桌面端或 Web 的 Scheduled 界面完成。

## A/B 数据集

输入版本是 `manual_chatgpt_ab_replay_dataset_v1`，必须包含 20–30 个唯一 Case。每个 Case 的 A/B Arm 都包含：

```json
{
  "model_calls": 12,
  "input_characters": 12000,
  "estimated_tokens": 3000,
  "api_cost_usd": 0.12,
  "wait_ms": 120000,
  "human_operation_minutes": 12,
  "citation_refs_total": 10,
  "citation_refs_passed": 8,
  "final_conclusion_id": "option_a",
  "basis": {
    "model_calls": "measured",
    "input_characters": "measured",
    "estimated_tokens": "estimated",
    "api_cost_usd": "recorded",
    "wait_ms": "measured",
    "human_operation_minutes": "measured",
    "citations": "measured",
    "final_conclusion": "measured"
  }
}
```

允许的依据只有 `measured / recorded / estimated / projected / unavailable`。值为 `null` 时依据必须是 `unavailable`，有值时不能写 `unavailable`。`declared_source_kind=historical_round` 还必须提供源快照 SHA-256，但这只封印用户提供的数据，不证明它确实来自历史运行；报告继续标记 `declared_historical_source_truth_verified=false`。

如果已经为每个历史 Case 准备经过人工审阅的脱敏指标快照，可使用 `historical-ab-replay`。当前新快照严格采用 `manual_chatgpt_ab_source_snapshot_v3`：顶层只允许 `version / case_id / a_source / b_source / a / b`。`a_source` 必须独立绑定旧流程 `room_id / round_id / source_record_sha256`，`b_source` 必须独立绑定 Manual ChatGPT `room_id / round_id / session_id / source_record_sha256`；两臂都必须带 `human_reviewed=true`。v3 要求 A 臂人工操作分钟为正数且依据只能是 `measured` 或 `recorded`；B 侧 `human_operation_record` 必须与 B 臂人工操作分钟完全一致，依据固定为 `recorded`，来源声明固定为经操作员复核的计时器或日志，并明确不是从 dispatch/import 墙钟时间推断。指定目录只能包含 20–30 个普通 JSON 文件。命令会拒绝 reparse point、非 JSON、重复 Case、任一侧复用 room/round、任一侧复用源哈希、B 侧复用 session、预测/缺失/不一致的人工时间、超限文件、读入期间发生变化的文件和任何额外字段，并从规范化快照计算 `source_snapshot_sha256`。

旧 v1/v2 快照仍可只读回放，以免破坏历史文件。v1 只有一组共享 `room_id / round_id`，因此不能得到 `ready_for_dual_arm_replay=true`；v2 已有双臂身份，但没有闭合的 B 侧人工操作时间来源，所以不能得到 `ready_for_complete_ab_replay=true`。只有 20–30 个条目全部为 v3、全部结构有效且八项指标覆盖完整时，才返回这个最严格的本地就绪状态。报告仍明确保留 `source_record_contents_verified=false` 与 `source_record_hash_recomputation_performed=false`，因为回放不读取外部旧系统记录或完整 B 臂导出。所有版本也都保持 `declared_historical_source_truth_verified=false`：本地哈希、人工声明和计时记录不等于外部来源真实性认证。

收集期间可以先运行 `historical-ab-status --source-directory <directory>`。该命令允许少于 20 个快照，逐项应用与最终回放相同的文件和 schema 校验，返回有效唯一 Case、无效文件及错误码、`remaining_to_minimum`、`capacity_remaining`、`ready_for_replay`、`ready_for_dual_arm_replay` 和最严格的 `ready_for_complete_ab_replay`，并按版本列出 v1 共享身份、v2/v3 双臂绑定及 v3 人工时间绑定数量。它还会对全部六项数值指标、引用通过率和最终结论变化分别报告 A/B 可用数、可比 Case、覆盖率及 basis 分布；引用总数为零时不把通过率视为可用，缺失值也不按零处理。覆盖率只是描述性数据质量信号，不是预设目标。诊断最多扫描 31 项；更大的目录会返回真实发现数并标记 `scan_truncated=true`，不会生成无界响应。状态报告不会返回绝对目录、不会读取数据库、不会修改文件，也不会调用 Provider 或市场接口；“就绪”只代表对应层级的本地契约满足，不代表效果达标或历史来源真实性已认证。

对于新流程的 B 臂，可在数据库访问另行获批后，从一个已 `FROZEN` 且完整性通过的 Manual ChatGPT 会话导出可审阅指标；命令使用 `mode=ro + query_only`，只向 stdout 输出 `manual_chatgpt_ab_arm_export_v1`，不创建 Case 文件：

```powershell
$env:AI_STUDIO_SKIP_LOCAL_ENV = "1"
python -m backend.manual_chatgpt_operations historical-ab-export-b-arm `
  --database "<operator-approved-existing.sqlite3>" `
  --room-id "<room_id>" `
  --round-id "<manual_chatgpt_round_id>"
```

导出会把已完成 API 审查次数、事件链、决定和引用绑定到源哈希。总 `model_calls` 仍标为 `projected`，因为 ChatGPT Panel 回合只能证明为人工协议声明，不能证明外部调用真的发生；API 审查次数则来自完整性通过的独立调用账本。`input_characters` 是冻结上下文字符数乘计划/验证调用数的投影，Token 是确定性估算。`wait_ms` 只使用事件链封印的 dispatch 到成功导入墙钟时间；Provider attempt 的 `elapsed_ms` 会作为未纳入指标的观察值单列，因为它不在事件链封印内。`human_operation_minutes` 始终保持 `null / unavailable`，因为事件时间不能证明人的主动操作时间。API 美元费用优先使用全部 Provider attempt 中完整且哈希通过的 `cost_usd`；否则只在冻结费率估算存在时标为 `estimated`，再否则保持不可用。引用通过数来自已校验结果中的引用出现次数，最终结论来自用户确认选项。

该导出只提供 B 臂并明确返回 `complete_ab_case=false`。操作员仍需提供一个只含 A 臂指标的已复核 JSON；在另行批准只读访问该 SQLite 后，可用下面的组合命令生成一个可直接保存的 v2 快照。命令只读基线文件和 SQLite、只向 stdout 输出 JSON，不创建或覆盖 Case 文件：

```powershell
$env:AI_STUDIO_SKIP_LOCAL_ENV = "1"
python -m backend.manual_chatgpt_operations historical-ab-compose-v3 `
  --database "<operator-approved-existing.sqlite3>" `
  --case-id "<case_id>" `
  --baseline-arm "<reviewed-a-arm.json>" `
  --baseline-room-id "<legacy_room_id>" `
  --baseline-round-id "<legacy_round_id>" `
  --room-id "<manual_chatgpt_room_id>" `
  --round-id "<manual_chatgpt_round_id>" `
  --b-human-operation-minutes "<reviewed-active-minutes>" `
  --acknowledgement I_REVIEWED_BOTH_AB_ARMS
```

组合器把规范化的 A 臂指标及其旧流程身份封印成 A 侧记录哈希，把完整 `manual_chatgpt_ab_arm_export_v1` 的 `export_sha256` 用作 B 侧记录哈希，并带入冻结 session ID。`--b-human-operation-minutes` 必须是 0–7 天范围内的正数，系统把它标为 `recorded` 的人工复核计时/日志值；它不会从事件时间推断，也不会回写 SQLite。精确 acknowledgement 只记录操作员已审阅两臂，不证明计时、旧记录、外部 ChatGPT 调用或历史标签真实。本地哈希与完整性也只证明当前读取内容一致。旧 `historical-ab-compose-v2` 仍为已有调用方保留，但其输出不会包含这个人工时间来源，也不能进入完整 A/B 就绪状态。

```powershell
$env:AI_STUDIO_SKIP_LOCAL_ENV = "1"
python -m backend.manual_chatgpt_operations historical-ab-replay `
  --source-directory "<20-to-30-reviewed-source-snapshots>" `
  --dataset-id "<reviewed-dataset-id>"
```

## 指标决策框架

主指标：

- `human_operation_minutes`：用户完成同一研究任务所需人工操作时间。它直接衡量“更舒服”的目标，但必须由日志或复盘计时支持，不能从点击次数随意换算。
- `model_calls`：A/B 可比 Case 中模型调用总数。需要区分实测与计划值。
- `api_cost_usd`：只接受明确美元口径；无币种费用不能混入。

诊断指标：

- `input_characters` 与 `estimated_tokens` 用于解释上下文压缩；Token 估算不等于模型 tokenizer 实测。
- `wait_ms` 用于解释端到端时延；报告同时给出覆盖率，缺失 Case 不按零处理。

质量护栏：

- `citation_pass_rate = passed / total`，按 A/B Case 的总引用聚合。分母为零时不可用。
- `final_conclusion_change_rate` 只在 A/B 两侧都有稳定结论 ID 时比较。变化不是天然的好或坏，而是需要复核的质量信号。

每项聚合都返回 `comparable_cases / coverage_rate / basis_counts`。只有数据集显式提供 `targets` 时才评估 `met / not_met`；系统不预设 66.7% 席位占比、下降比例或成本目标。任何含合成 Case 的报告都标记为 `contract_fixture_only`，不能用来宣称历史效果。
