# 离线冻结回归证据（2026-08-12）

## 权威基线

最终基线在源码冻结、系统临时 runtime 和显式临时 SQLite 下执行：

```powershell
python -u scripts\run_backend_tests_isolated.py --layer full --verbosity 1 --durations 30
```

- `1229/1229` 通过，耗时 `844.932s`，退出码 `0`。
- 网络审计版本：`backend_test_network_audit_v2`。
- 合法临时回环连接：`341`。
- 主测试进程阻断命中：`blocked_attempt_count=0`。
- Python 子进程阻断命中：`child_blocked_attempt_count=0`。
- 正式端口 `8770/11111` 均在 socket 系统调用前失败关闭；非回环连接和外部 DNS 同样失败关闭。
- Provider 密钥与代理变量在导入测试模块前清除；Futu 地址固定到进程内模拟离线的 `127.0.0.1:1`。

测试分层的先行兼容证据为：migration `87/87`、core `173/173`、domains `106/106`。分层结果只用于定位；最终完整结论以上述 full discovery 为准。

## 大型群聊积压性能优化

`StructuredMentionTests.test_large_skipped_interjection_backlog_cannot_finalize_round_partial`
会创建 `103` 条定向插话，验证插话失败时仍遵守“最多连续两条插话后推进正式发言”的公平性规则，并最终完成全部正式发言及发言合同审计。

分析发现，同一个失败插话原先会在公平性检查、成员选择和失败事件后重复构建完整收敛视图；其中每次构建都会重新读取房间快照、插件生命周期和发言合同。现在：

- 成员选择复用本次公平性检查已经得到的闭合收敛视图；
- 失败插话只在 SQLite `PRAGMA data_version` 证明事件 yield 期间没有其他连接提交时复用该视图；
- 观察器以 `mode=ro`、`query_only=ON` 打开；不可用或读取失败时自动退回完整重算，不影响正确性；
- 缓存只跨越紧邻的失败插话继续点，不跨正式发言、成功 AI 回复或未知数据库变更；
- 新回归在两个流事件之间另开 Store 连接写入消息，证明 `data_version` 变化会使缓存立即失效并重新计算。

冻结前的 full 基线中该用例为 `62.763s`；本次权威 full 中为 `11.707s`，观察到下降约 `81.3%`。完整 discovery 从上一记录的 `1130.290s` 降至 `844.932s`（约 `25.2%`）；总耗时仍会受机器负载影响，不能把全部差值都归因于单一优化。

相关定向证据：mentions 模块 `25/25`；mentions、orchestrator、convergence、checkpoint、pause/resume、turn-contract 和 turn-envelope 联合回归 `179/179`。最终 full discovery 已再次覆盖这些测试。

## 网络隔离闭环

- 主 runner 允许测试自己创建的动态回环端口，但拒绝 `8770/11111`、任何非回环目的地和外部 DNS。
- 已连接 socket 的 `send`/`sendall` 会重新核验实际 peer，不能通过临时替换 `connect` 绕过目的地门。
- runner 在每次运行前启动一个新 Python 子进程，验证 `sitecustomize` 硬门已在脚本执行前安装。
- 测试创建的 Python 子进程会继承该硬门；禁用目标命中会先追加 PID/目的地审计，再以专用退出码 `86` 终止。
- 当前测试树中所有 `subprocess.run`/`Popen` 调用均启动 Python；相关四个模块定向回归 `31/31` 通过。

## Windows 本机 HTTP 稳定性修复

安全门原先会在读取 POST body 前返回 `403/415`。Windows 在关闭仍有未读入站数据的 TCP socket 时可能发送 RST，使客户端丢失已写出的 4xx 响应并报 `WinError 10053`。

修复后，被拒绝且具有明确、受限 `Content-Length` 的请求会先有界清空 body，再显式关闭连接；分块、异常长度或超限 body 仍直接失败关闭，不做无界读取。

- 修复前同一 HTTP 安全用例连续运行 100 次：`85/100`，出现 15 个 `10053`。
- 修复后同一压力：`100/100`，网络审计阻断命中 `0`。
- 新增 20 轮、每轮四类拒绝请求的回归覆盖；最终 full discovery 包含该测试。

## 前端基线

- Node 前端单测：`318/318` 通过。
- Vite 生产构建：`1673 modules`，成功。
- 主入口 `482.39 kB`（gzip `146.03 kB`），没有超过 500 kB 的 chunk 警告。
- 前端仍使用宿主静态代码分割；football/stock/Action Desk 等重面板不进入首屏 eager bundle。

## 边界说明

- 本证据不启动正式 `8770`，不打开或连接 Futu/OpenD，不调用真实 Provider，不写正式 SQLite/WAL。
- 完整测试通过不等于正式数据库迁移获准。正式迁移仍必须经过只读预检、清单、可验证备份和用户显式授权。
- 外部进程是否监听 `11111` 是时点状态；即使外部 OpenD 已由用户或其他程序启动，本 runner 仍无法连接该端口。
- 项目仍不是 Git 仓库；未初始化 Git，也未清理或覆盖用户文件。

最终只读边界核验使用 SQLite `mode=ro&immutable=1` 和 `PRAGMA query_only=ON`：正式库 `integrity_check=ok`、外键违规 `0`、迁移记录 `31`。核验前后主库、WAL、SHM、journal 的存在性、大小和 SHA-256 完全一致；主库仍为 `B32E88A0C0BE5DB2D052904221C6C85D1B1C7862FD76F45EB8DF08B7EC41CC05`，WAL 为 `0` 字节。

该时点 `8770` 无监听；外部 `FutuOpenD.exe` 仍在 `127.0.0.1:11111` 监听，但已建立客户端连接为 `0`。本任务没有启动、连接或终止该进程。
