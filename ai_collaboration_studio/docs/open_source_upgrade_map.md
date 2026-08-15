# 开源方案复用与升级路线

更新时间：2026-08-03。热度数据只是当日 GitHub 页面快照，会随时间变化。

项目热度仅用于发现值得研究的方案；机制迁移状态更新至 2026-08-03，不以 star 数作为采用或复制代码的依据。

## 结论

主产品继续使用我们自己的“通用房间 + 可编辑成员身份 + 群聊时间线”外壳。开源项目用于迁移机制，不直接照搬固定角色、固定拓扑、品牌界面或真实交易路径。

## 方案对照

| 项目 | 当日热度 | 最值得复用 | 不直接照搬 |
|---|---:|---|---|
| [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) | 约 95k stars | 分析 → 多空辩论 → 交易员 → 风控 → 组合经理阶段门；结构化决策；确定性行情校验；检查点；事后反思 | 固定角色图、单标的 CLI、评级直接变交易动作；恢复语义必须独立做故障测试，不能只按示例移植 |
| [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | 约 59k stars | 多种独立投资逻辑并行、组合聚合、风险约束、回测入口 | 大量名人角色、重复提示、把人物风格当证据 |
| [hsliuping/TradingAgents-CN](https://github.com/hsliuping/TradingAgents-CN) | 约 27k stars | 中文产品化、模型选择、报告导出和本地部署体验 | 仓库声明商业使用需要授权；不复制专有前后端代码 |
| [AI4Finance-Foundation/FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) | 约 7k stars | Director、Agent Registration、Agent Adaptor、Task Manager；多角色专业报告 | 过重的全栈金融依赖、会执行外部动作的 Agent Action 层 |
| [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | 约 39k stars | 持久化状态、失败续跑、人类确认点、状态历史与 time travel | 当前阶段不为已有轻量服务引入整套依赖；先迁移状态机思想 |
| [microsoft/autogen](https://github.com/microsoft/autogen) | 热门多智能体框架 | Selector Group Chat、群聊状态保存、人类交接 | 项目已进入维护模式，官方建议新项目使用后继框架，因此不新增依赖 |
| [microsoft/agent-framework](https://github.com/microsoft/agent-framework) | 热门后继框架 | 顺序、并行、handoff、group chat、检查点、人类确认、事件与可观测性、声明式工作流 | 不为当前轻量内核整体换栈；优先迁移执行轨迹、能力协商和声明式房间蓝图 |
| [crewAIInc/crewAI](https://github.com/crewAIInc/crewAI) | 热门多智能体框架 | 可编辑角色、任务合同、Crews 与事件驱动 Flows、结构化输出 | 不把角色提示词当证据，不复制固定业务团队或外部动作层 |

## 已迁入并改进

1. 阶段门不是写死在代码里的角色名，而是每个 AI 身份可编辑的 `workflow_stage`：主持、分析、辩论、方案、风控、决策或自由协作。
2. 存储产业房间按“分析取证 → 多空辩论 → 模拟方案 → 风险复核 → 投委会结论”推进；主持模型只在当前阶段动态选择下一位。
3. 最终新增“投委会决策经理”，但其结论仍只是需要用户确认的研究方案。
4. 富途共同快照和历史日线是确定性事实层；行情缺失时不允许模型补数。
5. 1/5/20 日模拟观察先由用户确认，再冻结真实基准并到期结算；AI 置信度与统计胜率分离。

## 下一批迁移顺序

### P1 结构化观察提案（已完成）

最终决策阶段可输出结构化提案：标的、方向、期限、阈值、依据、反证、证据引用和主观置信度。服务端只把合法字段写入 `PROPOSED`，机器块不进入可见消息，用户确认后才进入真实验证。

### P2 轮次检查点与失败续跑（已完成）

每位成员发言后保存成员集合、已覆盖立场、下一步位置、冻结行情和资料上下文。暂停、断流或应用关闭后从最后成功状态继续，不重复用户消息，也不重新消耗已完成成员的模型调用。

### P3 结果反思记忆（已完成第一层）

观察到期后生成“事实结果 → 原判断 → 错误原因 → 可迁移教训”。反思必须引用观察 ID 和实际价格，下一次只注入少量同标的与跨标的已结算教训。

### P4 智能体绩效与校准（已完成第一层）

评价成员的证据引用率、反证覆盖率、结论稳定性、置信度校准和到期样本表现。不能仅以短期收益给成员排名，也不能用单次命中率自动更换模型。

当前已实现 20 个独立到期样本门槛、Wilson 区间、Brier 分数、最近 20/50 次滚动结果和同行相对命中；AI 记录进一步按“成员身份版本 × 方法版本”分组，角色或模型调整后不继承旧身份成绩，用户观察不进入 AI 表。样本不足时不显示统计胜率，也不根据少量结果自动给角色排名。

### P5 证据面扩展（已完成第一层）

财务、估值、技术指标、资金流、SEC、官方 IR/业绩材料和 FRED 行业代理已经进入共同证据截面。持牌 DRAM/NAND/HDD 专用价格仍待合法数据源与稳定接口。

### P6 房间模型路由与会前预检（已完成第一层）

借鉴多模型框架的 provider registry，但路由继续属于我们自己的通用房间。房间可批量调整成员执行器，同时保留可编辑身份和逐成员覆盖；新轮开始前检查所有启用成员的唯一 provider / model 组合，失败时不产生半轮讨论，也不静默回退到未授权供应商。

### P7 非 OpenAI + Futu 真实只读验收（历史讨论已验证，当前依赖需重新就绪）

本机 Futu OpenD 已返回 `ready` 四股共同快照；隔离数据库中的 DeepSeek/豆包动态轮已完成真实快照冻结、轮次启动、AI 动态调度、检查点和完成事件，OpenAI 调用为零。豆包首次把全部输出预算消耗在 reasoning 而未生成正文，现已按官方支持参数关闭深度思考；随后两角色轮 2/2 实际发言并 `COMPLETED`。成员失败提供安全结构化错误码，同轮与恢复后均不自动重试，也不回退供应商。

默认模板、政策、迁移和测试现已统一到 12 个职责，加入独立数据质量官并强制统一截止时间与防未来数据泄漏。正式轮次 `round_bd81f6121e1b` 已取得 12/12 不同成员成功覆盖，首轮路由为 DeepSeek 9、豆包 3，持久化 13 条主持决策并进入 `COMPLETED`，成员失败为零且 OpenAI 调用为零。绑定产物 `artifact_f323a29847d1` 已由豆包真实生成并保持 `DRAFT`；43 条证据关系全部为 `unreviewed`，所以仍必须由用户逐条核验，不能声称已经确认最佳方案。Futu 始终只读，不连接账户、不解锁交易、不发送订单。

历史强制刷新曾在本机 OpenD 监听 `127.0.0.1:11111` 时取得四股 4/4 `ready`、零缺失、零来源错误的快照 `futu_0a8a41ff51314945`；它只证明当时的只读数据可用性。2026-08-01 发布前复核时该端口未监听，SEC 合规 `SEC_USER_AGENT` 也尚未配置，所以新的真实存储轮必须继续阻断，不能沿用历史在线结论。执行能力始终为 `none`，真实交易仍禁止。

通用房间曾完成非 OpenAI 真实隔离讨论；最新一轮获准探测中四位可编辑角色 4/4 发言成功，DeepSeek/豆包共 11 次、OpenAI 零调用、正式数据库指纹不变。豆包 JSON Object 传输已能生成可解析 JSON，但该次产物没有形成至少两个候选、有效首选和理由，因此只证明真实讨论链路，不证明产物或用户确认闭环。`turn_contract_v1` 的 12 角色隔离 dry-run 另行取得 12/12 合格合同、零隐藏块泄漏和零外部调用；正式真实合同轮仍等待 OpenD 与 SEC 证据前置条件恢复。

### P8 固定纸面组合历史滚动回放（已完成第一层）

借鉴回测框架的滚动窗口、输入冻结、读侧重算和摩擦口径，但不复制真实执行层。每次回放绑定当前 `support` 决定包中的精确已确认组合版本，强制读取 MU、SNDK、WDC、STX 四股共同 Futu QFQ 已完成日线；按决策截止后的下一交易日收盘入场、固定初始名义持有、窗口末退出。v3 固定计算基准/压力/极端三档佣金、滑点、做空借券费与成交额容量代理；不可成交时整档阻断且不展示组合收益。完整组合与行情输入被冻结，读取时从快照重跑确定性结果。至少 20 个非重叠测试窗口才达到最低数据门。

当前计划是事后追溯应用，强制标记 `current_plan_retroactive` 与 `out_of_sample_claim=false`；历史正收益窗口比例不是未来胜率，也不是完整策略 walk-forward。该路径不调用模型或 OpenAI，不连接账户，不产生订单。真正 prospective walk-forward 要等待计划版本之后的新数据，或让结构化规则在每个训练窗内仅使用当时信息生成仓位。

### P9 结构化点名与轮次插话（已完成第一层）

群聊路由不再解析名字子串。用户选择成员后提交成员 ID 与所见身份版本，服务端以关系表保存点名顺序、源消息、请求类型和逐目标状态。空闲时只调用目标成员并按点名顺序串联上下文；活跃轮次把插话绑定原 `round_id`，在当前发言完成后的安全边界优先处理，不创建第二轮。普通消息不触发模型，成员失败不重试、不换 Provider，上游错误正文与伪造 provider/model 不进入 SQLite。崩溃时 `PROCESSING` 目标已使用租约所有者、到期回收和陈旧 worker fencing；启动恢复与重试语义均有离线故障测试。临时库中的真实 DeepSeek 定向回复已通过，OpenAI 调用为零。

### P10 候选对象谱系门（已完成第一层）

借鉴 TradingAgents 的“方案 → 风控 → 最终整合”阶段链，但不依赖角色名字或自然语言相似度。`candidate_lineage_v1` 只把决策发言之前、来自合格非决策成员的 `propose/revise` 建成候选对象；最终决策只能引用已有稳定 ID 和精确字段快照，不能在最后一步自造两个方案、改写 thesis 后再选择，或用不满两个前序方案伪装成比较完成。收敛服务从封印消息重新投影谱系并失败关闭；规范候选快照只注入正式 decision turn，普通成员、非正式点名和历史轮不会获得。

这一层只解决候选来源与修订谱系，不把候选来源闭合误写成风险已经审查；精确版本的对象级风险复核由 P12 独立完成。

### P11 数据就绪状态分层（已完成）

样板验收不再把 Futu 行情和 SEC/IR/业绩材料合并成一个容易误解的“行情数据”状态。新客户端分别显示 `market_snapshot_gate` 与 `research_evidence_gate`；旧 `market_data` 仍作为 v2 兼容汇总保留。因而“Futu 4/4 已就绪、官方证据仍受阻”会被如实同时展示，不会把行情成功误写成研究证据完整。

### P12 候选精确版本风险复核门（已完成）

`candidate_risk_review_v1` 只在房间显式启用 risk 工作流时生效。风险成员的每条结构化复核由服务端绑定精确的 `candidate_id + revision + latest_message_id + snapshot_sha256`；候选修订后，旧版本复核立即过期，最终 decision 只能选择并引用已经复核的同一候选版本，从而封住“审 A 选 B”和“审旧版选新版”。`support`、`challenge`、`reject` 都只是风险复核意见：它们不构成自动否决，也不替代最终 decision，更不替代用户确认。

启用该协议的新正式轮会把 `candidate_risk_review_v1` 冻结进 v8 round/checkpoint marker，并在暂停、恢复、持久化与确认路径中校验精确一致；历史轮的 marker 保持 `NULL`，不会回填或追溯套用新门。该协议仅形成可审计的讨论与决策约束，`execution_capability=none`、`live_trading_allowed=false`，不连接账户、不发送订单，也不产生任何自主资金动作。

### P13 会议产物三层治理证明（已完成第一层）

P13 不再把“形成候选”“风险成员发表意见”和“用户作出最终决定”压缩成一个模糊的已通过状态，而是保留三个独立、可追溯的层次：

1. **候选形成层**：`candidate_lineage_v1` 从封印后的正式发言重投影候选 ID、来源消息、当前 revision 和精确字段快照；决策成员只能比较或引用这些候选，不能在最后一步自造、改写或替换候选。
2. **精确版本风控意见层**：`candidate_risk_review_v1` 把每条 `support / challenge / reject` 绑定到确切候选 revision 和快照哈希。三个动作都只是风险成员的专业意见与处置建议，不是批准、否决、订单授权或执行指令；候选修订后旧意见立即失效。
3. **用户最终决定层**：AI 最终 decision 仍只是研究候选。只有用户先确认会议产物的精确版本，再对该版本追加 `support / hold / return`，才形成用户决定记录；即使用户选择 `support`，也只允许进入可审计的纸面研究和模拟验证，不获得真实交易能力。

#### 服务器重投影与不可变 attestation

新 marker 正式轮的产物确认不能信任前端字段、整理模型摘要或数据库里单独保存的 `ready=true`。确认事务必须从已封印的 round bundle 重新验证 checkpoint、正式 turn、成员身份版本、消息合同和终态 ledger，再按轮次冻结的 evaluator 版本重投影 `candidate_lineage` 与 `candidate_risk_reviews`。产物中的候选集合、revision、首选项和受治理的 decision slice 必须与投影一致；用户若改写这些字段，产物先回到新草稿，不能沿用旧风险复核证明。用户可以明确提出覆盖意见，但系统必须标成未经过该精确版本复核，不能伪装成原候选已通过风控。

每个成功确认的新 artifact version 同一事务追加一份服务端拥有、不可由客户端编辑的 `artifact_governance_attestation_v1`。证明至少冻结：`artifact_id + artifact_version + round_id`、turn contract 与 candidate risk review marker、治理 evaluator 版本、round 治理输入哈希、候选谱系与风险复核投影及其哈希、受治理 artifact slice 哈希，以及固定的无执行能力字段。用户最终决定除绑定 artifact snapshot 哈希外，还必须绑定该版 attestation 哈希。

历史详情以持久化 attestation 解释“确认当时看到了什么”；任何新用户决定、决定包或模拟派生则按 attestation 记录的原 evaluator 版本只读重算，并比较 round 输入、投影、artifact binding 和 attestation 哈希。不能用未来的 v2 规则静默重写 v1 历史；原 evaluator 不受支持时，历史仍可读取，但新的权限性操作失败关闭。attestation 与 artifact version 都只追加，不因读取、迁移或规则升级而原地改写。

#### 历史与通用房间兼容

- 旧轮次的 `candidate_risk_review_version=NULL` 保持原样，不回填 v1，也不因读取而补写 attestation。它可以继续作为历史会议记录读取，但界面和导出必须明确为 `legacy`，不能声称已完成 P12 精确版本风险复核。
- `round_id` 为空的通用群聊产物保持 `governance_not_applicable`，继续使用既有证据核验与用户确认流程；P13 不能把交易研究协议强加给项目研究、体育研究或普通协作房间。
- 新正式轮只要保存了非空治理 marker，就不得因为 marker 损坏、未知或检查点缺字段而降级为旧规则。
- 迁移前已确认但没有 attestation 的版本继续可读、可导出且不得被改写。它不能被表示为 P13 已证明；若后续操作明确要求 P13 治理保证，用户必须显式重新核验并生成新的确认版本，或重新发起启用新协议的正式轮。

#### Fail-closed 条件

以下任一情况都不得确认成 P13 治理版本，也不得创建新的 P13 用户决定或模拟派生：

- round、checkpoint、turn contract 与 candidate risk review marker 缺失、未知或不一致，或者非空 marker 试图降级为 legacy；
- checkpoint、正式消息合同、成员身份快照、终态 turn checkpoint 或 ledger 封印无法复核；
- 少于两个可比较候选、决策没有引用既有候选、候选 revision 漂移、当前版本缺少风险复核，或最终 decision 没有引用当前风险复核消息；
- artifact 中的候选、revision、首选项或受治理 decision slice 与服务器重投影不一致；
- attestation 缺失、JSON 损坏、身份交叉绑定、哈希不匹配、evaluator 不受支持，或 artifact 当前状态与其确认版本不一致；
- 用户决定绑定错误 artifact version、错误 attestation，或在检查完成后遇到并发版本变化；
- 任一治理记录出现账户、订单、资金能力，或 `execution_capability`、`live_trading_allowed`、`can_autonomously_decide` 不满足只读安全边界。

P13 仍只证明“讨论输入、候选谱系、精确版本风险意见、确认产物和用户决定之间的本地一致性”。哈希与业务数据保存在同一 SQLite 时，它不是外部公证，也不证明事实必然正确、未来胜率可靠或候选可以真实执行。

当前第一层实现已经落地：服务端会从封印轮次重投影治理状态，确认事务原子写入不可编辑的 `artifact_governance_attestation_v1`，用户决定再精确绑定该证明哈希；旧轮次与非轮次产物继续明确显示 `legacy_unavailable` 或 `not_round_bound`。前端已分别展示候选形成、精确版本风控意见和用户最终决定，风险意见不会被写成批准、否决权或执行授权。

### P14 规则优先、歧义才调用模型的主持调度（已完成第一层）

隐藏主持不再先调用 Provider、再让服务端覆盖多数无效或无必要的选择。`rules_first_director_v1` 会先处理四类可证明唯一的决定：门禁已满足后的安全结束、当前阶段唯一可执行成员、唯一匹配冻结工作区首要缺口的成员，以及唯一能补齐一项强制职责覆盖的成员。只有两位以上候选仍然同样合理时，才把冻结的候选集合交给用户指定的主持成员及其模型裁决。用户点名、轮次插话、主持身份版本、Provider 路由封印和暂停恢复边界保持不变。

每条确定性决定以 `source=rules_first` 持久化，前端明确显示“规则优先”，主持归因标记为 `service_policy / model_used=false`，不会伪装成 AI 主持实际选择。模型返回合法 JSON 但选择了非候选成员，或在收敛门未通过时要求结束，现会把主持调用和 `director_attempt` 同时记为 `INVALID`，立即打开同轮冻结路由熔断，再走安全确定性回退；不再把不可执行决定记成 `RESPONDED`。

离线 12 角色存储公司样板在继续满足 12/12 不同成员、DeepSeek/豆包双路由、完整发言合同、会议产物和零 OpenAI 网络调用的前提下，建议授权量为 21 次：2 次路由预检、12 次最低正式成员发言、6 次真正歧义的主持额度、1 次会议产物。`round_launch_plan_v3` 把它定义为授权 allowance，而不是用量预测；P17 起，用户确认计划后其中 6 次会进一步冻结为 `round_director` 硬子预算。规则优先使主持最小调用数为 0。若纳入最多 18 次成员尝试，含建议歧义额度的形式路径上界为 27；假定最多 17 个结构性主持机会全部调用模型时，保守结构上界为 38。用户确认的 28 仍是独立全调用硬上限，不会自动抬高，也不代表必须消耗。所有数字来自本地假 Provider 验收，不改写正式账本中已经消耗的 28 次。

### P15 Provider 输出能力协商与纯 JSON 发言封套（已完成第一层）

新正式轮不再要求模型先写可见正文、再在尾部拼接 XML。`turn_envelope_v1` 是只含 `version / turn_contract / visible_content` 的纯 JSON 对象；原 `turn_contract_v1` 的证据、回应、候选、风险、行动和主观置信边界保持不变。解析器拒绝重复键、非有限数字、额外字段、旧 XML 混入和 schema 漂移，可见消息只从 `visible_content` 投影。结构失败记为一次 `INVALID`，不自动修补、不重试、不切换输出模式，也不把失败回复伪装成成功发言。

Provider 通过 `provider_output_capabilities_v1` 声明 `json_schema / json_object / prompt_json`，服务端按固定优先级选择并在调用前锁定一种模式。DeepSeek 与豆包当前声明 `json_object`，GLM 使用 `prompt_json`；未声明能力的历史或测试适配器只获得兼容 `prompt_json`，不会被误认为支持更强约束。`round_launch_plan_v3`、`provider_member_routes_v2`、v9 checkpoint 与 `round_turn_ledger_v2` 共同封印 envelope 版本、schema SHA-256 和逐成员输出模式；历史 v1 路由、v7/v8 检查点及 XML 暂停轮保持原样，不回填、不追溯升级。

这一层只做了离线假 Provider、临时 SQLite、前端校验和构建验证，没有发起新的真实 Provider 请求，也没有改变正式账本已经消耗的 28 次。OpenAI 继续硬禁用；任何真实复验都必须取得用户新的明确调用授权。交易安全边界仍为 `execution_capability=none / live_trading_allowed=false`。

### P16 只读轮次执行轨迹与诚实调用预算（已完成第一层）

`round_execution_trace_v1` 以既有 SQLite 记录为唯一事实源，把 Provider run/attempt、主持尝试与决定、正式 turn、消息落库、候选与风控投影、风险、产物治理和用户决定整理为稳定分页事件。读取在单个数据库读事务内完成，超过容量就失败关闭；事件排序只用于稳定展示，相同时间戳不被解释为因果。输出使用字段白名单，不包含 token、prompt、response summary、可见消息正文或密钥标记；前端按需读取、可中止、可分页，并明确显示“0 次 Provider 调用 / 无执行能力”。

P16 落地时，轨迹会分别校验 Provider 与正式 turn 子账本，并复用服务端候选/风控投影和决定谱系校验；当时还没有把每个 Provider attempt 精确绑定到业务目标，因此不会猜测连接。该版即使局部账本均验证通过，整体仍诚实标记 `partial`；`trace_hash` 也只是当前一致读取快照的 SHA-256，不能描述为持久化历史链头。P17 已在下节补上新正式轮的精确绑定与持久锚，历史 P16 轮仍不回填。

### P17 精确调用绑定、主持硬子预算与持久轨迹锚（已完成第一层）

P17 把 P16 中“可见但未独立强制”的主持 allowance 变成确认后不可扩张的执行政策。服务端从重新构建且哈希一致的 `round_launch_plan_v3` 读取 `recommended_director_calls`，写入带 SHA-256 的 `provider_execution_policy_v1.kind_call_limits.round_director`；美国存储样板当前为 6 次。每次预留先检查用户确认的全局硬上限，再检查该类型是否配置了更窄的子预算。主持子预算耗尽时不发起 Provider 请求，而以 `director_call_budget_exhausted / model_used=false` 走确定性安全回退；失败、无效、取消和遗留尝试与成功尝试一样占用已经预留的全局名额，主持尝试还占用 `round_director` 名额，均不退款。

新正式轮同时冻结 `provider_operation_binding_v1`。每个实际 Provider attempt 在请求上游前获得服务端 UUID4 `operation_id`，并以 `operation_binding_sha256` 覆盖 run、attempt、序号、调用类型、Provider / 模型、成员版本及精确目标。白名单映射为 `preflight_probe → provider_route`、`round_director → director_attempt`、`round_speaker → round_turn`、`round_interjection → chat_request`、`artifact_generation → artifact_generation`；路由使用规范哈希，纪要使用稳定 `generation_key`。轨迹会重新计算绑定并核对目标属于同一轮次及批准路由，目标缺失、类型错配或哈希漂移都判为异常，不再依赖时间戳推测因果。

每条新 `director_decisions` 也写入 `decision_sha256`，封印记录 ID、房间和轮次、序号、动作、成员、理由、来源、阶段、工作区关注点、主持上下文和创建时间。新正式轮标记 `round_execution_audit_v1`；进入 `PAUSED / COMPLETED / PARTIAL / CANCELLED` 时，把 `round_execution_trace_v1` 的非敏感快照、来源水位与前一锚哈希追加到 `round_trace_anchors`，并原子推进 `rounds.trace_anchor_head_*`。同一快照重复封印幂等返回原锚；暂停后恢复产生新记录时，当前快照在下一个安全边界封印前会明确显示已经变化。

兼容与威胁边界没有被夸大：旧轮不回填 operation ID、决定封印或 trace anchor，仍显示 `partial`；活动中的新轮也可能在首个锚出现前暂时 `partial`。所谓 append-only 是应用正常写入路径和链头一致性约束，不是恶意数据库管理员级不可变存储。业务记录、摘要、锚链和链头都在同一个 SQLite；能同时重写它们的管理员仍可伪造自洽历史。本层没有私钥签名、数据库外 HMAC、外部时间戳或第三方公证，只提供本地、局部的防误改与篡改发现能力。

P17 的实现与回归验证只使用假 Provider 和临时 SQLite，没有发起新的 Provider 或网络请求；`MAX_28_PROVIDER_CALLS` 全局硬上限保持不变，交易边界仍为 `execution_capability=none / live_trading_allowed=false`。

### P18 精确证据来源与预算感知动态主持（已完成第一层）

正式轮次产物的来源选择不再由前端把“房间当前全部资料”和“当前已加载消息”临时拼接。`artifact_evidence_sources_v2` 从通过 SHA 校验的轮次检查点确定性投影三类来源：manifest 内的精确资料版本、同轮且不晚于产物创建时间的可见消息、唯一冻结市场快照。资料后来升级到 v2 时旧轮仍返回 v1；缺版、哈希不符或同一精确版本出现重复行时返回显式 `unresolved`，不会偷换成最新版。响应只带精确来源身份、有界脱敏预览和安全 HTTP(S) 定位，不返回整份市场 payload，也不访问实时行情；截断、脱敏或预算耗尽的预览不会被称为完整原文。

前端把该响应视为正式产物的权威候选集合。接口加载失败、未声明权威或遗漏已绑定引用时禁止保存，以免一次普通编辑静默删掉旧证据；非轮次草稿才继续使用房间当前资料与消息。来源面板同时显示支持、反证、冲突和缺口，精确版本不可读的来源只能解除旧绑定，不能新增为“已核对”。默认预览不完整时，用户可按需读取该产物绑定的完整消息或冻结市场快照；只读 detail 接口再次校验轮次、时间边界、成员版本或快照 revision/SHA，以 UTF-8 计最多返回 300 KiB，超限或脱敏后仍失败关闭。聊天消息获得稳定本地锚点，已加载的回复和消息引用可以直接跳转；外链只允许 HTTP(S) 且再次清理用户凭据、片段和敏感查询参数。

`rules_first_director_v2` 将首位之后的候选改为“阶段依赖前沿”：开放全部已满足阶段和最早未满足阶段，最低覆盖一旦完成，剩余早期成员即可与下一阶段一起动态竞争；尚未形成方案时不会先让风险或决策角色对不存在的候选作复核。`director_scheduling_context_v1` 计算每位前沿成员一次成功发言可推进的阶段、职责、独立发言者和首要工作区缺口。唯一最高贡献直接零调用选择；真正并列时才允许隐藏主持模型裁决。上下文还记录完成当前强制缺口至少需要的可见成员调用数、全局余额和主持子预算；全局余额只够这些成员发言时，主持不得占用额度。强制覆盖已满足后不再为“让所有可选成员都发言”继续付费；只能由下一新轮修复的冻结证据缺口由责任成员说明一次后，以 `partial_unrepairable` 零调用结束。模型候选、持久化候选和路由授权共用 256 个成员的单一上限；规则版本、规则 ID、候选集合、缺口代码、`target_stances`、修复范围和预算摘要经过白名单清洗后进入已有决定封印，不持久化共享正文或提示词。

这仍是“封印事实的只读投影 + 确定性调度摘要”，不是另一套权威证据数据库，也不是对来源真实性、未来表现或所谓交易胜率的保证。主持只能决定讨论顺序和是否满足服务端收敛门；最终方案、证据核验和任何模拟组合支持仍由用户确认，真实交易能力保持关闭。P18 的验证只使用临时 SQLite、假 Provider 和本地前端构建，没有新增正式 Provider 调用；`MAX_28_PROVIDER_CALLS` 继续保持 28。

### P21 AI 首选与用户选择分离（已完成第一层）

`artifact_user_decision_v2` 把产物中的 AI 条件化首选与用户最终选择拆开。用户在 `support` 时可选择同版治理候选中的任一有效项，包括 AI 推荐 A、用户支持 B；选择同时封印候选 revision、来源/最新消息、候选快照、风险适用标记、治理证明和整条决定记录。`hold` / `return` 没有候选选择。`decision_package_v2`、`artifact_evidence_graph_v2`、纸面组合、前向观察、`walk_forward_decision_binding_v2`、记分卡和 `storage_sample_acceptance_v3` 全部沿用户所选候选传递，AI 首选仅保留作审计对照；旧 v1 决定只读兼容，不会冒充显式选择。

这一层不扩大权限：全链路仍只用于证据整理、研究、历史回放、前向观察与纸面模拟，固定 `execution_capability=none / live_trading_allowed=false / can_autonomously_decide=false`，不连接账户、不发送订单、不触发资金动作。实现与验证不新增 Provider 调用，`MAX_28_PROVIDER_CALLS` 继续保持 28。

### P22 用户候选到模拟规格的精确映射（已完成第一层）

P21 虽然能证明组合沿用户所选候选 B 的决定链创建，却仍可能只绑定 B 的 ID，实际持仓、方向和评估期限并未实现 B。P22 增加服务端拥有的 `candidate_simulation_contract_v1`：从当前治理证明中的精确候选快照生成种子，由用户确认纸面权重，再冻结候选 ID/revision/消息、快照哈希、标的、方向、依据、失效条件、期限、规则与安全字段。存储适配器当前只接受 `UP → LONG`、`DOWN → SHORT` 的单标的 1/5/20 日规格；`NEUTRAL / FLAT / UNSPECIFIED`、多活跃标的、依据漂移和快照漂移全部失败关闭，不作静默推断。

组合新建、修订、复算、确认和历史回放均在读取市场数据前复核该合同；写入回放前还在事务边界再次验证，避免市场读取期间发生决定或版本漂移。有效候选合同只能使用 `fixed_candidate_direction_replay_v1`，测试窗口和步进锁定到候选期限；会换股的 `cross_sectional_total_return_rank_v1` 不得声称实现单标的候选。旧手工组合保持兼容但不冒充已验证映射。所有输出仍是历史研究证据，不是未来胜率、投资建议或执行授权。

这一层只使用临时数据库、假市场数据和本地前端构建验证；没有新增 Provider 或 Futu 调用，`MAX_28_PROVIDER_CALLS` 保持 28。

### P23 决定前 A/B/C 原子联合实验（已完成）

P23 新增独立 `candidate_experiment_authorization_v1`，从同一房间、同一已确认产物精确版本和同一治理证明中绑定 2–6 个唯一候选。授权只表示用户允许本次历史比较，不等于 `artifact_user_decision_v2 support`，也不复用要求既有 support 的 `candidate_simulation_contract_v1`。服务端统一生成截止日、QFQ、共同交易日历、候选期限、25% 纸面权重、训练/测试/步长、引擎、三档摩擦和不可成交政策；不兼容候选失败关闭，不作静默换算。

每个新 cohort 只执行一次批量历史读取，全部 arm 共享同一冻结内存数据和 dataset seal，Provider/OpenAI 调用为 0。所有 arm 先在内存计算；随后一个 `BEGIN IMMEDIATE` 事务重新验证请求语义、产物、治理证明和候选版本，再原子写入授权、cohort、输入封印和全部 arm。房间级 client request ID、语义哈希与唯一约束提供幂等；同 ID 改语义冲突。读取重算完整输入与结果谱系，任何普通校验异常都降为严格 `integrity_failed`，整组指标与内部封印隐藏，arm 由服务端安全白名单重建。

界面位于候选治理记录和第三层用户最终决定之间，只按授权顺序并列展示历史指标、证据、反证、容量阻断和失效条件，不排名、不宣称赢家、未来胜率或自动决定。实验后用户仍可独立选择任一有效候选，包括历史收益不是最高的候选。全部开发和验收使用临时 SQLite、假市场、假 Provider 与随机非 8770 端口；没有连接 Futu/OpenD、没有修改正式数据库，也没有新增正式 Provider 调用。

### P24 版本化 capability pack / domain adapter / UI contribution 插件合同（第二阶段已完成）

P24 第一阶段已经在既有能力包和领域适配器上增加静态、版本化、不可执行的 registry；不把交易流程并入通用 orchestrator：

1. `capability_pack_manifest_v1`：版本化声明 pack ID、领域、所需核心协议、提供的能力、依赖、最小/最大内核版本、安全不变量和迁移策略；安装或升级只能登记声明，不能改写房间核心规则。
2. `domain_adapter_contract_v1`：把领域对象、证据源、候选语义、模拟器和只读外部连接映射为通用端口。adapter 必须显式声明读写面、数据来源、失败关闭条件、Provider/市场读取预算及执行能力；默认 `execution_capability=none`。
3. `ui_contribution_contract_v1`：以稳定 slot ID 注册房间侧栏、产物工作区、检查器和设置页贡献；每个贡献声明可见条件、所需能力、只读/写动作、API schema 与响应式最低验收，不允许任意脚本注入或绕过核心决定组件。
4. `plugin_registry_snapshot_v1`：每个房间冻结启用 pack、adapter 和 UI contribution 的精确版本与哈希；正式轮次中途更新只影响下一轮，历史记录按冻结版本读取。
5. 兼容与隔离：未知版本失败关闭；卸载不得删除历史封印；插件表使用独立命名空间和迁移账本；TradingAgents 只能作为可选交易领域插件参考，不能覆盖其目录、替换通用主持或获得账户/下单能力。
6. 验收矩阵：合同 schema/迁移、版本漂移、禁用/卸载、多个 pack 冲突、UI slot 冲突、离线 Provider/数据源、1280/760/390 响应式及“最终决定仍由用户完成”均需独立测试。

当前已落地 `capability_pack_manifest_v1`、`domain_adapter_contract_v1`、`ui_contribution_contract_v1`、`plugin_registry_catalog_v1` 与 `plugin_registry_snapshot_v1`。房间持久化精确 registry snapshot；元数据更新保留原封印，能力包变化才重新解析。新正式轮在创建事务内复核设置版本和 registry 哈希并冻结完整投影，检查点、暂停恢复与轮次产物核对同一绑定。损坏能力包 JSON、manifest 漂移、未知 adapter/UI、非宿主 slot、实现版本缺失和安全字段漂移均失败关闭。旧轮只标记 `legacy_unversioned`，不会被回填成“当时已版本化”。

第二阶段已经把 ArtifactDialog、RoomInspector 与候选实验的实际渲染改为解析所属产物或轮次冻结的精确 contribution；当前房间 registry 只决定功能是否仍可编辑，不能改写历史界面归属。完整性失败、实现不可用或当前已禁用时保留只读历史，且代码级写入守卫阻止绕过界面提交；最终决定区仍是不可贡献、不可替换的内核固定区。`structured_project_research` 作为无市场、无 Provider、无领域 adapter 的非交易静态 pack，和存储研究 pack 一起证明通用房间不依赖交易类型。所有 contribution 继续是编译期宿主组件，不加载第三方脚本。

### P25 追加式插件生命周期（阶段 1 已完成）

P25 已实现 capability pack、domain adapter 与 UI contribution 共用的不可变注册记录、追加式事件账本和 CAS current head。catalog 与 activation 两轴分离；disable、quarantine、deprecate、tombstone 均保留历史冻结 snapshot，不运行卸载脚本或删除业务数据。房间、正式轮次和产物冻结 closure-specific lifecycle resolution，并在读取时对照真实历史 ledger head；当前状态另行叠加，不能改写历史。影响预览、双重确认、client request ID、语义哈希、幂等回放、并发冲突和故障回滚均由服务端验证。

生命周期不可用会关闭插件工作区和插件动作，并阻断 domain adapter 直接激活路径；核心聊天、治理记录和用户最终决定仍独立。replacement 仅是同 stable plugin ID 不同精确版本之间的可审计提示，读取会报告其当前可用性，固定不做自动迁移。独立插件迁移账本尚未实现；P26 与 P27 已分别完成 artifact projection 和 round context 两个真实非交易端口。

### P26 声明式 adapter ports 与项目就绪度只读竖切（已完成）

P26 保留所有既有 v1 pack、adapter、UI contribution 与 room/artifact snapshot 的精确历史语义，新引入 `plugin_registry_catalog_v2`、`capability_pack_manifest_v2`、`domain_adapter_contract_v2`、`ui_contribution_contract_v2` 和 `plugin_registry_snapshot_v2`。v2 snapshot 可与未参与端口解析的 v1 adapter 混合；每个被解析端口冻结 exact adapter/port/schema/handler/预算/失败策略，历史读取再由 P25 的不可变 lifecycle target ledger 锚定，禁止用当前最新版替算。

首个 `project_readiness_review` 只读能力包从精确已确认产物版本与证据关系生成结构缺口、证据缺口和阻断项。adapter 只实现 `core.artifact.projection/v1`，没有行情、时间线、preflight 或持久化空方法，也拿不到 Store、Provider、市场或 orchestrator。宿主 UI contribution 显式声明 source port 与 view-model schema，只允许编译期 `project_readiness_review` 组件；输出固定 Provider/market/business writes 为 0，不排名、不宣称赢家、不产生 approval，用户最终决定区保持内核独立。

### P27 第二个 adapter port 与下一轮项目补缺清单（已完成）

P27 新增 `project_round_focus` 能力包和 `core.round.context/v1`。adapter 只把宿主封印的 readiness projection 与房间上下文转换为下一轮补缺清单；没有已确认产物时显式生成零缺口 bootstrap，不会自动吸附后来的产物。用户点击“填入下一轮目标”只得到可编辑文本，仍需亲自打开 v4 启动确认单；系统不自动开始、不点名成员、不改讨论流程，也不触碰最终决定。

启用该包的房间把精确 focus authorization 纳入 `round_launch_plan_v4` 哈希，正式启动在一个 `BEGIN IMMEDIATE` 内重验 source、registry、lifecycle、port 和请求语义，再原子写入 round 与唯一不可变 context。历史读取同时重算 context、round anchor 与 execution trace anchor 链；插件后来停用时冻结记录继续可读但不能变成新授权。未启用该包的旧房间仍使用 v3 与旧 trace watermark 闭集。Provider、market 和 adapter business writes 均为 0。

### 核心会议行动台 v1（已完成）

行动台把已确认精确产物中的待办与后续执行进度分开：候选默认未采纳，用户核对来源、负责人和期限后才可逐项加入。采纳与更新绑定产物 ID、精确版本、待办 ID 和快照哈希，并以客户端请求 ID、语义哈希、乐观 revision、追加事件链和独立交叉锚实现幂等、并发冲突与篡改失败关闭。一个事务同时写入事件、行动链头、独立锚和锚链头，故障不会留下孤立记录或半个状态；只重封事件与行动链头也无法绕过读取校验。

行动台属于通用内核，不是新 adapter port，也不是交易执行或插件迁移框架。它不修改产物版本、不创建或修改 `artifact_user_decision_v2`、不自动启动下一轮讨论，也不向 Slack、Linear、Planner 等外部系统写入。产物出现新版本后，旧版本行动仍按原精确来源保留，新版本待办必须由用户重新明确采纳。完整边界见 [action_desk.md](action_desk.md)。

跨房间行动总览在一个共同 SQLite 读快照内复用逐房间来源、事件、行动链头和独立锚核验，只投影已采纳行动。健康房间可统一搜索和按状态筛选；任一失败房间整组隐藏行动内容与计数，只显示返回该房间复核的提示。总览没有批量更新、自动分配、排名或外部写入接口。

本阶段的全量离线测试、共享 fixture、隔离浏览器和正式边界证据集中记录在 [offline_acceptance_evidence.md](offline_acceptance_evidence.md)。

## 下一阶段顺序

1. 先观察跨房间行动总览的真实使用；旧版到新版行动若需要关联，只能使用用户确认的 `artifact_action_continuation_v1` 独立关系，不得静默迁移精确版本，也不接外部任务系统写入。
2. P28 按[独立插件迁移账本计划](plugin_migration_ledger_plan.md)推进，但只在出现首个真实 plugin-owned mutable schema v1→v2 用例后实施，不用测试空表制造迁移框架。
3. 继续按真实用例扩展第三个 adapter port；禁止非交易 pack 实现市场、Provider 或时间线空方法。
4. 继续把 `artifact_evidence_graph_v2` 扩展为轮次级只读证据图，并为资料版本增加重算完整性与追加式复核事件链；不复制第二套权威事实表，也不由模型猜测语义冲突。
5. 在用户重新授权 Provider 调用前，只运行离线合同、数据库和浏览器测试；之后才恢复并重新核验 Futu OpenD 四股只读行情、SEC 与公司官方资料前置条件。

## 许可边界

可优先复用 Apache-2.0 或 MIT 项目的通用机制，并保留原始版权和许可证要求。对带商业限制、来源不清或包含真实交易执行的项目，只做功能研究，不复制受限代码，也不接入执行路径。
