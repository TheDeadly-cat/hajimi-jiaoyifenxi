# 一条公开 RSS 摘录的独立真实验收

此工具默认只准备、核验本地文件。真实请求必须另获明确授权，并同时提供已核验的计划和独立授权文件 SHA256；`prepare` 生成的授权模板为 `approved: false`，不能执行。没有提供任何真实密钥、样本正文或已批准授权文件。

该工具不修改生产 backend/API/session，不连接生产 8770，不启动新闻采集器、不刷新来源、不读生产数据库或既有密钥。产品原有 24 小时/36 次/应用预约 3 元的手动与自动模式保持原样。本次单样本不能替代真实自动模式验收或 24 小时稳定性验收。

## 执行边界

- 输入：事先通过公开只读接口保存的一条官方 RSS 消息；固定外部样本文件 SHA、item ID、完整快照 SHA（含 `updated_at`）、产品 fingerprint、源码/策略/schema SHA。样本正文只能留本地，不提交 Git。
- 模型：`doubao-seed-2-1-pro-260915`；固定 Doubao Responses 接口；最多 900 个输出 tokens；45 秒超时；授权有效期最多 15 分钟。
- 限额：独立执行器最多一次真实模型 POST。原应用公式对最多 16000 字节的预约请求估算上界为 **0.124536 元**；这不是供应商实际账单硬上限，不宣称已核验价格或实际账单。
- 模式：只允许手动；无探测、重定向、重试。成功、失败、超时及 UNKNOWN 均不返还本次授权。启动过的计划也不可重新执行。
- 凭据：仅在获准的 `execute` 中通过交互式 `getpass` 遮罩输入；不支持参数、文件或环境变量凭据。非交互输入/可能回显的 fallback 拒绝。仅本进程短时持有，结束清除会话；Python 不保证内存字节被物理清零。
- 隔离：专用随机 loopback 端口和新独立分析库；当前样本的只读快照 runtime；正常 HTTP 消息 → `news_response` → `request` → worker → 真正 `_doubao` → 原 validator → 独立 `runs` → GET `ai_analysis`。结果写回本次独立验收库，不写生产库。
- 真实 provider 导入前只装入最小空凭据配置模块，避免其配置导入读取 `.env.local` 或现存环境；禁用自动代理发现。使用直接 HTTPS，不尝试环境代理或代理失败回退。调用真实 adapter/parser 不代表验证了生产配置加载。
- `checked_at` 由真实时钟生成，其 freshness 等派生字段随之重新计算；输入 guard 使用同一时刻重建完整材料并严格比对。原始发布时间、发现时间和更新时间不得改写。

## 两道不可重用限制

正常 `NewsAnalysis` 先提交 `pending` 预约。`generate` guard 核验唯一消息、完整输入、合同和预约后，以排他创建方式写入 `spent.json` 并 `fsync`，成功后才允许真正 provider 调用。文件已存在、写入失败或内容不完整一律拒绝再次发送。即使刚落盘后就崩溃、实际尚未发送，也保守记为已使用且结果 UNKNOWN。

实际 `open_provider_request` 入口还核对 POST 地址、真正序列化后的 body、模型、schema、输出上限、45 秒超时，再落盘 `transport-started.json`、消耗进程级一次计数并打开请求。第二次调用在同进程也拒绝，结束时不恢复未受约束的 opener。完整执行记录包含启动/授权哈希、预约、发送尝试计数和最终结果；不含 key、请求头、原始错误正文。

这约束的是此经审查执行器的单一外发路径，不是生产通用 session 能力，也不是防止恶意改写 Python 进程的操作系统隔离。超时不能证明供应商没有处理，必须保留 UNKNOWN。

## 准备和核验（不请求网络，不提示密钥）

以下命令使用固定 Python，尖括号由本地实际绝对路径/哈希替换，密钥永不进入命令：

```powershell
& '<python.exe>' -I -S -B -X utf8 '<project>\acceptance\news_api_sample\runner.py' prepare --sample '<external-public-sample.json>' --sample-sha256 '<sample SHA256>' --out '<new-empty-acceptance-directory>'
& '<python.exe>' -I -S -B -X utf8 '<project>\acceptance\news_api_sample\runner.py' inspect --plan '<acceptance-directory>\plan.json' --plan-sha256 '<printed plan SHA256>'
```

`prepare` 拒绝已存在的输出目录及没有可分析摘录的样本。源代码、样本或合同变化后 `inspect` 拒绝；必须重新准备并重新审阅计划。首个案例优先为既有 Fed 公开摘录；若发布时间超过 24 小时，应明确是旧消息阅读，不视为新事件或来源访问恢复证据。

## 仅在用户批准后执行

保留 `authorization.template.json`；另创建 `authorization.json`，仅把真实用户明确批准的边界写入其中。所有字段如下：

```json
{
  "schema": "news_single_rss_authorization_v1",
  "approved": true,
  "plan_sha256": "已审阅的计划SHA256",
  "grant_id": "计划中的固定grant_id",
  "max_calls": 1,
  "max_reserved_micro_cny": 124536,
  "approved_at": "用户批准后的实际带时区时间",
  "expires_at": "不晚于批准后15分钟",
  "approval_reference": "本次用户明确批准的可核验引用，不把模板或旧授权当批准"
}
```

核对授权文件 SHA 后，才可在用户可控制的交互终端运行：

```powershell
& '<python.exe>' -I -S -B -X utf8 '<project>\acceptance\news_api_sample\runner.py' execute --plan '<acceptance-directory>\plan.json' --plan-sha256 '<plan SHA256>' --authorization '<acceptance-directory>\authorization.json' --authorization-sha256 '<authorization SHA256>' --allow-real-provider
```

用户在该终端的 `getpass` 输入密钥；不要通过聊天、工具调用文本、日志或脚本注入。计划存在 `started.json` 即不重新执行，包括密钥取消、超时或其它失败。留存所有文件，按安全回执报告；任何再尝试需要另行明确授权。

## 必要的离线验证

`tests/test_runner.py` 只含合成材料/无效 key/假末端传输；外网与生产端口被阻断，测试保留真实本机 HTTP、worker、provider 解析、validator 和结果库链路。覆盖默认未授权、哈希/期限变化、提前持久化失败、timeout UNKNOWN、并发去重、禁止自动模式/其它 item、结果回读与 key 不落盘。

```powershell
& '<python.exe>' -I -S -B -X utf8 '<project>\acceptance\news_api_sample\tests\test_runner.py' -v
```

这些测试不能算真实 API 或模型质量通过。真实单次结果还需人工核对主体、事实、原句支持、条件、时间及未读全文限制；现行 `rss_excerpt_reading_v2` 不等于旧合成 `synthetic_material_review_v8` 合同，也不继承其评分。
