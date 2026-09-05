# 监控正确性与官方来源最小闭环（2026-09-05）

## 本轮基线与范围

实际开发目录是 `ai_collaboration_studio_official_source_monitoring/ai_collaboration_studio`，
不是其同级非 Git 正式源码副本。开始时分支为
`feature/monitoring-runtime-convergence-v2`，HEAD 为
`67fdb4ad548059506302298ee4d87846abfcece9`；已有 52 个修改文件和 8 个未跟踪文件。
这些既有 v2 改动保留，本轮不倒退、不重做 Broker/双管线，也不新增来源。
2026-09-05 只读查询确认该 HEAD 的 Actions `33818981763`、`33818984367`
均为 success；它们不覆盖当前未提交差异。

本轮授权范围：T1 时间、T2 SEC 基线、T3 调度一致性，现有控制面的有效状态，
假来源与临时 SQLite 的事件到研究草稿组合验收及兼容/回滚文档。
正式 SQLite、正式目录更新、原工作区提交/推送与发布分别需要授权。文末所述 SEC/Micron
临时库两轮公开数据试运行已获授权。
不访问正式 SQLite，不使用 8770/11111，不调用 Provider，不创建正式研究轮次。

## 冻结行为合同

| 概念 | 本轮语义 |
| --- | --- |
| Futu 请求时间 | 请求开始、可信本机响应接收、来源行情更新时间分开；耗时/取消沿用单调时钟 |
| 未来输入 | 快照或行情更新时间超过可信接收时间仍拒绝；不放宽窗口、不用来源时间当本机时钟 |
| SEC 首次基线 | 成功首轮取得的、固定股票与表单范围内的完整有界 recent ID 集合；不是 SEC 全档案历史 |
| 单轮上限 | 限制真正新增事件投递数量，不限制首次基线 ID 覆盖范围 |
| 基线边界 | 首次成功完整快照中已有 ID 只建立基线；之后首次观察到的新 ID 按正常规则投递 |
| 迟到与同时间记录 | 使用 ID 判新，不用发布日期截断；较早日期和同一时间的新 ID 仍可投递一次 |
| 修订 | SEC 新 accession 独立判新；IR/宏观沿用现有修订身份与观察时间，不推断未提供的修订 |
| 重放 | 同一事件保持同一主记录；已有导入、已阅、房间材料与草稿均保留 |
| 旧 checkpoint | 不能证明完整基线时明确要求升级/重新建立基线，禁止静默认为完整或清空收件箱 |
| 调度 | 一次选择绑定 adapter/config/state；正常状态竞争安全跳过，真正身份损坏仍失败关闭 |
| 故障 | 来源失败或导入事务失败不错误推进 checkpoint；保留可见错误和退避 |
| 界面 | 应用可用、线程存活、近期来源成功、dry-run、基线状态分别表达；轮询间隔不是实时保证 |
| 研究入口 | 新事件→导入/去重→用户已阅→手选房间→材料→研究草稿；不隐式启动 Provider 或正式轮次 |

## 验收记录

以下均为本轮当前工作区运行结果，不能替代真实来源或正式部署验收。

| 项目 | 结果与范围 |
| --- | --- |
| 修复前 T1 | 真实 FutuUsMarketAdapter + 假 SDK 推进 1,500 ms，1 项失败，误报未来快照 |
| 修复前 T2 | 首轮 8 项中 6 项失败；随后补充“暂时遗漏 ID”和容量案例，2 项均失败 |
| 修复前 T3 | 原 v2 已通过 A/B 到期变化案例；配置/状态竞争新增 4 项失败断言 |
| 后端组合回归 | 40 个相关模块，572 项：571 通过、1 跳过，179.876 秒；104 次测试随机回环连接、外部/受保护端口阻断和子进程阻断均为 0 |
| 跳过项 | `FutuLivePreflightCliTests.test_exact_locked_sdk_import_uses_only_disposable_profile`；当前解释器 SDK 版本不等于精确锁定版本，已单独核实 skip reason，不是在线 Futu 通过 |
| T1 补充开盘边界 | 组合回归后新增完整 SDK→静态授权→Supervisor→临时 SQLite 用例；现有 Futu adapter 模块 17/17，通过请求跨过 09:30 ET 后导入 1 条异动，账本不变 |
| 交付工具回归 | `test_formal_source_promotion_plan` + `test_release_drill`，21/21，59.283 秒，网络全部 0；是工具/合成升级回滚演练，不是正式目录提升 |
| 前端 | 安全 runner 完整 114 个文件通过；最末 SEC 预览说明文字修改后 DOM 20/20 再通过；最终 Vite check 构建 1,759 模块 |
| 代码格式 | `git diff --check` 通过；没有 stage、commit、push 或默认分支修改 |

