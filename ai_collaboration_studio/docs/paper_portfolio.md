# 模拟组合与风险预算

## 定位

模拟组合把投委会讨论中的纸面方向和权重转换为可复算的风险输入。它只在“美国存储产业投资委员会”样板房间启用，标的固定为 `US.MU`、`US.SNDK`、`US.WDC`、`US.STX`。

该模块没有账户、持仓、订单数量、委托价格、交易解锁或执行接口。`execution_capability` 永远是 `none`，`live_trading_allowed` 永远是 `false`。

## 方案合同

每个方案包含：

- 四只白名单标的的 `LONG / SHORT / FLAT` 纸面方向和非负权重；
- 每个非观望标的的研究依据与失效条件；
- 总敞口、净敞口、单一标的、年化波动、单日历史 VaR、最大回撤、最差 5 日和压力损失预算；
- 1 到 8 个可编辑压力情景，每个情景分别声明四只股票的假设价格冲击。

方向与权重只用于计算带符号收益：模拟做多为正权重，模拟做空为负权重。系统不把权重换算成股数、金额或订单。

## 候选到模拟规格的语义合同

新的受治理存储候选不能只靠 `decision_id` 或谱系事件声称“已实现”。服务端从当前 `decision_package_v2` 重新取得用户所选候选的精确快照，生成 `candidate_simulation_seed_v1`；用户在界面核对映射并确定纸面权重后，服务端构建和保存 `candidate_simulation_contract_v1`。合同同时绑定用户决定、产物治理证明、候选 revision、来源消息、最新消息、候选快照哈希、实现规则、评估期限和安全边界，并保存规范化 SHA-256。

当前 `storage_single_name_directional_v1` 适配器是有意收窄的第一版：

- 标的必须是 `US.MU / US.SNDK / US.WDC / US.STX` 之一；
- `UP` 只能映射为 `LONG`，`DOWN` 只能映射为 `SHORT`；
- `NEUTRAL`、`FLAT` 和 `UNSPECIFIED` 不会被猜成某个仓位，必须回到候选层补充可执行的纸面定义；
- 期限只能是 1、5 或 20 个交易日；目标标的的依据和失效条件必须与候选快照完全一致；
- 组合只允许一个活跃标的，其余白名单标的必须为 `FLAT`；用户只确认非负纸面权重，系统不推导股数、金额或订单。

新建、修改、重新复算、用户确认和 walk-forward 都会重新验证当前决定锚点、组合规格与合同哈希。验证发生在读取 Futu 历史之前；映射不一致、候选漂移或合同损坏会返回结构化错误且不读取市场、不写入新版本。旧组合没有该合同时继续可见，但只能标为 `legacy_lineage_only` 或手工模拟，不能进入正式候选语义比较。

## 确定性风险复算

服务端只读取方案中非观望标的的富途前复权日线，并取共同交易日计算：

- 总敞口、净敞口、单一标的最大权重和基于绝对权重的 HHI 集中度；
- 加权日收益的年化波动；
- 历史最大回撤；
- 95% 单日历史 VaR；
- 最差单日与最差连续 5 日；
- 每个用户定义压力情景下的组合损益。

风险结果保存输入指纹、样本数、首末日期和复算时间。少于 20 个共同收益样本、任一活跃标的历史缺失、任一预算超限，都会得到 `BLOCKED`，不会补值或使用模型估算。

## 版本与确认

1. 新建方案进入 `DRAFT`，保存风险结果和 v1 快照。
2. 每次编辑或重新复算都会生成新版本，并回到 `DRAFT`。
3. 只有风险门为 `PASS` 的当前版本可由用户确认。
4. 已确认方案进入下一新轮的共享上下文；正在运行或暂停的旧轮仍使用检查点中冻结的旧上下文。
5. AI 只能把 `DRAFT` 描述为待确认草稿，不能冒充用户决策。

确认仅表示用户接受这份研究记录及其预算设置，不代表系统认为未来收益、胜率或最大损失已经得到保证。

## 与 `decision_package_v2` 的版本关系

