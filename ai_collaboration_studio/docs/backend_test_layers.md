# 后端测试分层

统一入口仍是 `scripts/run_backend_tests_isolated.py`。无参数时保持完整 `unittest` discovery；传入 dotted test 名时保持原来的精确选择。分层只是新增的规范选择方式：

```powershell
python scripts\run_backend_tests_isolated.py --list-layers
python scripts\run_backend_tests_isolated.py --layer migration --verbosity 2
python scripts\run_backend_tests_isolated.py --layer core
python scripts\run_backend_tests_isolated.py --layer domains
python scripts\run_backend_tests_isolated.py --layer full --durations 30
```

层定义由 `scripts/backend_test_layers.json` 唯一管理：

- `migration`：P0 SQLite 启动、预检、备份、授权、提交、恢复和 owner 门。
- `core`：快速宿主内核、协议、启动计划、账本、安全、本地 HTTP 与源码备份门。
- `domains`：football、stock、candidate 及通用 domain adapter/round context 门。
- `full`：发现全部 `tests/test_*.py`。

manifest 是 closed/versioned 合同。未知字段、未知版本、缺失模块、非规范模块名、跨层重复或层顺序漂移都会在导入测试模块前失败关闭。`--layer` 不能与显式 dotted tests 或 discovery 覆盖参数混用。

每个实际运行路径都会先创建系统临时 runtime，显式把 SQLite 指向该目录，设置 `AI_STUDIO_SKIP_LOCAL_ENV=1`，清除 Provider 密钥与代理变量，并把 Futu 固定到 `127.0.0.1:1`。随后安装进程级网络硬门：只允许测试自己创建的回环连接，`127.0.0.1:1` 在进程内模拟为离线，不会到达操作系统；正式 `8770/11111`、非回环连接和外部 DNS 均在发包前失败关闭。退出时会打印 versioned `network_isolation` 审计，任何被阻止的尝试都会使 runner 返回失败，即使某个测试捕获了异常。

runner 还会把专用 `sitecustomize` 目录和项目根写入隔离 `PYTHONPATH`。测试启动的 Python 子进程及其后代会在执行 `-c`、`-m` 或脚本之前安装同一 socket 门；子进程一旦尝试正式端口或外网，会先向该次系统临时 runtime 的追加审计文件写入 PID/目的地，再以专用退出码 `86` 立即终止，不能通过捕获异常把外联尝试伪装成绿测。主 runner 会把标记汇总为 `child_blocked_attempt_count`，非零即整体失败。连接后 `send`/`sendall` 也会重新检查实际 peer，避免先建 socket、后替换或恢复 `connect` 函数绕过目的地门。该启动钩子只有 `AI_STUDIO_TEST_NETWORK_GUARD=1` 时才激活，普通产品进程不会加载。

列出层不会启动测试、服务或任何外部连接。分层通过不等于完整层或完整发现已经通过；验收记录必须写明实际运行的层或 dotted tests，并保留 `network_isolation` 的 `blocked_attempt_count=0` 证据。

runner 默认在结束时列出最慢的 20 项测试；`--durations N` 可调整数量，`--durations 0` 会列出全部测试耗时。长回归建议把 stdout/stderr 重定向到系统临时日志并使用 `--verbosity 2`，这样可观察当前测试名，而不是把无输出的长等待误判为死锁。