组合回归的可复跑入口（在本项目子目录中运行）：

```powershell
$modules = @(rg --files tests -g 'test_source_monitoring*.py' -g 'test_source_inbox*.py' |
  ForEach-Object { 'tests.' + [IO.Path]::GetFileNameWithoutExtension($_) })
$modules += @('tests.test_sec_edgar', 'tests.test_market_data',
  'tests.test_monitoring_official_delivery', 'tests.test_source_poll_control',
  'tests.test_backend_test_layers')
$modules = @($modules | Sort-Object -Unique)
python -B scripts/run_backend_tests_isolated.py @modules --verbosity 1 --durations 8
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run check
```

### 验收要求对应证据

| 要求 | 当前证据 |
| --- | --- |
| 正常行情耗时、真正未来输入、回退时钟 | `test_source_monitoring_futu_anomaly_adapter` 真实类/假 SDK 组合，拒绝不改变 checkpoint |
| 多批历史、基线期间新增、同时间/迟到 ID | `test_source_monitoring_sec_baseline` 13 条历史与每批 3 条；首快照后新增仅后续投递一次 |
| 临时消失再出现、空范围、畸形/不完整、容量 | 同一模块共 12 项，覆盖不淘汰历史 ID、空基线、截断数组、并集超 1,000 时不推进 |
| 调度时间变化、选择后停用、配置竞争 | `test_source_monitoring_runtime` + `test_source_monitoring_runtime_coordinator`；普通冲突安全跳过，错误身份仍 fatal |
| 来源失败、退避与无错误游标 | 官方 adapters、supervisor、runtime 模块的相应失败/恢复断言 |
| 事件→通知→已阅→房间→研究草稿→重启 | `test_monitoring_official_delivery` 使用真实 SEC/IR 解析器、Runtime/Scheduler/Supervisor、临时库、本地 HTTP；来源 transport 为假数据 |
| 去重证据、零模型调用 | 上述组合检查真实 Provider generate/probe spies、provider_execution_runs/provider_call_attempts/rounds 差值、重复 run 计数，而不是只检查响应 safety 字段 |
| 事务失败 | 上述组合在 item INSERT 后触发 SQLite ABORT，核验 import/item/state-event 全回滚、checkpoint 不变，恢复后只投递一次 |
| 无权限与非法输入 | 组合的三个写动作均先做缺 token 拒绝断言；原有 source_inbox HTTP/contracts 模块覆盖字段/时间/引用拒绝及无部分写入 |
| 默认关闭、有效配置、空数据与降级 | runtime 默认关闭测试；前端 sourceInbox 单元/DOM 和隔离浏览器证据 |
| 旧状态、房间材料兼容与回退 | SEC 显式迁移临时库测试逐行保留 inbox/rooms/materials；组合重启后逐字核对材料；发布工具合成回滚演练 |
| 真实网络、新公告延迟、正式运行 | 已执行文末所述有限官方来源观察；真实新增延迟与正式运行仍未验证 |

### 浏览器证据

Browser 插件/skill 未提供，使用现有隔离 harness + 已安装 Playwright/Chrome。
最终验证地址 `http://127.0.0.1:52446/`，页面标题 `AI 共创室`；该临时服务已结束。
检查路径：打开来源收件箱→展开健康→查看当前生效配置→选中官方 fixture→记录已阅→手选房间→附加材料→生成研究草稿。
桌面 1536×960、移动 390×844；页面非空、无框架错误覆盖、无移动横向溢出，console/page error
和意外网络请求均为 0。页面通知权限未启用，因此不声称 OS/浏览器通知已验收。
服务结束时 Provider 两张账本和正式 rounds 始终为 0，草稿从 0 变为 1（预期动作）。
旧 harness 的 `forbidden_counts_unchanged=false` 包含这项被允许的草稿增量，不能误读成发生 Provider 调用。

日志、浏览器脚本与截图保存于本机独立证据目录：
`C:/Users/Administrator/.codex/visualizations/2026/09/05/01a07020-ee63-7463-81e0-467786de2ab4/monitoring-correctness/`。
截图：`source-inbox-desktop-before.png`、`source-inbox-mobile-before.png`、`source-inbox-desktop-drafted.png`。

## 初始在线方案及授权边界

