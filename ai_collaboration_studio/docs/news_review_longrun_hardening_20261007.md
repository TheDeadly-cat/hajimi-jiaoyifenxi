# 新闻审核收尾与长时间运行工程改进

本批改动修复观察器在退出和初始化失败时的证据丢失风险，以及页面请求悬挂、来源包重复验证和新闻历史全量扫描造成的持续运行成本。候选从 `300855a5c971f1179b59bba5097741792d9898aa` 开始，Git 分支为 `qwen/closeout-audit-20261006`，PR 基于 `codex/news-review-clock-policy-20260927`，便于单独审核本批变化。

日期：2026 年 10 月 7 日。配套的可公开验证摘要见 [证据索引](evidence/news_review_longrun_hardening_20261007.json)。原始本地报告和日志保留冻结字节，本说明不改写它们。

## 分项改动

| 提交单元 | 行为与关键边界 | 相关入口与测试 |
| --- | --- | --- |
| X-07 静态回执契约工具 | 从绑定源码的写入调用解析实际命名模式与契约来源。严格区分 EXISTS、ABSENT_IN_CHECKED_SCOPE、UNVERIFIABLE；缺失不能推导写入路径未执行。CLI 输出路径、歧义写入点、不完整范围及无法解析的表达式保留无法确认。 | `scripts/closeout_receipt_contract_audit.py`；`tests/test_closeout_receipt_contract_audit.py` |
| X-03 退出与等待层 | 活动和等待阶段使用有保护的主备回执发布。复用已确认的启动 PID/ticks，初始化失败也尝试收尾；失败合并为有界类型码；同名文件不覆盖。报告状态未知保留 `None`。 | `scripts/run_news_review_observer.py`、`scripts/news_review_observer_wait.py`；观察器、等待层和启动身份测试 |
| 页面监控读取 | 来源列表、通知、详情、健康与正文等只读请求使用 15 秒超时，覆盖悬挂 fetch 和 JSON。迟到响应、调用者取消、计时器和监听器清理保持有效。写操作不增加重试。 | `frontend/src/api.js`；`frontend/tests/api.test.js`、`frontend/tests/monitoringReadTimeout.test.js` |
| 来源包验证复用 | 同一请求的同一事务内按 import id 复用不可变来源包验证；单条记录的摘要、镜像和状态仍逐条检查。缓存不跨事务、不跨请求。导入及幂等重放沿用同一边界。 | `backend/source_inbox_service.py`；`tests/test_source_inbox_read_cost.py` |
| 新闻补证批次 | 每轮物理追加扫描分别限制为 64 条新事件、32 条正文版本，并保留 16 条历史轮转额度。合并后最多选择 112 个不同事件；重启补查最近正文版本；保留持久化去重和停止检查。 | `backend/news_review_controller.py`；`tests/test_news_review_enrichment_batches.py` |
| 报告口径 | 传入明确的观察起点和时效标准，记录标准来源、版本与摘要。成功率以完成态轮询为分母，RUNNING 单列；没有完成样本时为 `None`。 | `backend/news_review_report.py`、`backend/news_review_source_analysis.py`；`tests/test_news_review_report_standards.py` |

备用回执仅用于离线补证。既有 watchdog 没有增加备用回执判定，不把备用文件等同于监测闭环完成；独立外部记录器 X-05 未由本批替代。主备落盘都失败时，内存失败码仍可能丢失；外部强制终止不能被概括成一定执行 `finally`。

默认诊断标准是 SEC 15 分钟、Micron 30 分钟，调用者可显式传入另行绑定的标准。默认值不构成监测批准，也不自动授予验收通过权限。正式比较需要核对报告与计划使用的实际标准及摘要。

## 已有离线验证