- 用户确认候选产物后，仍须针对该精确产物版本作最终 `support` 决定，并显式选择 `selected_option_id`。组合实现的是用户所选候选，不要求等于 AI 的 `ai_preferred_option_id`。只有这条 v2 决定仍是当前记录、选择封印与事件链完整时，才能从决定包创建新的关联模拟组合、观察或回放；`hold`、`return`、旧决定和损坏链均不可新增派生。原 `support` 过期前已合法建立的观察可以在链完整时继续补记确认、真实基线和到期结果，但不能据此创建另一条观察或回放。
- 关联组合新建为 v1 `DRAFT` 时追加 `implements`；任何编辑或风险复算生成新版本时追加 `revises`；风险门通过且用户确认当前精确版本时追加 `confirms`。三类事件都保留资源版本和当时快照，不能用新版本覆盖旧事件。
- 存储产业当前组合收敛门只接受当前决定包中最新事件为 `confirms`、事件版本与当前组合版本相同、快照一致、风险门通过且安全边界完整的组合。组合只是 `DRAFT`、事件版本落后或决定包已失效时都不能满足该门。
- `storage_sample_acceptance_v3` 直接复用上述 v2 用户选择门和组合收敛门，不另写较宽松的组合判断。`meeting_reviewed` 与 `research_sample_ready` 分离；只有当前精确决定为 `support`、决定包完整且规范门返回实现用户所选候选的精确已确认、风险就绪、`execution_capability=none` / `live_trading_allowed=false` 关联组合时才是 `accepted`。`hold` 为 `deferred`，`return` 为 `returned`，二者不保存选择；至少 20 个独立可比模拟样本的统计门仍单独计算。
- 旧版本中既没有 `decision_lineage_resources` 登记、也没有谱系事件的组合、观察和回放继续可见，并归类为 `LEGACY_UNLINKED`。如果资源已有登记但创建事件缺失或绑定不一致，则属于谱系损坏而不是旧资源，相关更新与收敛失败关闭。
- `decision_lineage_heads` 用持久尾序号和尾哈希检测链尾事件丢失；`decision_lineage_resources` 用首次事件登记区分真正未关联与已关联损坏。关系表重建和登记回填由 `SAVEPOINT` 原子迁移，失败时整体回滚。
- 谱系 SHA-256、持久链头和资源登记只用于本地一致性审计；它们没有签名或独立外部锚点，不是外部防篡改公证，也不证明研究事实、策略有效性、因果关系或外部真实性。所有组合和谱系记录仍为 `execution_capability=none`、`live_trading_allowed=false`，最终取舍由用户作出。

## 与模拟观察的区别

- 模拟组合描述同一时点的多标的纸面暴露和历史风险预算。
- 模拟观察检验某一标的在 1、5 或 20 个交易日后的方向阈值。
- 固定阈值历史基准率不是组合策略回测。
- v3 当前计划追溯回放不是策略样本外验证。
- v4 逐折规则回放只用各折训练窗信息生成该折持仓，并从下一交易日开始测试；它降低了折内信息泄漏，但规则家族并非在整段历史出现前预注册，所以仍不是未来样本或未来胜率声明。真正的实时 prospective 验证仍要积累规则合同冻结之后才出现的新数据。
- 对决定包关联组合，精确已确认版本向后分成两条平行轨：冻结输入的历史 walk-forward 记录为 `evaluates`；前向观察记录完整的 `tests(PROPOSED) → confirms(PENDING_BASELINE / OPEN) → 可选 revises(OPEN) → records_outcome(RESOLVED)`。`revises` 仅表示已确认观察后来补齐真实基线，不修改原提案；`records_outcome` 仅保存这一条真实到期样本。两轨都直接引用组合版本，观察不作为历史回放输入，不存在“观察 → walk-forward”的因果关系，单样本也不等于稳定胜率。

## 版本化纸面组合历史滚动回放

新回放只接受当前完整 `support` 决定包中最新 `confirms` 指向的精确已确认组合版本，并追加 `evaluates`；未关联或仅为草稿的旧组合仍可保留查看，但不能新增回放。运行时必须提交精确的 `portfolio_id + portfolio_version`。服务端会向 Futu 提交显式的四个日历年起止范围，再为 `MU / SNDK / WDC / STX` 各保留最新至多 500 条已完成 QFQ 日线；这样不会依赖 OpenD 省略日期时的默认短区间。任一标的缺失、交易日不对齐、来源异常、当天日线未完成或安全字段缺失都会失败关闭。

v3 兼容模式固定为：

- `evaluation_mode=retroactive_fixed_plan_replay`；
- `strategy_provenance=current_plan_retroactive`；
- `out_of_sample_claim=false`；
- 决策截止日后的下一交易日收盘作为模拟入场价；
- 每个测试窗口按固定初始名义买入持有，窗口内不再平衡；
- `walk_forward_config_v2` 只能选择服务端锁定的 `storage_friction_scenarios_v1`，不接受客户端自定义摩擦；
- 基准、压力、极端三档分别冻结纸面参考规模、每边佣金、进出滑点、年化做空借券费和最大日成交额参与比例；
- 容量优先使用正 `turnover`，缺失或非正时才使用正 `close × volume`；开仓与退出都必须满足，缺失或不足即为 `UNFILLABLE`，不部分成交、不缩仓、不改日期；
- 这些值只是假设和历史容量代理，不代表实时券商费率、可借券源、真实成交或账户资金。