建议首个在线范围为 SEC 的 `US.NVDA`（仅 8-K）和 Micron `US.MU` 官方 IR。
只使用新的临时 SQLite，先建立 seed_only 基线，再间隔 5 分钟读取第二轮；使用现有
固定来源客户端、白名单、超时和退避，不扩展其他来源。每轮最多一次 ticker 表、一次对应
submissions 和一次固定 IR RSS 逻辑读取，重定向仍受现有官方 host 白名单限制。
SEC 请求需要有效的本机联系型 User-Agent；不得把联系方式/环境值写入报告或提交。
未配置时先停止 SEC 网络步骤，不编造联系方式。

该试运行只证明所选范围的连接/基线/重放结果；若窗口内无真实新公告，不能声称已验证
真实新增发现延迟。真实公告到来后仍需记录来源身份、观察时间与草稿结果。Futu 在线、
其他官方源、真实通知、两小时 Canary、24 小时 Soak、正式迁移、候选正式推送/对应 CI、
正式目录更新与发布均尚未完成。本轮没有新建 GitHub Issue/PR。

粘贴正文中的 sandbox 任务书链接未作为可访问本地文件提供；本轮依据该正文的完整阶段与
验收要求实施，未假称读过链接背后的独立文件。

## 2026-09-05 授权后的实际进展

用户明确授权上述两轮官方来源读取，并要求任何后续 API 费用需求提前告知。该授权保留，
无需重复确认这两轮范围；它不授权创建付费账户、调用模型 API、迁移正式库或发布。

上线准备暴露 IR 同类基线问题：30 条历史记录中每轮只记住 8/20 条，剩余历史会在下一轮
导入；首轮 8 项回归全部失败。已补完整 RSS 基线、保留缺失身份、修订与迟到事件处理、
空范围/不完整范围区分及 v1 显式升级；累计上限 250 个身份，超限保留 checkpoint 并阻断。
最终相关 10 模块 142/142 通过（81.343 秒），16 次测试回环连接，无外部/正式端口或子进程
阻断尝试。另有测试清单 12/12、IR 提示单元 13 项和 DOM 20 项通过；没有重跑所有后端模块。

现场观察来自现有真实 OfficialIrReleaseAdapter→CompanyIrSourceAdapter→Scheduler/Runtime→
Supervisor→临时 SQLite。两轮使用不同 Runtime 实例且保存同一个临时库；没有正式服务端口。

| 项目 | 当前证据 |
| --- | --- |
| Micron 原 RSS | 两轮 HTTP 404；两轮均 DEGRADED，IR_FEED_ERROR + COMPANY_IR_BASELINE_SCOPE_INCOMPLETE |
| 实际间隔 | 首轮完成到第二轮开始为 300.036 秒 |
| 状态与数据 | 两轮 checkpoint 哈希保持空对象哈希；基线未完成；新收件箱项、草稿与正式轮次均 0 |
| 模型调用 | Provider generate/probe stub 尝试 0，两张账本和正式 rounds 前后均为 0 |
| 来源代码 | 观察期间记录的 10 个源文件 SHA 均未变化 |
| SEC（初次准备时） | 当时尚无有效联系型 SEC_USER_AGENT，发送数据请求数为 0；后续授权后的实际结果见下文 |
| 正式资产 | 未读取/迁移正式 SQLite，未操作 8770/11111，未提交或推送 |

正式观察文件：独立证据目录下 `online-ir-95t__so7/observation.json`。先前准备运行
`online-ir-b1y0wdnj` 因 poll timeout 大于 join timeout 被构造合同拒绝，0 次网络请求；已按
现有合同修正临时执行脚本，不能把准备失败算作一次在线来源请求。
现场程序均已结束；临时 SQLite 路径由 observation.json 记录，仅供这次观察取证。

