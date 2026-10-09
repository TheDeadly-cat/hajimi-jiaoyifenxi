# X-05：独立 Windows 进程退出记录器

入口：`scripts/record_news_review_exit.py`。它是一个可单独运行的标准库工具，读取指定的宿主和观察器进程，保存原生退出证据。它不导入产品后端、不打开数据库、不请求来源或模型、不控制目标进程，不改变现有 watchdog。

## 运行方法

使用 Windows 和 Python 3.14。先为**本次新运行**确定独立证据目录，在目标仍存活时启动记录器。输出目录必须尚不存在，其父目录必须已经存在；本地目录及其祖先不能是重解析点。每轮使用新的目录和 `run_id`。

从本次宿主启动回执和 `observer-pin.json` 取得对应 PID 与 `process_start_utc_ticks`。后者沿用产品的 .NET UTC ticks：从公元 0001 年起算的 100 ns 整数。JSON 中使用十进制字符串，不能经过 JavaScript Number 或浮点数。

下面仅说明请求格式，示例身份需要替换为本次运行的真实身份：

```json
{
  "version": "news_review_exit_request_v1",
  "run_id": "0123456789abcdef0123456789abcdef",
  "identity": {
    "candidate_sha": "b625221d4e517e496a1f2fe0d389ad4decc5d1ca",
    "policy_sha256": null,
    "activation_sha256": null,
    "observer_plan_sha256": null
  },
  "maximum_observation_ms": 1800000,
  "targets": [
    {"role": "host", "pid": 1234, "process_start_utc_ticks": "639000000000000001"},
    {"role": "observer", "pid": 5678, "process_start_utc_ticks": "639000000000000002"}
  ]
}
```

`candidate_sha`、策略、激活与观察器方案哈希都是调用者提供的关联标识。记录器不独立证明目标正在执行哪个提交或策略；正式新运行必须另外核对候选与批准材料。三个可空哈希允许隔离测试进程使用，不替代正式运行的身份材料。允许 1–2 个目标，角色与 PID 均不能重复；最长观察期限为 48 小时，到期只结束记录器的观察，不终止目标。

从 `ai_collaboration_studio` 目录执行；请使用本机已确认的 Python 路径。PowerShell 示例：

```powershell
$taskRequest = 'C:\ApprovedNewRun\exit-request.json'
$taskDigest = (Get-FileHash -LiteralPath $taskRequest -Algorithm SHA256).Hash.ToLowerInvariant()
python -I -S scripts/record_news_review_exit.py `
  --request $taskRequest --request-sha256 $taskDigest `
  --output 'C:\ApprovedNewRun\independent-exit-evidence'
$taskRecorderExitCode = $LASTEXITCODE
```

调用方还需要保存记录器的 stdout 和真实退出码。目标全部出现 `target-<role>-bound.json` 后，才能确认记录器已经持有这些进程对象的句柄。绑定失败的目标保持未确认，不能用另一个进程补位，也不能事后改写请求后继续同一轮。

## 原生身份与退出证据

记录器只申请查询与同步权限，先比较创建时刻，然后在整段观察期间持有**同一个**句柄。这样 PID 复用不会把另一个进程当成原目标。

原生创建时间与退出时间来自 `GetProcessTimes`。工具将 FILETIME 的 1601 年起点转换为产品已有的 .NET UTC ticks 起点；内核/用户 CPU 时长不作该转换。原生终止须由 `WaitForSingleObject` 确认，再读取 `GetExitCodeProcess`。真正的退出码 259 仍作为退出码保存，不会被解释为进程仍存活。

`detected_at_ms` 是记录器检出终止的墙钟时刻；`native_exit_utc_ticks` 是 Windows 返回的退出时刻。这两个字段含义不同，不把检查节奏形成的检出边界称为真实死亡窗口。

相关原生契约：

- [OpenProcess：访问权限与打开失败](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openprocess)
- [GetProcessTimes：FILETIME 与未退出时的退出时间](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes)
- [WaitForSingleObject：进程对象的终止信号](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject)
- [GetExitCodeProcess：退出码 259 的限制](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getexitcodeprocess)

## 如何读结果