对带有效 `candidate_simulation_contract_v1` 的组合，兼容模式进一步锁定为 `fixed_candidate_direction_replay_v1`：目标标的、方向和 `test_days / step_days` 必须与候选合同一致。横截面规则可能在每折更换标的，因此不得用于证明单标的候选已经被实现；请求会在任何历史数据读取前失败关闭。其输出仍只称“历史固定方向回放”或“历史正收益窗口比例”，不称未来胜率。

## 已有候选回放的同口径只读复核

`POST /api/rooms/{room_id}/candidate-comparisons/preview` 接受 2–6 个唯一 `run_id` 和明确的历史用途确认。服务端在一个 SQLite 只读快照内取回所选记录及其完整冻结输入，但不会把完整行情行返回前端。只有以下项目全部一致且每条记录都通过候选合同、谱系、完整性标记和冻结输入重算门时，才返回可见指标：

- 四股 QFQ 历史的精确内容与共同交易日；
- `train_days / test_days / step_days`、1/5/20 日期限和相同纸面权重；
- 固定的 `storage_friction_scenarios_v1` 三档摩擦及不可成交政策；
- `walk_forward_engine_v3`、候选评估规则和精确候选版本。

任一输入不一致、重复候选或完整性失败时，整次响应为 `blocked`，全部候选的场景与收益指标同时隐藏。成功响应只展示基准、压力、极端三档中的历史累计收益、历史正收益窗口比例、最大回撤和容量阻断；不生成排名、赢家或自动决定。接口固定 `provider_calls_total=0`、`market_data_reads=0`、`execution_capability=none`，不读取 Futu、不插入或更新业务记录，也不创建或修改用户决定。

这一层只复核“过去已经分别运行过”的记录，不能证明候选曾在同一次冻结中公平运行，也不能替代下面的决定前多候选实验。

## 决定前原子多候选实验

P23 的 `candidate_experiment_authorization_v1` 直接绑定同一已确认产物版本、治理证明及 2–6 个精确候选，不依赖既有 support 决定，也不创建用户决定或纸面组合。服务端生成唯一共同规格并只冻结一次四股 QFQ 历史；所有 arm 在同一不可变内存数据上计算，随后在单个 SQLite 写事务内复核版本漂移并全量提交。失败时授权、cohort、输入封印和 arm 全部回滚。

cohort 使用房间级 `client_request_id` 与语义哈希幂等。读取会从冻结输入重新执行历史引擎并核对完整哈希链和表字段镜像；任一数据、规格、计划、结果、arm 或聚合异常都会隐藏整组指标，并只返回固定无执行能力的安全投影。界面按授权顺序并列显示三档历史摩擦指标、证据、反证、容量阻断和失效条件，不生成排名、赢家、未来胜率或自动决定。实验完成后，用户最终决定仍由 `artifact_user_decision_v2` 独立记录，允许选择任一仍有效候选。

v4 新增服务端白名单策略合同：

- `walk_forward_engine_v4 / walk_forward_config_v3 / walk_forward_result_v4 / walk_forward_input_snapshot_v3` 绑定 `strategy_rule_contract_v1`；
- 当前唯一规则为 `cross_sectional_total_return_rank_v1`，客户端只能选择白名单 ID，不能提交或改写完整合同；
- 合同从精确已确认组合冻结多头数量、空头数量及两侧纸面预算，源组合持仓不直接复制为逐折测试持仓；
- 每折只以 `train_start..train_end` 的收盘价计算训练窗总收益，按收益排序、标的代码升序打破并列，再生成该折同侧等权持仓；
- `decision_cutoff=train_end`，`test_start` 必须是下一交易日，入场使用 `test_start` 收盘，首个收益观察从再下一交易日开始；
- 每折策略决定及其哈希不包含测试窗或摩擦场景；场景输入与测试行只进入该场景的 fold 输入哈希；
- 合同强制 `test_data_excluded_from_fit=true`、`partial_fills_allowed=false`、`position_shrinking_allowed=false`、`date_shifting_allowed=false`；
- 结果强制 `out_of_sample_claim=false`、`future_performance_claim=false`、`retrospective_dataset=true`。这里的“逐折训练”只说明折内信息边界，不表示规则已在历史发生前注册，也不表示未来实盘胜率。

