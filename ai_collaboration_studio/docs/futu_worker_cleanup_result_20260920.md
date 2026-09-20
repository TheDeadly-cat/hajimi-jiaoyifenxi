# Futu managed worker 离线清理修复

2026-09-20，在交接基线 `4d2bf623bce007ab95b465a5861677602f0485b0` 上复现了两项已知失败，修复代码提交为 `6fad871a0682d53437d87e4a98340f59fa5c0542`。本次没有连接真实 Futu/OpenD，也没有重跑全量回归。

## 原因与改动

两项失败分别是 `test_crashed_managed_worker_restarts_on_next_request_only` 和 `test_one_shot_and_managed_use_same_bounded_protocol` 的 managed 情况。响应合同与进程复用检查已通过，失败点均为 `broker.stop()` 返回 False。

针对原代码的异常跟踪记录：进程等待已返回、管道关闭后，`TemporaryDirectory.cleanup()` 抛出 `PermissionError`，`errno=13 / winerror=32`，临时目录仍存在。证据指向 Windows 目录占用解除与立即清理之间的竞态；这不是 Futu 服务器响应失败。未进一步把观察扩大为所有后代进程均已终止的证明。

现在仅对目录清理的 Windows sharing violation 32 每隔 10 毫秒重试，单调时钟窗口为 2 秒。只有清理成功才释放临时目录所有权；持续占用到期仍返回 False 并保留句柄，供后续显式停止再清理。其他权限错误立即失败，进程未确认退出时仍不进入目录清理。这里的 2 秒是重试调度窗口，不是对操作系统文件调用耗时的硬保证。

未改变进程启动、行情请求、固定目标、符号白名单、协议、SDK、API 调用、交易权限或默认启用状态。

## 定向验证

- 原代码：2 项既有检查均失败，首次失败日志保留。
- 新增的瞬时占用、持续占用、其他权限错误三项回归：修复前 2 失败 / 1 通过，记录保留。
- 修复后经 `scripts/run_backend_tests_isolated.py` 运行 broker、runtime coordinator、Futu anomaly adapter 三个模块：**35 项通过，0 失败，0 错误，0 跳过**。包含原失败两项及新增三项，重复执行不累计覆盖。
- 静态安全基线：**7/7 通过**。这不是完整安全审计。
- 测试网络审计：允许回环连接 0、模拟离线连接 0、阻断尝试 0、子进程阻断尝试 0。进程协议测试用合成 worker；适配器集成测试使用替身，没有真实 OpenD 请求。
- 执行的是本 API pilot checkout 的代码；只读复用交接指定的受管 Python。没有修改旧源码或受管环境。

保留的本机日志为 `futu-baseline.log`、`futu-cleanup-trace.log`、`futu-regressions-before-fix.log`、`futu-targeted-after-fix.log`、`static-security.json`。完整本机证据根为父目录的 `offline-closeout-20260920`，不随仓库上传。修复后测试代码与本提交源文件内容一致，文档提交不重新标记历史模型调用所用源码身份。

## 数据与交付边界

旧 API 试验数据库仍为 SHA-256 `f03b82af72c59d57fbea488320b775ea3774e650f5c5fde2da2fdd3c0f4c16b3`；手工 ChatGPT 副本仍为 `e4c019f46e1e0f1007158e6a5856585841d52f4d9709e19cbd76ef0c30c82dfa`。核对使用文件哈希，没有打开数据库；无 WAL/SHM 残留。历史三次 API 授权已消费且不重置，手工副本继续等待独立复核。

交接基线的 Actions `35471982969`（PR）和 `35471981223`（push）在本阶段开始及中途核对时仍为 `in_progress`；不能记为通过。后续以 PR #19 所指精确 HEAD 的真实终态为准。定向通过不代表全量回归、在线 Futu、无人值守、合并或部署验收。