| 文件／信号 | 可支持的结论 |
| --- | --- |
| `recorder-start.json` | 本次请求、记录器身份与实际工具源文件哈希；目标初态未确认 |
| `target-<role>-bound.json` | 已按 PID 和创建时刻绑定，并持有该进程句柄 |
| `target-<role>-unconfirmed.json` | 绑定失败及 bounded 原因；不能推断目标已经死亡 |
| `target-<role>-exit.json` | 已取得的原生终止、退出码与退出时刻，分别给状态 |
| `recorder-final.json` | 汇总已取得事实、未确认项与记录器失败；不会自证自己成功落盘 |
| stdout 与实际退出码 | 调用方确认记录器完成的进程外信号，须与本次请求和回执共同核对 |

身份不匹配、打开/等待失败、观察期限届满，均保持 `UNCONFIRMED`，退出码未知时为 `null`。句柄终止信号已确认但退出码查询失败时，可保留“进程已终止”这个事实，同时明确退出码仍未知。它不会把目标的非零退出码当成记录器自身失败：例如目标退出 7，而完整留证的记录器退出 0。

记录器退出 **0** 只表示所有目标的身份、原生终止、退出码、原生退出时间与本轮写入流程均确认；其他可处理失败返回 **2**。异常仅保存受限错误码、异常类型和原生错误号，不保存异常自由文本、进程命令行、环境或密钥。

最终文件中的 `summary_persisted` 始终为 `null`，`recording_complete` 始终为 `false`：文件不能证明其自身之后的写入、同步和读回已经成功。最终写入及读回复核完成后，stdout 才报告 `summary_persisted=true`；调用方仍须保存实际退出码。`observations_complete` 描述终态观察和此前流程，不是最终文件自身落盘确认。

文件以新临时文件写入、flush、fsync，再排他发布和逐字节读回；既有回执不覆盖。目录身份每次发布复检。这些措施**不构成断电持久性证明**，也不宣称解决有同目录写权限攻击者造成的所有并发路径替换。若回执和 stdout 都无法留存，或记录器被外部强制终止，不能自证完成；只有已经保存的记录可用，缺失文件不证明某条代码未执行。

所有结果中的 `monitoring_acceptance`、`host_closeout_verified`、`ledger_drained_verified` 与 `power_loss_durability_proven` 保持 `false`。工具不加入现有 watchdog 判定，不发送通知，不替代宿主停止、排空、最终报告或独立账单核验。旧 B 轮退出原因仍是 UNKNOWN。

## 已执行的隔离验证

从冻结运行候选 `b625221d4e517e496a1f2fe0d389ad4decc5d1ca` 建立独立工程候选，仅新增工具、测试与本文。既有观察器、watchdog、宿主、账本、模型路由与批准逻辑未修改。

```powershell
python -X utf8 -W error::ResourceWarning scripts/run_backend_tests_isolated.py `
  tests.test_news_review_independent_exit --verbosity 2
```

本机 Windows / Python 3.14：**12 项通过，0 跳过，其中 5 项实际创建原生子进程并调用记录器 CLI**。正常双目标退出使用冻结候选的原生身份函数生成输入，验证与宿主／观察器 ticks 契约一致；覆盖实际退出 0/259、强制终止自有测试目标、创建时刻错误、观察到期，以及强制终止自有记录器后的未完成状态。其余检查覆盖写入失败、身份/等待/退出码失败、不覆盖旧证据和精度/JSON/哈希拒绝。只管理测试自行创建的进程。

网络隔离记录：父进程与受审计子进程的阻断计数均为 0。原生 CLI 使用 `-I -S` 独立启动，因此不加载测试入口的子进程 `sitecustomize` 守卫；其源代码只用标准库文件和进程 API，没有任何网络代码。不能把这个审计计数说成所有 `-I -S` 子进程都受 socket 守卫覆盖。

首轮沙箱临时目录权限错误的失败记录保留；正常系统临时目录下重新执行通过。身份起点兼容修正后的结果另行绑定最终工具/测试字节。以上是隔离进程验证，不是新运行的启动、排空、报告闭环或连续 24 小时成绩；新提交的远端 CI 必须单独核对。