已进一步核对官方 [新闻页](https://investors.micron.com/news/default.aspx)和该页引用的
[Q4 脚本](https://investors.micron.com/js/module/widgets/dist/latest/evergreen.q4Api.min.js)。它们
明确使用同域公开 GET 新闻 JSON 接口（GetPressReleaseList）；一次 pageSize=2、bodyType=0
的有界只读核验返回 HTTP 200，2,352 bytes、2 条记录。该请求不是付费模型调用。
响应存于本机 `C:/Users/Administrator/AppData/Local/Temp/micron-public-json-observation-798_eywz/response.json`，
SHA-256 `ce1b6b9ad3adf655c36e0124be7a4a5b3ccf93f816484529dcfcbae7d1279bc8`。

新接口的 PressReleaseDate 如 `08/26/2026 16:01:00` 没有时区；它保留为原始声明，
不参与时区推断。随后在对应官方公告 head 中核对到唯一 NewsArticle 的 URL、headline
和明确时区的 datePublished/dateModified；按每条公告独立绑定，不能由这一个样例推导
全站时间偏移。适配只保留该 head 元数据，不采集新闻正文。

Micron 默认入口已接入固定 recent-30 公开 JSON；同一轮最多 31 个 GET、最多 4 个并发，
每条 head 都验证成功才形成完整基线。该完整性仅指此固定查询返回的全部记录，不能推断
全档案或分页历史完整。列表 1 MB、head 128 KiB，使用现有 Runtime deadline/cancel；
失败、取消和缺失时间均不得推进 checkpoint。发布时间对照可信响应接收时钟，运行身份
仍保存轮询开始时间。身份基于 PressReleaseId，RevisionNumber 与规范化元数据参与修订
投影；轮询时间不进入事件指纹。

新 `company_ir_v2` 明确区分 JSON 元数据与旧 RSS，两个哈希分别证明选定的 NewsArticle
时间元数据和合并投影，均不代表公告正文哈希。旧 RSS v1 的影响规则不会套用于 JSON；
JSON 目前输出 neutral/no_match，sidecar 来源语义版本为 v2。配置升级到 v3 并封印格式，
不改 DDL，也不自动迁移旧状态。旧固定 SEC/四 RSS preflight 已显式固定 legacy RSS
范围，不可用它证明新 Micron JSON 默认 Runtime 可用。

格式接入后的增量验证：reader/JSON composition/official adapters 共 52/52，包含实际
producer→Supervisor→独立校验的 neutral sidecar；rules/initialization/legacy preflight/
测试清单 67/67；official delivery/impact/runtime/coordinator/foundation 54/54。
这些均使用隔离 runner，后两组分别为 2/16 次测试回环连接，外部与正式端口阻断尝试为 0。
前端当前定向 14 项单元 + 21 项 DOM 通过，真实后端生成 sidecar 也通过前端 normalizer，
最终 Vite check 构建 1,759 模块。最末 JSON 格式没有再执行完整浏览器流程；前述截图
仍是较早的隔离 fixture 流程证据，不冒称新格式已经完整浏览器验收。

首个 JSON 两轮观察保存在 `online-ir-fzibbq2k/observation.json`。使用默认 20 秒 poll
时限、30 秒 join 和 2 个 head 并发，两轮都 `FAILED / SOURCE_MONITORING_POLL_DEADLINE_EXCEEDED`，
完整基线未建立。两次列表均 HTTP 200，分别取得 18/25 个 head 的 HTTP 200，另有 2/1
个 head 请求超时；每轮总请求分别 21/27，实际间隔 300.027 秒。13 个来源代码文件哈希
在两轮之间未变，checkpoint 均为空、收件箱/草稿/正式轮次/Provider 两张账本均 0，
Provider 方法尝试也为 0。临时进程已经退出。这证明当前 2 并发读法未在本机默认时限内
完成，不能算基线或在线功能通过。

随后修复一个接入时发现的混合来源可用性回归：正常轮询仅剔除 Micron 自身不完整的
子范围，恢复健康 RSS/JSON 项的既有导入路径；任何来源错误或拒绝仍保留整轮 checkpoint，
由 Supervisor 记录 DEGRADED。首次 seed 继续要求全部配置来源完整。3 个新增组合回归
在修复前为 2 失败、1 通过，修复后通过。head 默认/最大并发从 2 调整为 4，最大值与
实际值加入配置封印；请求总量、默认 20 秒截止时间、取消、字节、时间和正文边界未改。
最终 5 个相关模块 77/77 通过（34.838 秒），网络连接与阻断均为 0；其中包括 26 项
transport 测试，覆盖四路阻塞时全部取消/超时关闭且无遗留线程。

最终 4 并发两轮观察：`online-ir-7eev2imd/observation.json`，使用新的临时 SQLite、
两个不同 Runtime 实例和原默认 20 秒时限，运行结束后进程已退出。

| 观察 | 第一轮 | 第二轮 |
| --- | --- | --- |
| 状态 / 总耗时 | SUCCEEDED / 11.004 秒 | SUCCEEDED / 7.709 秒 |
| HTTP | 1 个列表 + 30 个 head，全部 200 | 1 个列表 + 30 个 head，全部 200 |
| 基线身份数 | 30；初始化完成 | 30；识别 30 条重复历史 |
| 新收件箱 / 草稿 / 正式轮次 | 0 / 0 / 0 | 0 / 0 / 0 |
| checkpoint | `aaed91a780cd6a2c287f72a58cbb6e726e436b2d7bfb37f83bccdd2227364dce` | 与第一轮完全一致 |

第一轮完成至第二轮开始为 300.012 秒。被记录的 13 个来源代码文件哈希在观察期间未变，
Provider generate/probe 尝试为 0，两张 Provider 账本前后均为 0。临时库为
`C:/Users/Administrator/AppData/Local/Temp/studio-authorized-official-trial-7eev2imd/observation.sqlite3`。
此结果通过所选 Micron recent-30 的真实连接、首次完整基线及重启重放观察；没有真实新
公告在窗口内出现，因此仍不能证明真实新增发现延迟、通知到研究草稿的在线闭环、SEC
连接、长期运行、正式部署或发布。付费 API 未调用；后续 SEC 结果如下。

用户提供联系型身份后，仅在测试子进程中设置 SEC User-Agent；值不写入源码、应用日志
或回执。`online-sec-1xntck4y/observation.json` 记录两轮真实 NVDA 8-K 观察：
两轮均 SUCCEEDED，分别耗时 2.098 / 1.570 秒，首轮完成至第二轮开始为 300.022 秒。
首轮单轮额度为 3，但完整建立 63 个 accession 的基线；新 Runtime 第二轮识别全部
63 个重复历史，未误导入，checkpoint 均为
`f1bd378ef258ae248ea78667ad0376f138a6e71cdc0fcf45465dfee3ea877c65`。
两轮各读取一次 ticker 表和 NVDA submissions；所有账本、正式轮次、草稿和新增收件箱
项保持 0，Provider 方法尝试 0。观察进程已经退出，联系方式没有持久化到项目配置。
SEC 和 Micron 的有限基线/重放观察均通过，真实新增发现延迟仍未得到观察证据。

最终 JSON 用户流程已在原 `test_monitoring_official_delivery.py` 中补一项组合回归，
复用真实 JSON producer 与既有 Runtime/HTTP/临时库：30 条基线后新 ID 31 只导入一次，
缺 token 拒绝三种写动作，随后已阅、挂接、草稿、重启重放和材料逐字一致均通过。
整个模块 3/3（7.318 秒），32 次测试回环连接、外部/受保护端口/子进程阻断均 0；
Provider spies、两张账本和正式轮次均 0。这是 fixture HTTP 组合证据，不冒称真实新增公告。

## 发布与回滚边界

阶段 5 的隔离候选使用既有 `create_github_source_projection.py` 和版本化源码备份工具。
只读扫描当前开发源码后，向源目录之外的新目录生成完整、经过路径排除的源码投影；
本地候选 commit 仅写该独立目录的 Git index/objects，不修改原工作区的 HEAD、index、
分支或 remote。它包含此前保留的 v2 改动和本轮修复，不假装是一份只含三项修复的 PR。
确切候选 SHA、父提交、投影/归档哈希及只读提升计划保存在独立证据目录的
`candidate-source-review/` 回执中。该候选供审阅使用，生成它不会授权推送、合并、
覆盖正式目录、迁移数据库或启用常驻监控；GitHub CI 仍须由正式推送后的实际运行证明。

真实双版本读取核验使用 HEAD `67fdb4ad` 的完整 Git archive 和当前候选，对同一个
系统临时 SQLite 做只读、query_only 读取。15 项矩阵证明：旧版拒绝 SEC/IR v2
checkpoint 且不重置；房间、普通材料、Q4 已附加材料与旧 Inbox 项仍可读；但新 Q4
neutral sidecar 及包含它的未过滤列表会被旧版报为 `SOURCE_INBOX_RECORD_CORRUPT`。
这是旧 reader 不支持新格式，不是 SQLite 损坏。两版 integrity_check 均为 ok，
表计数及数据库 SHA 均未变，网络为 0。当前候选全部可读，旧 v1 checkpoint 明确要求升级。
因此不能把切回 `67fdb4ad` 视为新 Q4 收件箱的兼容回滚；应停用监控并保留数据，继续
使用认识新格式的候选，或另行审阅兼容适配/备份恢复方案。不得据此自动恢复旧数据库。

- 停用监控仅停止后续轮询，保留 checkpoint、运行记录、收件箱、房间和材料。
- 回退代码前必须检查上一候选是否认识新 checkpoint；不能把版本号改回旧值冒充兼容。
- SEC 旧基线升级必须停用、确认无活跃运行、审阅旧状态和替代基线策略，再通过已有显式配置迁移入口处理；不自动执行。
- 未修改 DDL 的变更也不构成正式数据库访问授权；数据库恢复仍走既有迁移/备份门。
- 现有未提交改动来源混合，不使用 reset/clean 回滚；如用户要求撤回，按本轮具体文件差异逐项审阅。
