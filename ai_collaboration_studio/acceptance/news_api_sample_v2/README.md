# 独立单样本验收 v2：遮罩粘贴与安全 HTTP 状态

v2 位于新目录，不修改 v1 执行器、已消费计划或既有回执。**历史真实尝试仍为 1 次、failed；v1 数字 HTTP 状态仍是 UNKNOWN。** 改善输入方式不代表获准第二次调用，旧授权不可复用。

## 改动

- 新的 Windows tkinter 遮罩输入框支持 Ctrl+V、右键粘贴，并显示“已输入 N 个字符”。不显示密钥字符；不支持从参数、文件、环境或旧凭据中取 key。
- 输入与现有 backend 规则一致：1–4096 个 ASCII 可见字符，不允许空格、换行或任何控制字符，也不自动 trim。输入错误会显示原因；点击取消、Esc、关闭窗口或授权过期立即取消。关闭后再次按 Enter 或重复按钮回调不能继续。
- 字符数只确认输入控件收到内容，不验证密钥有效性；允许单个可见字符也是沿用现有 backend 格式合同。本次旧 HTTP 失败不能因此归因为粘贴失败或密钥错误。
- 只有显式 `execute`、新的匹配计划/授权文件 SHA、新授权有效期及 `--allow-real-provider` 全部通过，才可能打开输入框。默认 prepare/inspect 不弹窗、不提示 key、不请求网络。
- 专用 launcher 结束后直接退出，**没有明文 Read-Host 等待提示**。不要在已结束的旧窗口里粘贴密钥。
- 新 `transport-result.json` 只保存白名单数字 HTTP 状态、开始/响应 UTC、单调耗时及安全标志；未知状态用 null/UNKNOWN。HTTPError 的整数 `code` 会保存；原始错误正文、HTTP headers 和密钥不会保存。

## 不变的边界

固定模型 `doubao-seed-2-1-pro-260915`，固定 Doubao Responses endpoint、一个公开已保存 RSS 快照、900 输出 tokens，应用估算预约上界 0.124536 元。**45 秒是 socket timeout，不是整个请求的墙钟硬截止；预约估算也不是供应商账单硬上限。** 不探测、不重定向、不重试。

独立消息 HTTP → 原 `news_response` → `NewsAnalysis.request` → worker → 真实 `_doubao` → 原 validator → 本次独立结果库 → GET `ai_analysis`。仅最终传输入口在离线测试中替换为假实现。生产 8770、生产数据、采集器和现有手动/自动模式不变。单样本不能代替自动模式真实验收、长期稳定性或旧 v8 质量合同。

原 pending 预约提交后，排他创建并 fsync `spent.json`；真正打开请求前再消耗进程级一次计数。失败、超时、未知或不完整消费回执均不能恢复次数。`started.json` 出现后计划不可重新启动，包括用户取消；不会自动重复弹窗。

密钥只在用户操作的新进程内存中短时使用。确认/取消会清空输入控件和会话引用，但 Python 无法保证内存字节物理清零。真实 adapter 导入使用空凭据配置模块、禁止环境代理自动发现，不读取现存 `.env.local` 或环境凭据，不回退尝试其它连接。

## 新计划（不发送请求、不弹窗口）

使用新的、尚不存在的输出目录；不要复用 v1 已消费目录。样本 JSON 保存在 Git 之外，仅提交代码中的合成测试资料。

```powershell
& '<python.exe>' -I -S -B -X utf8 '<project>\acceptance\news_api_sample_v2\runner.py' prepare --sample '<external-public-sample.json>' --sample-sha256 '<sample SHA256>' --out '<new-v2-run-directory>'
& '<python.exe>' -I -S -B -X utf8 '<project>\acceptance\news_api_sample_v2\runner.py' inspect --plan '<new-v2-run-directory>\plan.json' --plan-sha256 '<printed plan SHA256>'
```

生成 `news_single_rss_plan_v2` 与 `approved:false` 的 `news_single_rss_authorization_v2` 模板，绑定 backend、v2 Python 文件和 launcher 的实际字节。代码/样本/合同变化会使核验失败，必须准备新计划。

## 必须另获授权的真实执行

用户需批准新具体样本/计划；另保存授权文件，字段为 `schema:news_single_rss_authorization_v2`、`approved:true`、匹配的 `plan_sha256/grant_id`、`max_calls:1`、`max_reserved_micro_cny:124536`、真实批准及到期时间（间隔最多 15 分钟）和明确批准引用。不能把旧授权或改进输入体验的请求当成再调用授权。

```powershell
& '<python.exe>' -I -S -B -X utf8 '<project>\acceptance\news_api_sample_v2\runner.py' execute --plan '<new-v2-run-directory>\plan.json' --plan-sha256 '<plan SHA256>' --authorization '<new-authorization.json>' --authorization-sha256 '<authorization SHA256>' --allow-real-provider
```

也可由 `launch-approved-sample.ps1` 显式传入 `-PythonExecutable/-Plan/-PlanSha256/-Authorization/-AuthorizationSha256`；它没有 key 参数，不会在无参数时交互索要内容，结束即退出。实际固定 Python 路径放 Git 之外的本地启动配置。脚本只是入口，runner 执行全部授权约束；计划同时绑定 credentials.py、其它 v2 Python 文件和 launcher 字节。**当前尚未批准或执行第二次真实请求。**

## 独立输入窗口检查（不使用真实密钥或计划）

`ui_smoke.py` 默认只显示帮助。显式加 `--synthetic-input-only` 才打开独立演示窗口，只接受公开合成字符串 `SYNTHETIC_DEMO_KEY`；可验证 Ctrl+V、右键粘贴、字符数、遮罩和取消。它不导入 runner/backend，不读取授权/数据库，不发网络请求，也不消耗真实计划。请勿输入真实密钥。

```powershell
& '<python.exe>' -I -S -B -X utf8 '<project>\acceptance\news_api_sample_v2\ui_smoke.py' --synthetic-input-only
```

演示只报告是否接受合成输入或取消，不打印输入内容；120 秒后自动取消。该命令必须由用户或主线程明确启动，安装和 prepare 不会自动打开它。

## 必要验证

`tests/test_credentials.py` 使用 MockTk，验证粘贴事件、字符计数、控制字符拒绝、取消与关闭后重复回调；不打开真实窗口或读取剪贴板。`tests/test_runner.py` 使用本机 HTTP 和假模型传输，禁止外网/8770；验证哈希/期限、一次性预约、去重、状态码与敏感内容不落盘。

```powershell
& '<python.exe>' -I -S -B -X utf8 '<project>\acceptance\news_api_sample_v2\tests\test_credentials.py' -v
& '<python.exe>' -I -S -B -X utf8 '<project>\acceptance\news_api_sample_v2\tests\test_runner.py' -v
```

MockTk 通过不等于已经验证用户机器上的真实 GUI/剪贴板；假 HTTP 状态不补足 v1 缺失的状态，也不证明真实 provider 或模型质量恢复。