分组提交前重新核对冻结清单：17 个源文件及 90 个本地证据文件的长度、SHA-256 全部匹配。发布检查随后只清理两个新增测试文件各一行末尾空行，保留 CRLF 和原字节副本；前端相关 35 项、来源读取成本 5 项重新通过。公开证据索引分别记录冻结身份、最终身份及补查日志摘要。产品代码保持冻结版本，既有离线结果保留原范围。各组检查存在重叠，不累加成唯一测试数。

本轮另完成既有离线发布基线 7/7。它不等于完整安全审计；完整 GitHub CI 仍以实际运行结果为准。

| 检查组 | 结果 |
| --- | --- |
| 观察器、退出、等待层、监测探针和 X-07 | 153 项通过，0 跳过 |
| 新闻审核、恢复、边界、正文、运行器和双管线 | 154 项通过，0 跳过 |
| 最终补证批次及启动补查 | 11 项通过，0 跳过 |
| 报告标准与时钟 | 42 项通过，0 跳过 |
| 来源收件箱及行情异动适配器 | 36 项通过，0 跳过 |
| 前端 API 与监控读取超时 | 35 项通过，0 跳过 |
| 前端来源收件箱、通知、新闻审核与正文 DOM | 4 个文件、70 项通过 |
| 本地真实浏览器合成交互 | PASS；超时后恢复、25 次刷新与搜索、桌面及移动视口 |
| 前端构建 | 成功，1,766 个模块转换 |

原生 Windows 进程用例使用新的隔离 fixture；不是旧 B 运行根核验。浏览器使用独立本地合成接口，不连接旧应用。API、SDK 和模型协议测试中的 fixture 调用不等于真实来源或供应商调用。

在 50,000 条合成历史事件上执行 86,400 轮加速调度，耗时约 94.5 秒。576 个新事件和 24 个正文更新均在到达当轮被选择；每轮入队回调最大 19 次。八个采样点均为 1 个线程、127 个句柄，保留内存约 357,657 至 364,984 字节。此结果只说明所测调度场景，不代表真实墙钟 24 小时、完整网络流水线或整个宿主的长期资源稳定性。

来源包包含 50 条合成记录时，同一读取的整包验证从 50 次降至 1 次。历史轮转的取舍是旧积压逐步处理：50,000 个事件按 16 条额度完整轮转需要约 3,125 轮，实际耗时还包括每轮工作。新事件与新正文有独立额度。

## 复现与后续检查

从本应用目录，在全新的可写临时目录中运行，只使用合成数据。设置 `AI_STUDIO_SKIP_LOCAL_ENV=1`、`PYTHONUTF8=1` 和 `PYTHONDONTWRITEBYTECODE=1`，并将 TEMP/TMP 指向新目录。

```text
python -B -W error::ResourceWarning -m unittest tests.test_news_review_enrichment_batches tests.test_news_review_report_standards tests.test_source_inbox_read_cost tests.test_news_review_observer_startup_identity
node --test --test-concurrency=1 --test-isolation=none frontend/tests/api.test.js frontend/tests/monitoringReadTimeout.test.js
python scripts/run_static_security_checks.py --report <new-output-directory>/static-security.json
```

完整检查继续由既有 `Isolated validation` GitHub Actions 执行，覆盖历史读取兼容、前后端回归、干净源码安装/构建/启动、隔离安装升级回滚和依赖清单。GitHub CI 结果与本地离线记录分开识别，以实际提交 SHA 和对应运行记录为准。

## 授权与验收边界

产品审核模型仍绑定豆包；Qwen Code 承担工程工作。没有更换模型路由、扩大来源或预算、改动数据库结构、启动新试验、重建旧监测身份、合并、部署或执行交易。

旧 B 轮退出原因继续为 UNKNOWN。文件缺失不能证明写入路径未执行，新测试通过也不能证明历史故障走了同一路径。真实市场连续采集、模型质量、账单及系统重启/睡眠后的完整监测闭环仍需独立授权与验收。

后续真实运行须使用新的最终提交身份、准备清单、明确批准、预算和来源范围，以及新的运行目录和观察身份。旧候选 SHA 或旧批准回执不能自动授权本候选。