系统只统计非重叠测试窗口，并要求至少 20 个窗口才允许生成回放结果。这些窗口不被称为统计独立样本；“历史正收益窗口比例”也不是未来胜率。任一正式非重叠窗口不可成交时，整档场景进入 `blocked`，该场景所有 fold 与汇总的组合收益、正负和回撤字段都置空；容量缺口、阻断证据和明确无摩擦的四股等权基准仍保留。审计指标均由后端生成，前端不自行重算。

运行前，后端会用共同历史行数和 `train_days / test_days / step_days` 计算确定性的 `walk_forward_feasibility_v1`。`test_days` 表示完整的收盘到收盘收益观察数，因此一个测试窗需要 `test_days + 1` 条价格行；相邻测试窗只可共用边界收盘，不能共用收益观察，也不会把 1、5、20 日等期限缩短、补值或伪造成已完成。若最多可用的非重叠窗口少于 20，服务端在生成任何 fold 和写入数据库前返回 HTTP 422，响应包含原因码、实际/最低窗口数、最低历史行数和缺口行数。

500 行只读抓取上限下，默认配置为 `train_days=99`、`test_days=20`、`step_days=20`：测试起点从索引 99 到 479，共得到恰好 20 个不重叠收益窗口，最后一个窗口在索引 499 结束。原 `120/20/20` 至少需要 521 行、在 500 行上限下最多只有 18 个窗口，因此不再作为默认值。99 日训练段是保持 20 日测试期限和 20 日步进不变时，对原训练段所作的最小可行调整；这只使最低数据门在理论上可达，不保证 Futu 实际返回 500 条完整对齐历史，更不把 20 个历史窗口解释为未来胜率。

每次成功运行保存不可变记录：精确组合快照、完整冻结历史、配置、展开后的三档摩擦、引擎版本、输入/结果哈希、数据截止日和安全字段。v4 的 `walk_forward_decision_binding_v2` 还分别冻结 `decision_version`、AI 首选、用户所选候选、候选 revision、来源/最新消息、候选快照哈希、风险适用标记和治理证明，以及白名单策略合同、创建前谱系链头和每折拟合决定；写入事务会在最终插入前重新验证同一组合版本与决定锚点，避免市场读取期间发生版本漂移。legacy v1 决定使用旧 binding 只读兼容，不冒充显式用户选择。v3 记录继续按固定方案口径读取，v2 记录显示旧摩擦兼容警告，缺少冻结输入的 v1 记录只作不可完全验证的历史记录。保存前服务端会用冻结输入重新计算并逐字段比对结果；不允许 `api_key`、token、prompt、账户或订单字段进入记录。该流程不调用任何模型，`provider_calls_total=0`、`openai_calls=0`、`execution_capability=none`、`live_trading_allowed=false`、`can_autonomously_decide=false`。

读取 v2-v4 记录时，服务端不只重算规范化 SHA-256，还会从冻结历史、方案与配置重新执行确定性引擎并逐字段比对结果；v3 另校验冻结组合快照、完整摩擦注册表与安全绑定，v4 再校验策略合同、决定锚点、冻结谱系前缀及本次 `evaluates` 事件。每条新运行还原子保存不可原地修改的 `walk_forward_integrity_profile_v1`，冻结创建时的代际、身份版本、是否必须存在决策谱系及是否必须保留候选合同；列表读取在同一 SQLite 只读快照内完成所有标记和事件链校验。带候选语义合同的 v3 回放还要求冻结输入、运行行、精确组合版本快照和 `evaluates` 创建事件中的合同/评估哈希完全一致；改写版本标签、删除候选标记或删除谱系登记都不能把新记录降级成旧普通回放。全部通过才返回 `integrity_ok=true / fully_verified=true`；同步改写结果和自哈希也会因重算不一致而失败，前端隐藏指标。历史记录完整性与当前行动资格分开：来源决定后来正常过期时，完整历史仍可验证，但 `source_decision_current=false / actionable_now=false`，不能据此创建新验证。SHA-256 和本地不可变触发器没有签名或外部锚点，因此用于发现并阻止应用内的本地内容变化，不是外部防篡改公证；拥有数据库管理权限的人仍处于本地信任边界内。缺少冻结输入的 v1 记录标记为 `legacy_unverifiable`，不会伪装成完整验证。

接口：

- `POST /api/rooms/{room_id}/paper-portfolios/{portfolio_id}/walk-forward`：按 `expected_portfolio_version` 运行并保存历史回放；可行性不足时返回 422 和结构化诊断，不保存失败记录。
- `GET /api/rooms/{room_id}/paper-portfolios/{portfolio_id}/walk-forward`：读取版本化回放记录及其审计清单。
