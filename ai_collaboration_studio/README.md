# AI 共创室

AI 共创室是通用多 AI 协作群聊。用户创建房间，为不同 AI 配置身份、职责、边界、立场、模型和发言顺序；AI 读取同一份讨论记录，互相回应并形成由用户确认的结论。

## 当前能力

- React + Vite 群聊界面，Python 标准库 HTTP 服务。
- SQLite 持久化房间、成员、身份版本、消息和讨论轮次。
- 房间模板、分类层级、成员身份和领域能力包彼此分离；模板只是创建起点。左栏按“交易研究 / 体育研究 / 项目研究”等大类组织小群聊，新建房间可用“/”建立子类，并可独立选择服务端白名单中的领域能力包。`turn_contract_v1` 已成为所有新正式轮不可关闭的群聊内核协议；房间仍可随时增删领域能力包，但不能取消可审计回应链。既有轮次和暂停恢复严格沿用其原先冻结的协议，不做破坏性回填。
- 每个房间持久化 `capability_pack_ids`，服务端据此派生只读领域 `capabilities`。当前可选领域包包括“结构化项目研究”、“美国存储产业只读研究”、`football_research_readonly` 和 `stock_research_readonly`；`structured_turn_contract_v1` ID 只为历史房间与版本快照兼容保留，并在界面中作为系统管理的核心协议只读展示。足球包封印比赛上下文并在没有真实匹配校准时禁止未来胜率；通用股票包要求用户显式股票池和 Futu/SEC/IR/复权/公司行动逐项预检，二者均不提供执行能力。普通四成员模板也能挂载任一领域包，专业入口不依赖固定模板 ID。结构化项目包会把纪要升级为可审计工作区：需求状态与验收标准、风险触发条件与应对方案，以及按价值、成本、周期、依赖、可逆性比较的方案矩阵，全部复用统一证据、版本和用户确认机制。领域包携带成员讨论规则和主持调度重点，增加专业方法而不污染通用内核。
- 未知能力包在房间创建前失败；服务端目录、创建和更新都会强制验证所有包为 `execution_capability=none`、`live_trading_allowed=false`，不能通过新增包打开真实执行能力。完整合同见 `docs/capability_packs.md`。
- 通用讨论内核不直接依赖富途、存储市场服务或交易研究提示语义。需要专业数据、机器块或产物证据扩展时，由经过安全验证的 `DomainAdapterRegistry` 按房间能力激活领域适配器；未知适配器、必需适配器缺失或安全声明不合格都会失败关闭。每个适配器必须固定为 `execution_capability=none`、`live_trading_allowed=false`。提供行情上下文的适配器按其 `activation_capabilities` 与房间能力的交集选择，同一房间最多只能激活一个。
- HTTP 入口在分发前集中拒绝账户、下单、支付、钱包、转账等执行型路径；Provider 的所有 POST 请求统一经过文本生成边界，递归拒绝 `tools / functions / tool_choice`，并禁止向执行型 URL 发送请求。AST 扫描只是额外的回归审计，不被当作唯一安全墙。
- 页头“房间设置”可在创建后修改名称、长期目标、层级分类、动态/顺序调度和领域能力包，并用 `expected_updated_at` 防止多个页面以旧版本覆盖新设置。能力包变更不删除既有消息、资料、观察或模拟组合。
- 成员可随时添加、编辑、暂停、归档、恢复和排序；添加或编辑时可从服务端身份模板目录填入身份、职责、边界、阶段和能力标签，也可完全自定义。模板不会改动 Provider、模型、启用状态或既有历史。活动与归档成员都可打开只读身份版本账本，对比任意两个完整快照的职责、边界、阶段、能力、Provider、模型和生命周期状态；历史读取不提供隐式回滚。归档是保留全部消息、结构化点名、轮次与身份版本的可逆操作，不再由公开接口硬删除。身份、立场、流程阶段和专业能力从该成员下一次发言生效，历史消息与历史 `@` 均按发言或点名当时的身份版本展示。
- `@成员` 已是结构化路由而不是文本装饰：前端提交成员 ID 与用户所见身份版本，SQLite 持久化点名顺序、请求状态和源消息关系。成员重名、跨房间、已停用或身份版本过期都会原子失败，不会按名字猜测或换成其他 AI。
- 房间空闲时只让被点名成员按点名顺序回复；后一位读取前一位的新回复。普通消息和只有文本 `@名字` 但没有结构化点名的请求不会调用模型。成员明确返回的终态失败彼此隔离，不自动重试、不跨 Provider 回退；回复通过 `reply_to_message_id` 审计绑定用户原消息。只有连接中断、租约过期或服务重启留下的非终态任务会进入恢复重试。
- 正在讨论时输入框仍可发送普通插话或结构化点名，但“开始一轮”保持禁用。插话与当前 `round_id` 一起进入持久 FIFO 队列，不中断正在生成的成员；下一安全调度边界优先处理用户点名。为防止持续插话饿死正式讨论，若仍有正式工作，连续两个终态插话后会强制完成一个正式发言，再继续原队列；计数进入完整性检查点，暂停恢复不能将其清零规避。插话回复会消耗该轮已授权的 Provider 调用名额，但不创建正式 `round_turn`、不要求发言合同，也不改变正式 `next_order`、角色覆盖、失败成员或完成状态；即使插话失败，后续正式成员仍按原检查点继续。轮次结束竞态检测到未处理插话时安全暂停而不丢弃。
- “暂停讨论”是服务端权威命令：`POST /api/rooms/{room_id}/rounds/{round_id}/pause` 先持久化暂停请求，已经进入的当前 Provider 调用允许安全结束并提交唯一消息与检查点，但不会再启动下一位成员。前端在服务端确认前保持“正在暂停”并锁定冲突操作，不再用中止 SSE 伪造 `PAUSED`；恢复继续沿用同一冻结轮次且不会重复已终态发言。只要房间仍有暂停轮次，前后端都拒绝开始新轮，避免旧检查点失去正常恢复入口。房间快照另外返回唯一权威 `pending_round / pending_round_checkpoint`；用户既可继续，也可明确“结束本轮”，后者只把状态改为 `CANCELLED`，不会删除消息、检查点或审计记录。
- 每个点名目标使用默认 360 秒的 claim lease 和不透明 `claim_token`；`chat_request_attempts` 逐次记录 `STARTED / RESPONDED / FAILED / CANCELLED / ABANDONED`。目标进入 `PROCESSING` 后，只有当前 token 持有者能在同一 SQLite 事务中提交目标终态和回复，过期工作者的迟到提交会被拒绝；流连接提前关闭时，当前目标释放回 `PENDING`。
- 本地服务先对正式 SQLite 旁的 `.owner.lock` 取得操作系统级独占锁，再初始化应用、绑定端口和执行启动恢复；同一数据库的第二个进程即使改用其他端口也会失败关闭，不能回收首个实例的任务。异常退出会由操作系统释放锁，遗留锁文件本身不代表占用。取得数据库所有权且成功绑定端口后，遗留的 `PROCESSING` 目标才回到 `PENDING`、对应尝试记为 `ABANDONED(server_restart)`；只有具备结构有效、成员明确且政策可严格校验检查点的 `RUNNING` 轮次转为 `PAUSED`，没有安全恢复依据的 `RUNNING / PAUSED` 遗留轮次改为 `CANCELLED`，避免永久死锁。
- 每个房间都有可编辑的 `workflow_policy`：阶段顺序、各阶段最低覆盖、必须听到的立场/能力、不同成员总覆盖、每人发言上限和追问预算均可调整；模板只提供可恢复的默认值。
- 存储产业投委会默认包含 12 个可编辑角色，并采用“主持开场 → 分析取证 → 多空辩论 → 模拟方案 → 风险复核 → 投委会结论”；主持人仍在当前阶段动态选择下一位。默认政策要求 12 位不同成员成功发言、分析阶段覆盖 6 位，并把数据质量官列为独立必达职责。
- 决策阶段成员可在正常群聊结论后输出受限的结构化观察提案；服务端校验后只进入 `PROPOSED` 队列，机器块不显示，仍需用户确认。
- 普通正式发言使用唯一 `round_turns` 账本；AI/系统消息、turn 终态与下一检查点在同一 SQLite 事务提交。若恢复时发现该 turn 已有终态消息，只推进已保存检查点而不再次调用模型；若进程在上游已返回但本地终态尚未提交前退出，外部调用仍可能重试，但持久化终态不会重复。暂停、断流或进程中断后继续沿用原轮次冻结的 Provider 跳过策略、流程政策、能力包、派生能力、富途快照、资料上下文和项目工作区缺口。`round_evidence_manifest_v1` 会校验资料确切版本、正文/快照哈希、截断清单、完整上下文和市场快照；恢复前失败关闭。
- 每个新正式轮都会在创建时冻结 `turn_contract_v1`，其是否生效只以该轮记录和检查点为权威，不受之后房间设置变化影响；旧轮和暂停中的旧协议轮保持空值兼容，不会被事后回填。每位正式成员在隐藏机器块中声明引用的本轮消息、资料或唯一冻结市场快照，以及回应关系、候选方案更新、风险、下一步和主观置信度；市场快照只接受本轮 manifest 的 `snapshot_id`，revision 与完整快照 SHA 由服务端按轮次绑定。服务端按成员阶段、立场和能力校验后，与可见回复及检查点原子保存。无效、越权、跨轮或含执行字段的合同会使该 turn 失败且不计入收敛，隐藏机器块不会进入群聊正文。
- 新正式轮生成会议产物时，候选、风险、下一步和决策板由服务端从正式 `round_turns` 账本中的合格合同确定性投影；模型只能起草摘要、结论等自由文本区，不能覆盖这些合同区。只有精确历史身份中的决策职责可选择或暂缓方案，且“选择 / 暂缓”必须严格二选一。
- 已确认产物中的待办可由用户逐项采纳到独立“行动台”。每项始终绑定产物 ID、精确版本、待办 ID 与快照哈希；负责人、期限、状态和备注之后只通过追加事件与乐观版本推进，不反向改写会议产物、讨论记录或 `artifact_user_decision_v2`。跨房间行动总览只在一个共同数据库快照中重验并汇总健康房间，支持本地搜索、状态筛选和返回精确房间；损坏房间整组隐藏，AI 也不会自动采纳、指派、完成或外发任务。旧版行动与新版确认待办只能通过用户明确确认的 `artifact_action_continuation_v1` 关系连接，关系不继承状态、不采纳新版、不改变最终决定。
- 新 envelope 发言会封印成员版本、可见消息与合同、前序消息快照、终态 v9 检查点和 `round_turn_ledger_v2`；合同、envelope、schema、逐成员输出模式或 ledger 任一标记不一致时都不能降级成 legacy。既有 `round_turn_ledger_v1`、XML 合同轮与普通历史轮保持兼容，升级前未完整封印的轮次只能作为不可信草稿重新运行，不能被迁移静默认证。
- 动态模式由主持模型在房间政策和所选能力包协议边界内选择下一位发言者。房间设置可显式指定任一启用成员为隐藏动态主持；未指定时才使用流程首阶段成员。新轮检查点冻结 `discussion_mode / domain` 以及隐藏主持的成员 ID、身份版本、Provider 和模型；每条新调度记录还保存 `director_moderator_context_v1`，明确展示冻结主持身份及本次决定究竟来自主持模型、用户点名、服务端流程规则还是安全回退。规则和回退决定会标记“未使用主持模型”，不会伪装成 AI 选择；旧记录保持空归因，不事后补造。恢复不会读取后来改过的房间路由，也不会在主持停用时偷偷换人。冻结只约束隐藏调度：主持人作为普通群聊成员发言时，仍与其他成员一样从下一次发言采用用户最新编辑。主持身份、职责、边界和补充规则只能影响调度偏好，不能覆盖证据、安全、无执行权和用户最终决策规则。结构化项目房间会把最新产物压缩为不含正文的缺口快照，按“需求补证、风险反证、方案矩阵整合”等目标职责动态点名；存储研究房间还会把技术指标过期、未来数据、官方来源缺失或明确报错等冻结证据质量问题转成服务端 blocker，并优先回派数据质量官及责任分析师。首轮覆盖后可再次追问。每人上限与整轮追问预算由房间政策决定，模型调度不可用时仍按首要缺口与成员能力确定性回退。
- 普通发言与隐藏主持都读取完整的当前轮持久化消息，而不是只取最近固定条数。上下文超过字符预算时，服务端保留最近原文，并为每条较早消息生成确定性的短索引；不会因长轮次静默抹掉最早的观点、反证或数据质量提醒。同毫秒消息按 `(created_at, id)` 稳定排序。
- 右栏实时展示“下一位是谁、为什么、由 AI/流程规则/安全回退选择”以及正在补齐的项目缺口，不再把动态讨论伪装成固定编号轮询；同一房间禁止并发启动两个轮次。
- 主持人的每次点名和“建议结束”都会先按服务端正常写入路径追加到 `director_decisions`，再推送到群聊界面；记录包含轮次内序号、动作、成员、理由、来源、阶段、受限关注点和冻结主持上下文。新记录同时写入 `decision_sha256`，把这些字段、记录 ID、轮次归属和创建时间一起纳入 canonical SHA-256 校验。刷新或恢复后仍能按原顺序查看；旧记录不补造封印，审计轨迹会据实标记 `partial`。
- 每次真实隐藏主持调用另写入 `director_attempts`：记录冻结主持版本与路由、`STARTED / RESPONDED / FAILED / INVALID / CANCELLED` 状态、规范错误码，以及响应和最终调度摘要的 SHA-256；不保存提示正文、响应正文、异常正文、认证头或密钥。正式轮中的调用还在请求上游前生成 UUID4 `operation_id`，并以 `operation_binding_sha256` 精确绑定本次 Provider attempt 与对应 `director_attempt`。一次 `FAILED / INVALID` 会对同轮同一路由打开熔断器；`round_director` 硬子预算耗尽时则完全不调用 Provider，改走确定性安全回退并标记 `director_call_budget_exhausted`。成功调用只有在主持决定已持久化、普通发言 turn 已预留后才进入 `RESPONDED`；暂停或恢复遗留调用会显式取消，不留下永久 `STARTED`。
- 新轮目标在会前执行编码完整性检查；高比例 `?` 或 Unicode 损坏字符会在任何模型预检和数据库写入前被拒绝，避免乱码目标进入正式讨论。
- 已实现 OpenAI、DeepSeek、豆包/火山方舟和智谱 GLM 成员级适配器；适配器存在不等于当前密钥、模型或上游服务可用，实际可用性以会前预检为准。成员调用失败仍按成员隔离并持久化，`speaker_failed` 会记录安全的 `error_code / provider / model`，不保存上游响应正文或异常详情，也不自动重试或切换供应商。
- “美国存储产业投资委员会”是交易研究中的一个样板房间，不是产品边界。
- 12 个正式角色是：投资委员会主持人、存储周期分析师、硬盘产业分析师、基本面分析师、技术与资金分析师、新闻与情绪分析师、多头研究员、空头研究员、数据质量官、模拟交易员、风险经理、投委会决策经理。数据质量官负责统一时间截面、资料版本和防未来数据泄漏；所有身份、职责、边界和模型仍可由用户编辑。
- 富途只读适配器一次冻结 `US.MU`、`US.SNDK`、`US.WDC`、`US.STX` 的共同证据截面，并标记数据时间、过期和缺失状态。
- 共同证据截面包含富途快照基本面字段、复权日线确定性技术指标、日度资金流摘要、最新只读财报关键指标和主营构成；保留报告期、币种、会计准则与披露维度，不自动把不同公司标签当成同一产品。
- 可选的 SEC EDGAR 适配器通过官方 ticker/CIK 映射与 Submissions API 冻结 10-K、10-Q、8-K、20-F、40-F、6-K 记录；表单出现本身不被解释为利好、利空或交易方向。
- 四家公司固定域名的官方 IR RSS 作为另一层一手自述来源；各源失败互相隔离，外链和未来发布时间被过滤，与 SEC 同日或次日记录只标记关联候选，不静默删重。
- 季度业绩新闻稿会归一化为 `official_earnings_pack_v1`：标记财政期间、DRAM/NAND/HDD 范围、官方演示/讲稿/补充财务资料链接、SEC 日期候选和 Sandisk/WDC 分拆口径断点；当前只做可审计索引，不镜像整份材料，也不把公司自述当作独立验证。真实入口受反自动化页面影响时，使用带核验日期的官方链接目录并显式标记降级。
- FY2026-Q3 已加入第一批带 PDF 页码/表名的结构化指标：MU 的 DRAM/NAND bit shipment 与 ASP、SNDK 的终端收入、WDC/STX 的 HDD exabyte 等；历史事实和公司指引分开显示，指标方向不自动转译为股价方向、胜率或仓位。
- 用户可以从官方事件列表选择一条 SEC 申报或公司 IR 新闻稿，确认冻结为版本化房间资料；服务端重新核对当前官方源与精确 URL，重复操作返回同一资料，AI 之后才可按真实资料 ID 引用。
- 共同快照新增五条 FRED 官方月度行业代理：美国存储设备出货、库存、设备 PPI，以及广义半导体 PPI 和产能利用率；设备与半导体口径分开，库存/出货比由原始序列复算，不生成综合供需分数。
- 存储产业房间的所有成员在同一轮内收到同一个 `snapshot_id`，避免各自读取不同时间截面的价格。
- 正式 12 角色流程要求数据质量官单独核验：行情、历史日线、财报、新闻和资料发布时间均不得晚于本轮冻结截止时间；恢复轮不得读入暂停后出现的数据；无法证明时间有效时必须标记缺失并阻止收敛，不得回填、猜测或使用未来数据。
- 存储研究的最终收敛还执行冻结证据质量硬门：`evidence.state` 必须为 `ready`（或由同房间完整性校验确认的 `ready_with_manual_substitution`），MU、SNDK、WDC、STX 各自都要有不晚于行情且最多相差 7 天的技术指标、非空 SEC 申报索引、非空公司 IR 发布索引和至少一个通过版本、标的、严格财政季度、官方 IR 域名、重算稳定 ID 及只读安全字段校验的 `official_earnings_pack_v1` 季度材料包，并且来源没有未解决的显式错误。材料包字段缺失、对象无效、同一行混入任一无效包、四股覆盖为空或状态仅为 `partial` 都失败关闭；畸形 JSON 也只返回阻断项而不打断服务。缺失、严重过期、未来数据或降级状态会定向回派数据质量官及责任分析师；讨论可以继续修复和解释，但本轮最多落为 `PARTIAL`，主持模型不能绕过服务端宣告完成。
- 收敛数据门严格要求 Futu OpenD `ready` 快照、四行有效价格与市场时间、无缺失/来源错误，以及显式 `execution_capability=none` / `live_trading_allowed=false`。盘中价格仍须满足 20 分钟实时窗；闭市期间仅在富途对本批标的一次性返回明确的 `AFTER_HOURS_END` / `CLOSED` / `WAITING_OPEN`，且快照年龄不超过 96 小时时允许作为“最近闭市截面”研究就绪，并保持 `quote_is_live=false`。Readiness、前端和 convergence 都要求完整的新鲜度合同；服务端复算时间年龄，旧版仅写 `quality=ready` 而缺字段的行也会失败关闭。未来时间、开市状态、状态缺失/查询失败、超龄或缺少安全字段同样不会准入。
- 存储产业新轮在创建轮次、写入消息和探测模型前先通过同一行情数据门；失败返回 `ROUND_MARKET_PREFLIGHT_FAILED` 且没有持久化副作用。通过的快照只抓取一次并冻结进本轮；恢复只校验检查点中的冻结快照，不重新读取当前行情。
- 用户确认模拟观察及其后续基线协调复用同一套严格快照校验；只有来源为 `futu_opend`、四股齐全、无缺失/源错误、时间有效且明确无执行能力的 `ready` 快照才能建立基线。旧版、残缺、未来时间、停牌或非只读快照均失败关闭，不进入胜率样本。
- 同一批复权历史还会计算 1/5/20 日非重叠窗口的固定阈值历史基准率，以及 MU/SNDK/WDC/STX 等权模拟组合的年化波动、最大回撤、历史 VaR、最差 5 日和相关性；它们是确定性风险参照，不是策略回测、未来胜率或真实仓位。
- 存储产业房间可建立多个版本化模拟组合，分别设置四只标的的纸面做多/做空权重、风险预算和可编辑压力情景；服务端使用富途复权历史计算加权波动、回撤、VaR、最差 5 日、集中度和压力损失。任一预算超限或历史缺失都会阻止用户确认，确认结果从下一新轮起进入风控成员的共享上下文。
- 新的受治理存储候选必须先生成 `candidate_simulation_contract_v1`，再建立、修订、确认或历史回放组合。合同精确冻结用户所选候选的 ID、revision、快照哈希、方向、标的、1/5/20 日期限、依据、失效条件和用户确认的纸面权重；当前存储适配器只允许 `UP → LONG`、`DOWN → SHORT`，并要求除目标标的外全部为 `FLAT`。`NEUTRAL`、`FLAT`、未声明方向、旧候选或任何快照漂移都失败关闭，不能被静默解释成仓位。通过该门的候选只能运行同标的同方向、同期限的固定历史回放；会换股的横截面排名规则不得声称实现该候选。旧手工组合继续只读兼容，但明确属于“仅有决定关联”，不计入候选语义验证。
- 富途或 OpenD 不可用时显示真实错误，不生成替代行情。
- 每个房间可添加研究笔记、网页来源和文件摘录；编辑资料会新增版本。
- 新闻、公告、研报与社交观点可记录来源类型、服务端派生的来源层级、发布者、含时区发布时间、事件类型及存储股标的映射；缺失时间保持未知，不以抓取时间代替。
- AI 使用共享资料时必须标注真实资料 ID；不存在、本轮未纳入或轮次之后新增的 ID 会被标记为无效引用。消息保留本轮冻结的资料版本，轮中更新/停用不会错绑最新版，历史标题和来源也按该版本展示。
- 共享资料支持抓取公开网页，以及上传 TXT、MD、CSV、TSV、JSON、HTML、XML、DOCX 和文字型 PDF；只保存提取文本、哈希和来源元数据。
- 网页抓取拒绝本机、私网、保留地址、带账号密码的 URL、非标准端口和危险重定向；每一跳都把已验证的公网 DNS 结果固定为实际 TCP 数字端点，HTTPS 仍以原域名执行 SNI/证书校验，且不使用无法证明最终连接目标的环境代理。外部内容在提示中始终标记为不可信证据。
- 资料重新抓取、替换文件或手工修订都会新增版本；版本轨迹可见，任一历史快照可通过 API 取回。
- 可把讨论整理为会议纪要草稿，分别记录需求、风险、结论、分歧、待验证事项和待办；未启用项目研究包的房间仍保持通用纪要结构。
- 会议产物采用版本化的 `DRAFT → CONFIRMED` 流程；摘要和每条内容都必须绑定真实消息或资料证据后才能由用户确认。绑定轮次的产物还必须证明轮次状态为 `COMPLETED`，且冻结政策要求的成功成员、阶段和职责均由确切成员身份版本覆盖；`PAUSED / PARTIAL / CANCELLED` 或覆盖不完整的产物只能保留草稿。
- 会议产物的每条证据关系还必须标记“支持/反证/背景”和“未核验/已核对原文/已交叉佐证/存在争议”；模型与刚冻结的官方资料默认未核验，反证或争议必须留下说明。
- 摘要、每条结论、分歧、待验证和待办分别选择自己的证据关系；同一来源可以在一条结论中作为支持、在另一条分歧中作为反证。
- 产物编辑器显示全部证据关系的全局核验进度、去重来源记录数和“下一条未核验”；下一条会定位到具体关系。历史资料按被引用的精确版本加载，在精确版本加载成功前不能标记为已核对；不可用来源也不会伪装成可迁移的最新版。同一来源被多处引用时仍须逐关系核验，不能批量把来源核对等同于所有主张均成立。
- “保存进度并继续”会用乐观版本门保存一个新草稿版本并保持编辑器打开，便于分批完成审核。前端在发现未核验关系或其他完整性缺口时禁用确认；确认接口仍会按持久化版本重新计算证据、版本漂移、反证说明和支持证据门。合同轮还会重新解析原始合同 JSON，核对精确成员版本、消息前缀引用、冻结资料清单、正式 turn 终态及检查点成功成员集合；普通同轮 AI 消息不能冒充正式合格发言，任何缺口都失败关闭。
- 资料更新或停用后，既有会议引用不会静默升级：用户必须选择保留并说明历史快照，或迁移最新版并重新核验；确认接口会再次检查即时版本状态。
- 已确认产物可导出 Markdown；继续编辑会生成新的草稿版本，不会覆盖已确认版本历史。
- 产物确认与用户最终决定是两个独立动作：`CONFIRMED` 只表示用户确认该版会议记录与证据标注；之后用户才能针对这个已确认的精确版本选择“支持 / 保留 / 退回”，且必须填写理由。产物内的 `preferred_option_id` 是 AI 条件化首选；`support` 必须由用户显式选择同版决策板中的任一唯一有效候选，允许与 AI 首选不同。`hold` / `return` 不保存任何候选选择。
- 用户最终决定以不可变历史追加保存；同一版本的新决定会让旧决定变为历史记录，产物一旦编辑并生成新版本，旧版本决定会明确标记为过期，不能替新版本背书。任何支持、保留或退回都不授权真实下单、账户操作、投注、支付或其他资金动作。
- `decision_package_v2` 以 `artifact_user_decision_v2` 为锚点，分别冻结 AI 首选、用户所选候选、候选精确 revision/来源/最新消息、候选快照哈希、风险复核适用标记和治理证明。只有“仍是当前决定、决定封印与链完整、动作是 `support`、用户选择绑定有效”的包可以创建新的派生资源；AI 首选可以与用户选择不同。`hold`、`return`、旧 v1 推断、已过期决定或损坏链不能新建关联组合、观察或回放。唯一的续记例外是：原 `support` 过期前已经合法关联的观察，在链仍完整时可以继续记录用户确认、基线补齐和真实到期结果；这只是把既有研究样本收尾，不是恢复派生权限。
- 关联模拟组合的新建、修订和用户确认分别追加 `implements`、`revises`、`confirms`。当前收敛只接受当前决定包内、通过风险门且由 `confirms` 指向精确当前版本的组合。只有既无 `decision_lineage_resources` 登记、也无谱系事件的旧资源才是 `LEGACY_UNLINKED`；已经登记但创建事件缺失或绑定不一致属于谱系损坏，必须失败关闭，不能静默降级成旧资源。
- 精确已确认的关联组合之后分成两条并行验证轨：冻结输入的历史 walk-forward 追加 `evaluates`；前向观察则完整记录为 `tests(PROPOSED) → confirms(PENDING_BASELINE / OPEN) → 可选 revises(OPEN) → records_outcome(RESOLVED)`。其中 `revises` 只在用户已确认但真实基线暂缺、之后成功补齐时出现。前向观察不是历史回放的输入，两者之间不存在“观察 → walk-forward”的因果边，也不能互相冒充验证结果；单条 `records_outcome` 只是一个真实结算样本，不等于稳定胜率。
- `decision_lineage_heads` 持久化每条决定链的尾序号与尾哈希，可发现最后事件被删除；`decision_lineage_resources` 持久化资源、用户决定与首次事件的绑定，可发现关联资源事件缺失或错绑。关系表重建和登记回填使用 `SAVEPOINT`，任一步失败都回滚本次迁移，不留下半迁移结构。
- 决定包的资源快照哈希、前序事件哈希、持久链头和清单哈希只用于本地顺序与内容一致性审计；它们没有签名或独立外部锚点，不是外部防篡改公证，也不证明事实真实性或因果有效性。包和事件始终保持 `execution_capability=none`、`live_trading_allowed=false`、`can_autonomously_decide=false`，最终决定仍属于用户。
- 会议产物包含通用“多方案决策板”：保存多个候选方案及逐项证据，并单独记录首选候选与选择理由。项目研究房间还要求把每个方案放入价值、成本、周期、依赖、可逆性五维矩阵；项目研究与存储产业房间都只有在至少两个方案完成比较、首选与理由均存在且证据由用户核验后，才允许显示“候选最优”。它永远不是自动执行指令。
- 绑定正式轮次的会议纪要使用 `(room_id, round_id, artifact_kind)` 稳定生成键；同一轮重复点击会返回原草稿，不再次调用模型或制造更多重复产物。历史重复草稿不会被删除或改写，只把最早一份服务生成记录绑定为后续幂等目标。
- 存储产业房间使用确定性的 `storage_sample_acceptance_v3`。API 为已有调用方保留原七阶段 `stages` 兼容轨，其中 `market_data` 仍是“Futu 四股行情快照 + 官方研究证据”的合并投影；新客户端应读取独立的 `market_snapshot_gate` 与 `research_evidence_gate`，界面按八阶段显示“Futu 行情快照 → 官方研究证据 → 12 个独立合格角色 → 唯一已确认纪要 → 全证据复核 → 精确当前用户决定 → 当前决定包的确认纸面组合 → 模拟统计”，避免把行情已到误写成研究证据已齐。`meeting_reviewed` 只表示会议、证据与精确决定已完成复核，`research_sample_ready` 才表示研究样板可接受；只有当前决定是 v2 `support`、用户所选候选绑定完整（AI 首选仅作对照）、当前 `decision_package_v2` 完整，且纸面组合精确实现该用户选择、版本已确认、风险门通过并保持 `execution_capability=none` / `live_trading_allowed=false` 时，状态才是 `accepted`。`hold` 与 `return` 的选择必须为空，分别进入 `deferred` / `returned`。v1/v2 验收、旧用户决定、旧检查点或无 `turn_contract_v1` 的历史轮只标为“旧版记录，不计入当前验收”；验收器不调用 Provider、不刷新市场数据，也不补造历史。模拟统计仍单独报告，并且只接受 `observation_scorecard_v3` 中通过持久化决策谱系完整核验、且以 `qfq_close_to_close_v2` 同口径结算的样本；至少 20 个独立可比样本才能显示统计胜率，不反向放宽上述业务门。
- 正式存储投委会纪要必须绑定一个非空 `round_id`，只读取该轮消息及 `round_evidence_manifest_v1` 中冻结的共享上下文和资料确切版本；不得从房间“最新消息”或资料最新版补入轮后信息。缺少或不匹配的轮次检查点会失败关闭；不绑定轮次的通用草稿不会进入该轮收敛判断。
- 需求、风险、结论、分歧、待验证和待办必须使用跨版本稳定的条目 `id`。已确认需求必须有验收标准和已核对的支持证据；每条项目风险必须记录触发条件，标记为已缓解或已接受时还必须写明缓解方案或接受理由。阻断且仍开放/监控的项目风险会阻止候选方案进入“等待用户决策”。
- 分歧必须标记 `open`、`resolved` 或 `accepted_risk`；阻断性的 `open` 分歧即使被确认成准确会议记录，仍会阻止候选方案进入“等待用户决策”，`accepted_risk` 必须写明接受理由或处理说明，且不得改写成共识。
- 空白纪要不能确认：至少需要非空摘要及已核对的支持证据；所有实际存在的需求、风险、结论、分歧、待验证事项和待办也必须逐条绑定并核验证据。模型不可用时生成的框架只能在用户补充并核验后确认。
- 存储产业房间可创建 1/5/20 个交易日的模拟观察；保存时为 `PROPOSED`，用户确认后在真实基准可用时进入 `OPEN`，不可用时进入 `PENDING_BASELINE`。
- 富途离线时观察保持“等待真实基准”，不会伪造价格；之后补到真实基线才转为 `OPEN`，到期后以 `RESOLVED` 保存真实结果。到期结果只使用基准日之后的真实日线收盘，不使用未来数据；单个结果不构成稳定胜率。
- 每条观察保存方法 ID/版本、轮次、产物、成员身份版本和样本键；同一方法版本、标的和期限内，重复或时间窗口重叠的记录不会重复计分。
- AI 主观置信度与用户输入置信度分开标记；只有带成员身份版本、并且通过 `observation_scorecard_v3` 唯一计分口径的 AI 提案进入 Brier 校准。该口径要求观察通过完整持久化决策谱系核验，来源是用户 `support` 决定，绑定精确已确认的纸面组合版本，并用同一 Futu 1d/QFQ 序列的基准日收盘与第 N 个后续交易日收盘结算。旧的“快照 last 对未来 QFQ close”样本只保留审计。少于 20 个独立可比计分样本时只显示“样本不足”，达到门槛后才显示统计胜率、Wilson 区间和 Brier 分数；混合方法总体不解锁“统计胜率”。
- AI 提案还按“成员身份版本 × 观察方法版本”分别记分，并保留当时的身份、Provider 和模型；用户手工观察不进入 AI 身份表，角色或方法变化后不会把新旧样本混成同一胜率。
- 确认观察时同时冻结另外三只白名单股票的同截面基准；结算时计算到期日同行等权收益、目标相对收益和独立相对命中，至少两只同行有效才出结果。
- 记分卡提供最近 20/50 次滚动结果和置信度分组差值；每个统计分组仍独立执行 20 样本门槛。
- 记分卡还会从已校验的决策谱系分别生成 `by_decision_package / by_portfolio_version / by_candidate_option`。无谱系、旧版或损坏谱系观察继续保留在观察历史，并进入 `scoring_population` / `lineage_grouping` 的明确排除计数，但不会进入 `overall`、最近 20/50、置信度校准、任何分组胜率或 20 样本门。每个有效分组重新执行独立样本、同方法和同条件门，混合条件或混合方法不会显示为统计胜率。
- 真实结算会生成绑定观察 ID、结果快照和 SHA-256 指纹的反思草稿；只有用户补充教训、限制和下一次验证条件并确认后，才会作为历史案例进入未来讨论。
- 不执行真实交易、投注、支付或其他资金动作。
- 本地服务在创建监听前拒绝 `0.0.0.0`、局域网地址和其他非回环地址；每个请求还核验真实客户端地址必须是 IPv4 / IPv6 回环，不能只靠可伪造的 `Host: localhost`。写接口继续要求同源 JSON 与进程级随机会话令牌，页面拒绝 iframe 嵌入。令牌只存在于当前本机进程和前端内存，不写入 SQLite。

## 本地运行

```powershell
cd C:\Users\Administrator\Documents\交易分析\ai_collaboration_studio\frontend
npm.cmd ci --cache .npm-cache
npm.cmd run build
cd ..
python -m pip install -r requirements.txt
$env:AI_STUDIO_PORT = "8770"
python server.py
```

打开：`http://127.0.0.1:8770/`

桌面快捷方式为 `AI 共创室 - 交易分析.lnk`，入口脚本使用 `scripts/start_ai_collaboration_studio.ps1`：若 8770 上已经是本项目服务则直接打开；否则在后台启动服务并等待健康检查通过。若端口被其他程序占用，入口会失败关闭，不结束或替换现有进程。入口本身不调用 Provider、Futu 或任何交易接口。

服务会从项目目录及父目录的 `.env.local` 安全读取模型密钥。密钥不会发送到前端，也不会写入 SQLite。当前没有可用模型执行器时，界面明确显示不可用状态，不生成伪造回复。

测试或临时验收必须同时使用 `AI_STUDIO_SKIP_LOCAL_ENV=1`、系统临时 runtime 与显式临时 `AI_STUDIO_DATABASE_PATH`；统一入口为 `python scripts/run_backend_tests_isolated.py`。仅改变端口不构成数据库隔离。

正式服务启动只做 SQLite 只读预检和临时副本迁移模拟；发现任何待迁移差异会在绑定端口前失败，绝不静默建表、加列、seed 或回填。正式迁移必须依次完成清单、可验证备份与候选库、用户对精确 prepared SHA 的显式授权、原子替换和迁移后 integrity/外键/WAL/物理及逐表逻辑哈希核验。命令与恢复边界见 `docs/database_migration_gate.md`。

足球与通用股票研究现以 `football_research_readonly`、`stock_research_readonly` 两个版本化只读能力包接入。检查成功不会自动进入正式轮；用户必须显式授权精确合同，服务端才会在 Provider 调用前重新核验并冻结通用 round context。足球 v1 不生成未经真实校准的未来胜率，股票 v1 要求房间显式股票池并逐项核验 Futu、SEC、IR、复权与公司行动；两者都不投注、不接钱包、不下单、不替代用户决定，也不复用存储股票 candidate experiment。合同和边界见 [`docs/readonly_domain_packs.md`](docs/readonly_domain_packs.md)。

后端测试统一入口支持 `migration / core / domains / full` 四层，定义与命令见 [`docs/backend_test_layers.md`](docs/backend_test_layers.md)。不初始化 Git 的版本化源码快照工具见 [`docs/source_backup.md`](docs/source_backup.md)；它显式排除 `runtime`、本地环境文件和凭据，不替代数据库迁移备份。

### 已有候选回放的只读对比

模拟组合面板可选择 2–6 条已经完整验证的固定候选回放，调用 `POST /api/rooms/{room_id}/candidate-comparisons/preview` 做一次只读复核。只有冻结行情内容、共同交易日、训练/测试/步进、候选期限、纸面权重、三档服务端摩擦和引擎代际完全一致时才显示指标；任一记录损坏或口径不同会隐藏全部收益指标。该接口不读取 Futu、不调用 Provider、不插入或更新业务记录、不排名、不宣称赢家，也不把“历史正收益窗口比例”称为未来胜率。

这只是对已有运行的审计工具，不等于决定前的 A/B/C 联合实验；P23 在产物工作区提供了独立入口，两者不会互相替代。

### 决定前 A/B/C 原子历史实验

`POST /api/rooms/{room_id}/candidate-experiments` 接受同一房间、同一已确认产物精确版本和同一治理证明下的 2–6 个唯一候选。请求只能提交 cohort 级 `client_request_id`、产物/治理预期令牌、候选 ID/revision/来源消息/最新消息/快照哈希及明确的历史用途授权；截止日、QFQ、共同交易日历、1/5/20 日期限、25% 纸面权重、训练/测试/步长、引擎、三档摩擦与不可成交政策均由服务端生成，客户端不能覆盖或静默换算。

本实验使用独立的 `candidate_experiment_authorization_v1`，不要求也不创建 `artifact_user_decision_v2 support`，不复用 `candidate_simulation_contract_v1`。每个新 cohort 只批量读取一次历史行情，全部 arm 共享同一冻结内存数据和 dataset seal，Provider/OpenAI 调用为 0；所有 arm 先完成内存计算，再在一个 `BEGIN IMMEDIATE` 事务中重新校验请求、产物、治理与候选绑定，并原子写入授权、cohort、输入封印和全部 arm。任一计算或提交点失败都不会留下部分记录。同一请求 ID 同语义返回原 cohort，改语义返回冲突。

`GET /api/rooms/{room_id}/candidate-experiments/{cohort_id}` 会重算授权、共同规格、冻结数据、计划、结果、arm、聚合及四表镜像。任一输入或结果异常时整组指标与内部封印同时隐藏，返回严格安全投影。界面只按用户授权顺序并列展示历史指标、证据、反证、容量阻断和失效条件，不排名、不宣称赢家或未来胜率，也不修改第三层用户最终决定；用户仍可独立选择任一有效候选，包括历史收益不是最高的候选。

### 房间讨论流程

在房间右栏或页头齿轮打开“讨论流程设置”。可调整阶段顺序、阶段与职责最低覆盖、不同成员总数、每人发言上限和追加追问额度，也可恢复模板默认。保存使用 `PATCH /api/rooms/{room_id}` 的完整 `workflow_policy` 替换；服务端拒绝未知字段、非法范围和任何试图打开真实执行能力的请求。

右栏会即时检查当前启用成员能否覆盖全部阶段、最低人数和必须职责。若配置本身不可执行，“开始一轮”会禁用；服务端还会在行情读取、模型会前检查、轮次或消息写入之前返回 `ROUND_WORKFLOW_PREFLIGHT_FAILED`。临时指定部分成员仍可形成不完整讨论，后续由收敛门明确显示缺失覆盖，不会冒充完整结论。

新轮启动后政策冻结进检查点。运行中修改房间政策只影响下一新轮，暂停恢复继续使用原政策。成员身份、职责、边界、立场、阶段和专业能力从该成员下一次发言开始生效；正式轮已确认的 Provider / 模型路由除外，它们只影响下一新轮。完整合同与迁移规则见 `docs/workflow_policy.md`。

### 模型执行器

复制 `.env.example` 中需要的变量到本机 `.env.local`，只填写实际使用的供应商；不要把 `.env.local` 提交到版本库。

- OpenAI：`OPENAI_API_KEY`，默认模型 `gpt-5.4-mini`。
- DeepSeek：`DEEPSEEK_API_KEY`，默认模型 `deepseek-v4-pro`。
- 豆包 / 火山方舟：`ARK_API_KEY`，默认模型 `doubao-seed-2-0-lite-260215`。
- 智谱 GLM：`GLM_API_KEY` 或兼容变量 `ZHIPUAI_API_KEY`，默认模型 `glm-5.2`。

每位 AI 成员可独立选择执行器与模型；模型栏留空时使用该执行器的默认模型。供应商状态接口只返回是否已配置、默认模型和是否被部署策略禁用，不返回密钥、密钥片段或认证请求头。代码策略无条件禁用 `openai`；`AI_STUDIO_DISABLED_PROVIDERS` 只能追加其他禁用项，请求也只能继续追加，不能用空环境变量或 `skip_providers=[]` 重新开启 OpenAI。

新正式轮的成员发言使用纯 JSON `turn_envelope_v1`：顶层只允许 `version / turn_contract / visible_content`，其中机器合同仍是原有 `turn_contract_v1`，可见正文只从 `visible_content` 入库。Provider 能力按 `json_schema > json_object > prompt_json` 的固定优先级协商；当前 DeepSeek、豆包声明 `json_object`，GLM 使用严格 `prompt_json`，未声明能力的兼容适配器也只能使用 `prompt_json`。每位成员的选定模式、envelope 版本和 schema SHA-256 在启动计划与调用路由中封印。一次发言最多调用 Provider 一次；JSON、schema 或合同校验失败记为 `INVALID`，不修补、不重试、不切换模式，也不降级回旧 XML。

房间右栏会显示启用成员的 provider / model 分配，并支持把全部启用成员批量迁移到 DeepSeek 或豆包；批量迁移只修改模型路由，不改变成员身份、职责、边界、阶段、顺序或历史身份版本，仍可逐成员覆盖。右栏“本机配置检查”只读取 Registry 状态，固定为 0 次外部调用，也不创建调用账本。正式新轮由服务端 round stream 在用户确认计划与调用上限后，按冻结的唯一 `(provider, model)` 组合执行极小真实连通性检查；会前探测与后续成员调用都必须命中该轮封印的成员路由清单。缺少配置、认证失败、模型无权限或上游不可用时，在创建轮次、写入消息和冻结证据前失败关闭。预检结果只返回安全状态，不返回凭证、上游原文或响应正文。

#### 正式新轮启动确认与 Provider 调用账本

点击“开始一轮”后，前端先请求 `POST /api/rooms/{room_id}/round-launch-plan`。该接口只读取房间快照和本机 Provider 状态，返回冻结成员、主持路由、调用次数投影、阻断项与 `plan_hash`；不探测 Provider、不生成文本、不读取市场数据，也不创建轮次或调用账本。

用户必须在启动对话框中明确确认 `client_round_request_id`、原样 `plan_hash` 和 `max_provider_calls`。公开接口只接受 1–28 次；服务端会重新构建计划并校验哈希，房间、角色、路由或跳过策略漂移时失败关闭。低于推荐值仍可确认，但会显示警告，讨论也可能因次数用尽而提前暂停；超过 28 次直接拒绝。

当前 `round_launch_plan_v3` 明确把授权额度与用量预测分开。美国存储产业投委会的核心成功路径为 14 次：2 次会前路由探测与 12 次最低正式成员发言；计划建议另保留 6 次真正歧义的主持裁决和 1 次可选会议纪要，因此建议用户授权 21 次。用户确认计划后，这 6 次不再只是界面提示，而会冻结为该执行账本的 `round_director` 硬子预算。若把 18 次最大成员发言尝试也纳入，含建议歧义额度的形式路径上界为 27 次；再把最多 17 次结构性主持机会全部假定为模型调用，保守结构上界为 38 次。后两者都不是用量预测，也不包含用户临时插话。运行时采用 `rules_first_director_v2`：首位定界后按“已满足阶段 + 最早未满足阶段”开放因果前沿；某阶段最低覆盖完成后，剩余该阶段成员可与下一阶段一起按阶段、职责、独立发言者和当前证据/项目缺口贡献竞争，但风险与决策不会越过尚未形成的方案。唯一最高贡献、唯一候选与安全收敛均为 0 次主持调用；强制阶段、职责和独立发言者覆盖已经满足时，不会仅为让所有可选成员都发言而继续购买调用。冻结证据缺口若只能在下一新轮重建，则由匹配职责成员说明影响和补证条件一次，随后零调用结束为 `PARTIAL`，不把文字说明伪装成证据已修复。28 仍是用户独立确认的全调用硬上限，不是必须花完的目标，也不会被计划自动抬高。运行时还会为完成强制覆盖仍需的最少可见成员调用预留额度；即使缺口摘要暂时为空，只要服务端仍要求继续讨论，也至少保留下一位可见成员的 1 次调用。全局硬上限优先判定，主持子预算只会进一步收紧调用，不能扩大授权。`provider_call_budget_profile_v1` 会把上述语义和未投影的调用类型显式返回；`provider_member_routes_v2` 继续把 envelope、输出模式、成员身份版本、Provider 与模型一起封印。

用户确认后，存储房间先通过只读行情门；行情失败不会创建调用账本。行情通过后才创建持久化 `provider_execution_runs / provider_call_attempts`。账本把启动计划中的全部 `{member_id, approved_member_version, provider, model}` 规范化并以 SHA-256 封印；`provider_execution_policy_v1` 另封印 `kind_call_limits.round_director` 与 `provider_operation_binding_v1`。会前探测只能使用清单里的 Provider / 模型组合，带成员的真实调用还必须匹配该成员的确认路由。每次真实会前探测、成员发言、主持决策、轮内插话或纪要生成都必须在请求上游前原子预留一个名额；预留时先检查全局上限，再检查该类型是否配置了更窄的子预算。缓存命中、未配置、被跳过或部署禁用的路由为 0 次；`FAILED / INVALID / CANCELLED / ABANDONED` 均已消耗全局名额，主持尝试还会消耗 `round_director` 名额，不退款、不补回。

新正式轮中的每个实际 Provider attempt 都由服务端生成 UUID4 `operation_id`，并将 run、attempt、序号、调用类型、Provider / 模型、成员版本和目标写入 `operation_binding_sha256`。目标映射固定为：`preflight_probe → provider_route`（规范路由哈希）、`round_director → director_attempt`、`round_speaker → round_turn`、`round_interjection → chat_request`、`artifact_generation → artifact_generation`（稳定纪要 `generation_key`）。目标类型、目标归属或绑定哈希不一致时，审计轨迹判为无效；它不会仅凭相近时间戳猜测因果。

暂停恢复和绑定该轮的会议产物继续使用同一个账本、全局上限、`round_director` 子预算、operation 绑定版本、Provider 跳过策略及成员路由封印，不能增加额度或改换模型。恢复前遗留的 `STARTED` 尝试只会在取得该房间正式执行锁且确认轮次仍为 `PAUSED` 后转为 `ABANDONED`，并且不退款；纪要遇到未终结调用会失败关闭，不会擅自回收在途请求。暂停期间可继续编辑身份、职责和边界，下一次发言读取最新字段，但 Provider / 模型仍使用本轮确认路由；路由编辑从下一新轮生效。同一轮产物幂等重放为 0 次调用。账本还冻结唯一纪要成员版本、Provider 和模型；轮后编辑成员或请求指定其他整理者不能改写已批准路由。当前部署固定禁用 OpenAI，启动计划中 OpenAI 必须显示为 0 次，不预检、不发言、不主持、不生成纪要。

本节只约束正式新轮、该轮恢复及绑定该轮的产物。右栏“本机配置检查”是 0 次外呼的只读元数据投影；结构化 `@成员` 点名仍是独立显式路径，不计入正式轮次调用账本。

存储产业房间的前端同时显示“数据接入就绪中心”和“模型执行器”会前状态。数据中心把“证据准备”和“正式讨论准入”拆开：OpenD 离线时仍可显式刷新公司 IR、官方业绩材料和 FRED 公开代理并把真实官方事件冻结为共享资料，但这些独立资料永远不能让 Futu 四股行情门变为通过。面板中的 Futu 准入行与“开始一轮”复用同一个严格四股行情门；即使尚未主动刷新独立公开证据，也不会把已经 4/4 通过的当前快照误显示为“待核验”，反之最新快照失败也会覆盖旧 readiness 的通过状态。新轮只有在当前四股快照完整时才可尝试启动；暂停恢复沿用原轮冻结快照，不受当前 OpenD 在线状态影响。服务端仍是最终强制门，前端状态不能绕过它。覆盖完整但带来源错误的业绩材料包标记为 `partial`，不会误显示为完全就绪。

#### 历史本机配置与验收基线（2026-08-03）

最新的 P23–P27、Action Desk、迁移硬门、足球/股票只读能力包、分层测试、源码备份、浏览器隔离和正式边界证据见 [`docs/offline_acceptance_evidence.md`](docs/offline_acceptance_evidence.md)（含 2026-08-12 continuation addendum）；逐项目标状态见 [`docs/completion_audit_2026-08-12.md`](docs/completion_audit_2026-08-12.md)；下面条目保留为历史配置与验收记录，不替代最新证据。

- DeepSeek 已配置，最小真实请求通过；当前解析模型为 `deepseek-v4-pro`。
- 豆包 / 火山方舟已配置，最小真实请求通过；当前模型为 `doubao-seed-2-0-lite-260215`。
- 智谱 GLM 已配置，但真实请求返回密钥未授权或模型尚未开通；修复前不把成员路由到 GLM。
- OpenAI 适配器仍保留，但当前部署在代码策略中无条件硬禁用 `openai`；环境变量只能继续增加禁用项，不能移除这条固定策略。Registry 执行查询、正式连通性检查、隐藏主持、成员发言、点名恢复和会议产物都会失败关闭。前端仍显式提交 `skip_providers=["openai"]`，服务端再与固定策略取并集并冻结进 round / 点名检查点；显式空数组或空环境变量都不能削弱它，也不会探测、调用、静默换供应商或回退到 OpenAI。
- `AI_STUDIO_DEFAULT_PROVIDER` 控制新成员的后端默认执行器；未设置时默认使用 DeepSeek。
- “已配置”只表示本机存在非空配置，不代表真实 API 可用；任何状态接口和错误事件都不得返回密钥、认证头或密钥片段。
- `turn_envelope_v1`、Provider 输出能力协商、`round_launch_plan_v3`、`provider_call_budget_profile_v1`、`provider_member_routes_v2`、v9 检查点和 `round_turn_ledger_v2` 已完成离线实现与回归验证。新正式轮还会把计划给出的主持建议额度冻结为 `round_director` 硬子预算，并启用 `provider_operation_binding_v1`；历史 XML `turn_contract_v1` 暂停轮继续按原冻结协议恢复，不回填新政策或 operation 绑定。新轮收到旧 XML、纯文本或损坏 JSON 时只消费该次已预留调用并失败关闭。该升级没有发起任何真实 Provider 请求，正式账本仍受 `MAX_28_PROVIDER_CALLS` 硬上限约束。
- `round_execution_trace_v1` 已提供按需读取的本轮执行轨迹，把 Provider 账本、主持尝试与决定、正式 turn、消息落库、候选/风控投影、产物治理和用户决定整理为稳定分页事件。它在单个 SQLite 只读事务内生成当前快照，不调用 Provider，也不拥有执行能力。新正式轮可校验 UUID4 operation 与精确目标绑定、主持决定封印，以及按服务端正常写入路径只追加的 `round_trace_anchor_v1`：轮次进入 `PAUSED / COMPLETED / PARTIAL / CANCELLED` 时持久化非敏感快照、来源水位、前一锚哈希，并原子推进轮次链头；同一快照重复封印幂等返回原锚。运行中或恢复后出现新记录时，在下一次封印前会诚实标记“快照已变化”。旧轮缺少 operation 绑定、决定封印或 `round_execution_audit_v1` 时不回填，继续显示 `partial`，不会凭时间戳猜测因果或把旧快照伪装成完整审计链。
- `discussion_audit_v1` 在执行轨迹之上提供独立的只读讨论审计：列出规则/主持模型/安全回退的选人结构、合格合同中的回应边，以及候选数量、精确版本风控和条件化首选检查点。它不复制聊天正文、主持理由、提示词或模型名，也不改变 trace v1/hash/anchor；`semantic_causality` 固定为 `unknown`，只证明结构引用，不能声称模型确实理解或因前文作答。前端与执行轨迹并行读取并要求 `audit.source.trace_hash` 精确匹配当前轨迹，审计失败或时点漂移不会遮蔽原轨迹。
- 执行轨迹中的既有 `candidate_projection` 现已直接显示候选形成过程：候选 revision、证据数量、失效条件、当前/过期风控复核、AI 条件化首选、阻断和投影指纹均为只读投影；用户决定事件分别显示 `ai_preferred_option_id` 与 `selected_option_id`，兼容字段不再被标成 AI 首选。主观置信度不会被改写成胜率，风控意见也不等于用户授权。
- P18 第一层已把正式产物来源候选改为服务端权威的 `artifact_evidence_sources_v2`，并把 `director_scheduling_context_v1` 纳入新主持决定封印。规则主持会记录统一上限内的候选集合、阶段/职责/独立发言者/首要证据缺口贡献、最少剩余可见调用以及全局和主持子预算快照；当全局余额只够完成强制可见发言时，主持模型调用为 0。只能在下一轮修复的冻结证据缺口带 `repair_scope=next_round_only`，责任成员说明一次后以 `partial_unrepairable` 零调用收尾。该层是对既有封印事实的确定性投影和调度辅助，不等于事实真实性证明，也不允许 AI 自动替用户确认产物、确定最终方案或执行交易。
- `turn_contract_v1` 已在隔离数据库完成 12 角色完整 dry-run：12/12 正式发言合同合格、零隐藏块泄漏，并由明确标注为非真人的 `isolated_fixture_user` 走完逐证据复核、精确版本确认、`support` 决定、安全纸面组合、真实 `ConvergenceService` 与 `StorageSampleAcceptance` 验收，得到 `accepted / research_sample_ready=true`。正式 SQLite 以 `query_only` 读取且前后 SHA-256 相同；全流程 27 次均为本地假 Provider，外部网络和 OpenAI 调用为 0，fixture 复核阶段 Provider 增量为 0。最近一次获准的 DeepSeek/豆包真实隔离讨论为 4/4 成员成功，但当次会议产物没有形成合格的两方案候选板；因此仍不能把本次离线结果宣称为真人确认或完整真实 Provider 闭环，也没有为本次升级额外消耗模型调用。
- 合同轮的“正式发言 → 产物 → 确认”链已增加离线确定性投影和二次完整性审计；伪造的模型风险/行动/决策区会被合同区替换，存储后合同或检查点被修改则拒绝新建或确认产物。该门禁已由隔离回归测试验证，但不等于已完成一次新的真实外部数据闭环。
- 当前 `decision_sha256`、Provider operation 绑定、turn/治理摘要和 `round_trace_anchor_v1` 都使用 canonical SHA-256，目标是发现内容未同步更新摘要或链头的本地误改、局部篡改、删改和降级。业务数据、摘要、锚记录与链头仍在同一个 SQLite 中；能同时重写数据库内容、全部摘要、锚链和链头的恶意管理员仍可伪造一套自洽历史。因此这些机制不是数字签名、外部时间戳、第三方公证或数据库管理员级防篡改保证；若需要该威胁模型，必须把 HMAC/签名密钥或锚根放到数据库之外。
- 正式 `room_storage` 的历史房间设置仍保留 `storage_research_readonly + structured_turn_contract_v1` 兼容 ID；所有房间之后创建的新正式轮都会由内核冻结合同，旧轮和旧消息的合同版本仍保持 `NULL`。OpenD、四股共同截面与 SEC 的实际就绪状态只以运行时接口为准；任一正式门禁未通过时都不启动新的真实样板轮。

### 富途只读行情

富途是可选数据源。桌面“富途牛牛”客户端不等于 Futu OpenD；只安装牛牛客户端不会自动开放 `11111`。请从[富途官方可视化 OpenD 页面](https://openapi.futunn.com/futu-api-doc/quick/opend-base.html)下载安装并登录 OpenD，在图形界面保持监听 `127.0.0.1`、端口 `11111`。Windows 可视化版默认安装在 `%APPDATA%`；本项目只需要行情登录，不需要解锁交易。Python 环境需要 `futu-api`。也可以在 `.env.local` 中设置非敏感连接参数：

```dotenv
FUTU_HOST=127.0.0.1
FUTU_PORT=11111
FUTU_CACHE_TTL_SECONDS=5
```

SEC EDGAR 不需要 API 密钥，但官方要求自动访问声明产品或组织名与联系邮箱。只在本机 `.env.local` 配置真实身份；服务状态不会回显该值。未配置合规 `SEC_USER_AGENT` 时，SEC 适配器保持不可用，不发起抓取，也不伪造申报记录：

```dotenv
SEC_USER_AGENT=Your Product Name contact@example.com
SEC_CACHE_TTL_SECONDS=300
```

只读接口：

- `GET /api/bootstrap`：返回房间分类路径、既有模板能力包、仅用于新建表单的能力包默认值，以及安全的能力包目录；不返回任何密钥。
- `GET /api/market/futu/status`：SDK、OpenD 和能力状态。
- `GET /api/market/storage/readiness?force=1`：分别返回四股正式讨论准入、SEC/IR/业绩材料/FRED 收敛准备度、逐来源覆盖和修复动作；OpenD 离线时不会调用 Futu 行情接口，但会隔离采集可独立访问的公开证据。返回值不包含 User-Agent 原文、密钥或认证头。
- `GET /api/rooms/{room_id}/storage-sample-acceptance`：只读返回当前存储样板七阶段验收、`meeting_reviewed` / `research_sample_ready`、用户决定状态、规范纸面组合门审计、阻断原因和独立统计样本门；不会调用任何模型或市场 Provider，也不会修改旧轮次。
- `GET /api/market/storage/snapshot`：四只存储股的同批次行情、快照基本面、复算技术指标、资金流和财报关键指标证据。
- `GET /api/market/storage/snapshot` 的 `evidence.research_analytics`：非重叠历史窗口基准率和等权模拟组合风险；无历史数据时显式离线，不补值。
- `GET /api/market/storage/history?symbol=US.SNDK&limit=120`：白名单标的日线历史。
- `GET /api/market/storage/financials?symbol=US.MU&statement=income&limit=4`：读取白名单标的利润表、资产负债表、现金流量表或关键指标；只接受固定报表类型。
- `GET /api/market/storage/filings?symbol=US.MU&forms=8-K,10-Q&limit=8`：读取 SEC EDGAR 官方近期申报元数据和原始文件链接；只接受固定标的与固定表单类型。
- `GET /api/market/storage/revenue-breakdown?symbol=US.WDC`：读取最新有效报告期的产品、行业、地区和业务收入构成；保留币种、比例和可用历史期间。
- `GET /api/market/storage/ir-releases?symbol=US.STX&limit=8`：读取固定官方域名的公司 IR 新闻稿，并附可能的 SEC 关联候选；公司一手自述不等同于独立核验。
- `GET /api/market/storage/earnings-packs?symbol=US.MU&limit=12`：读取归一化后的官方季度业绩材料包；优先返回已定位的具体官方材料链接，缺失时退回官方入口，财政期间保留推导置信状态。
- `GET /api/market/storage/earnings-materials?symbol=US.STX&limit=24`：发现具体官方 presentation、prepared remarks、supplemental information、release 或 transcript 链接；`hub_discovery_state`、`discovered` 与 `fetchable` 分开记录。人工核验目录只有在 45 天有效期内且官方文件直链当前可访问时才可进入证据链并覆盖入口故障为 warning；没有实时发现可兜底时，超时、WAF、过期或非白名单重定向仍以 `source_errors` 阻止收敛。若官方入口已实时发现有效材料，失效的人工候选会从 `materials` 排除并进入 `rejected_curated_materials`，同时留下 warning，不能混入证据包。当前 STX 目录已更新到 FY2026 Q4。
- `GET /api/market/storage/industry-proxies`：读取固定 FRED 官方月度序列及库存/出货派生比值；无密钥、六小时缓存、逐序列降级，不能当作 DRAM/NAND/HDD 即时报价。
- `POST /api/rooms/{room_id}/materials/freeze-official-evidence`：由用户把当前官方目录中的一条 `sec_filing` 或 `ir_release` 冻结为房间资料；请求只提交标的、官方 URL 和证据类型，标题、正文边界与元数据由服务端重新生成。

适配层只创建 `OpenQuoteContext`。它没有交易上下文、解锁交易或下单能力；权限不足、OpenD 离线和标的缺失都会作为数据质量事件返回。

当前依赖状态以运行时接口为准，不在文档中宣称持续在线：只有 Futu SDK 可用、OpenD 已启动并登录、行情权限满足且 MU、SNDK、WDC、STX 四行快照完整时，行情证据才可标记为研究 `ready`；`ready` 不等于实时，逐行 `quote_is_live` 与 `freshness_basis` 才说明它是 20 分钟实时窗还是 96 小时内的最近闭市截面。OpenD 离线、权限不足、时间异常或任一标的缺失都会明确降级并阻止研究收敛。SEC 证据同样要求本机合规 `SEC_USER_AGENT` 与官方端点实际可用。两条路径都只读取研究证据，始终声明 `execution_capability=none`、`live_trading_allowed=false`，没有账户、交易上下文、解锁交易、委托或下单能力。

冷启动的独立证据就绪检查会并行读取 SEC、公司 IR、官方业绩材料和 FRED；总等待时间由最慢的只读来源决定，不再把四类网络超时串行相加。公司 IR 与业绩材料适配器内部也按四家公司并发，单家公司多份人工目录文件使用最多四个只读探针并发核验；同一标的和 limit 的并发刷新通过 single-flight 共享一个进行中的请求。IR 错误或空结果短缓存 60 秒，业绩材料受限结果最多缓存 120 秒；`force=1` 绕过已完成缓存但仍合并同键在途刷新。人工目录的正缓存不会跨过 `valid_until` 边界，提速不改变白名单、访问验证和失败关闭语义。

### 12 角色完整闭环验收合同（正式目标）

以下条件必须同时满足，才能把存储产业样板标记为完整验收。代码路径已由临时库 fixture 用户完整证明，但仍需一次获准的真实 Provider 轮次和真人复核才能形成真实样板证据：

1. 12 个正式角色均成功发言，数据质量官是独立必达职责；任一角色失败都只能形成 `PARTIAL` 或不完整草稿。
2. 数据质量官确认所有行情、历史行、财报、新闻和材料均不晚于本轮冻结截止时间，并验证恢复过程没有读入未来数据。
3. 纪要绑定唯一 `round_id`，完整输入来自该轮 12 条职责覆盖消息、消息对应身份版本和冻结材料版本；轮后消息或材料更新不得进入。
4. 摘要及每个结构化条目均有跨版本稳定 `id` 和可审计证据；阻断性分歧必须完成处理，明确标记为非阻断的开放分歧可保留为后续观察项。至少一条已复核反证必须引用本轮合格空头或风控正式发言，不能只证明“反证角色曾经发言”。
5. 空摘要或没有任何结构化条目的空白纪要不得确认；用户确认只确认记录与证据标注，不自动授权交易。
6. 验收只使用 DeepSeek/豆包等当前获准执行器；OpenAI 保持跳过，不预检、不调用、不回退。
7. Futu 始终只读，产物只属于研究、回测或模拟观察；没有账户、解锁交易、委托、支付或真实下单能力。

### 模拟组合与风险预算

- `POST /api/rooms/{room_id}/paper-portfolios`：保存纸面权重、风险预算和压力情景，并用只读复权历史生成确定性风险结果；从当前完整 `support` 决定包创建时同时提交决定 ID 和推导说明，追加 `implements`。
- `PATCH /api/rooms/{room_id}/paper-portfolios/{portfolio_id}`：按 `expected_version` 保存新版本；已确认方案被编辑后会回到 `DRAFT`，关联组合同时追加 `revises`。
- `POST /api/rooms/{room_id}/paper-portfolios/{portfolio_id}/evaluate`：重新读取只读历史并生成新版本的风险复算。
- `POST /api/rooms/{room_id}/paper-portfolios/{portfolio_id}/confirm`：仅在历史数据足够且所有预算检查通过时，由用户确认当前版本；关联组合同时追加精确版本的 `confirms`。
- `POST /api/rooms/{room_id}/observations/{observation_id}/decision-lineage`：由用户把同一研究轮内仍为 `PROPOSED` 的 AI 原提案绑定到当前 `support` 决定和精确已确认组合。服务端保留原 AI、身份版本、方法、阈值和置信度，只追加 `tests` 谱系事件；跨轮、用户自建、已确认、旧组合或冲突绑定均失败关闭。绑定本身不读取市场，之后仍需用户调用观察确认入口才能冻结真实 Futu 基准。
- `GET /api/rooms/{room_id}/paper-portfolios/{portfolio_id}/versions`：查看纸面组合和风险结果的版本轨迹。
- `POST /api/rooms/{room_id}/paper-portfolios/{portfolio_id}/walk-forward`：按精确组合版本运行版本化历史滚动回放，并保存冻结输入与可复算结果；v3 保留固定方案追溯回放，v4 可用服务端白名单规则逐折生成纸面持仓。关联组合只接受决定包内精确已确认版本并追加 `evaluates`。
- `GET /api/rooms/{room_id}/paper-portfolios/{portfolio_id}/walk-forward`：读取不可变回放记录、结果哈希和数据清单。

回放强制覆盖四只股票的 Futu QFQ 已完成日线，在决策截止后的下一交易日收盘模拟入场，窗口内按固定初始名义持有。服务端会给 Futu 同时提交显式的四年起止范围，再保留每股最新至多 500 条完成日线，避免 OpenD 在省略日期时只返回默认短区间。候选语义合同模式只允许用户所选单一标的的固定方向 v3 回放，并把合同同时绑定到冻结输入、运行记录、精确组合版本和 `evaluates` 事件；`walk_forward_integrity_profile_v1` 另冻结原始代际及候选/谱系要求，列表在同一 SQLite 快照内复核，改写版本标签或删除候选、谱系标记都不会把新记录降级成普通旧回放。v4 的 `strategy_rule_contract_v1` 当前只允许 `cross_sectional_total_return_rank_v1`：每折只读取该折训练窗，按训练窗总收益和固定标的次序生成排名，再按已确认纸面组合冻结的多空数量与两侧预算生成该折持仓；测试窗及摩擦场景不会进入拟合。v3/v4 都使用服务端锁定的基准、压力、极端三档纸面假设，分别计入佣金、进出滑点、做空借券费，并用 `turnover`（缺失时用 `close × volume`）检查开仓和退出容量；它们不是实时券商费率、券源或真实成交。任何正式非重叠窗口不可成交都会阻断整档场景并隐藏该场景全部组合收益、正窗口率和回撤，只保留容量证据及明确无摩擦的等权基准。至少 20 个非重叠测试窗口才达到最低数据门；不足时返回 422，不生成 fold、不保存结果，也不会缩短或补造测试期限。500 行上限下默认 `99/20/20` 恰好可容纳 20 个窗口，但实际缺行仍会失败关闭。v4 的 `walk_forward_decision_binding_v2` 分别冻结 AI 首选、用户所选候选及其 revision、来源/最新消息、候选快照哈希、风险适用标记和治理证明，同时冻结策略合同、共同历史和每折拟合决定并在读侧重算；legacy 决定只读兼容为 v1 binding，不冒充显式选择。它仍明确标记 `out_of_sample_claim=false`、`future_performance_claim=false` 与 `retrospective_dataset=true`，因为规则家族是在回看整段历史后加入系统，不能称为预注册样本外验证或未来胜率。该路径模型与 OpenAI 调用均为 0，没有账户、订单或执行动作。完整边界见 `docs/paper_portfolio.md`。

### 模拟观察与历史验证

- `POST /api/rooms/{room_id}/observations`：保存一个待用户确认的方向、期限、阈值、方法 ID/版本、依据、反证和证据引用；从当前决定包派生时必须绑定精确已确认组合版本并追加 `tests(PROPOSED)`。
- `POST /api/rooms/{room_id}/observations/{observation_id}/confirm`：由用户确认并尝试冻结富途真实基准价，谱系追加 `confirms(PENDING_BASELINE / OPEN)`。
- `POST /api/rooms/{room_id}/observations/reconcile`：补取待定基准，成功后为既有观察追加 `revises(OPEN)`；再用 1/5/20 个后续交易日的真实收盘结算到期样本，并追加 `records_outcome(RESOLVED)`。原支持决定后来过期不妨碍既有观察完成这些状态续记，但不能据此新建观察。
- `GET /api/rooms/{room_id}/observations`：读取观察、同行相对结果及 `observation_scorecard_v3`；主指标、最近 20/50 与置信度校准只使用已核验决策谱系和 `qfq_close_to_close_v2` 同口径样本，`scoring_population` 明确报告旧测量口径、未绑定、损坏谱系及重复/重叠排除数。
- `GET /api/rooms/{room_id}/rounds/{round_id}/discussion-audit`：从完整执行轨迹与已核验正式发言合同生成 `discussion_audit_v1`；只读、零 Provider/行情调用，输入截断、跨作用域、封印分歧或篡改均失败关闭。
- `PATCH /api/rooms/{room_id}/observations/{observation_id}/reflection`：编辑真实结算后生成的反思草稿；保存后保持或回到 `DRAFT`。
- `POST /api/rooms/{room_id}/observations/{observation_id}/reflection/confirm`：由用户确认反思；只有 `CONFIRMED` 反思会进入未来轮次。

这里的“观察”不是订单，也不是买卖建议。统计口径、命中判定和防未来数据规则见 `docs/simulation_observations.md`；反思记忆规则见 `docs/reflection_memory.md`。

### 轮次检查点与继续

- `POST /api/rooms/{room_id}/round-launch-plan`：只读生成正式新轮启动确认单；不探测 Provider、不读取行情、不创建轮次或执行账本。
- `POST /api/rooms/{room_id}/rounds/stream`：必须提交确认单对应的 `client_round_request_id / plan_hash / max_provider_calls`；服务端复核计划后才开始新轮，并在每个成员状态完成后更新检查点。
- `POST /api/rooms/{room_id}/rounds/{round_id}/resume/stream`：只允许继续 `PAUSED` 且存在检查点、并能精确找到原 `scope=round` 调用账本的轮次；迁移前没有账本的暂停轮保持暂停并失败关闭。
- `POST /api/rooms/{room_id}/rounds/{round_id}/cancel`：只允许用户明确结束 `PAUSED` 轮次；保留全部历史并解除新轮锁。
- 房间快照返回按时间排序的 `latest_round / round_checkpoint`，并另返唯一权威 `pending_round / pending_round_checkpoint`；即使旧数据库里暂停轮后曾错误出现更新终态轮，界面仍能找回恢复或结束入口。

恢复不会重复写入用户消息，也不会重新抓取富途快照或读取暂停后新增、更新、停用的资料。证据清单、上下文、市场快照哈希或 Provider 成员路由封印不一致时，轮次保持暂停且不会调用模型。v4 检查点冻结失败成员集合；v5 冻结房间能力包与派生能力；v6 再冻结项目工作区缺口与目标职责，因此暂停期间编辑房间或产物只影响下一新轮。同轮及恢复后不会再次调用已失败成员；字段缺失或格式损坏会在轮次进入运行态前失败关闭。任何实际重试仍须从同一轮剩余名额重新预留，旧失败或遗留尝试不退款。详细状态规则见 `docs/checkpoint_resume.md`。

检查点保留 `512 KiB` 的 UTF-8 硬上限；超限仍在模型发言前失败关闭。历史验收用 Futu 证据快照约 163 KB，测试使用约 191 KB 的快照验证完整保存、哈希一致和恢复不重抓，并使用超过上限的样本验证拒绝路径；这些样本大小不代表当前 OpenD 在线。

### 成员生命周期

- `PATCH /api/rooms/{room_id}/members/{member_id}`：必须携带 `expected_version`；过期编辑返回 409，不会覆盖另一页面已经保存的身份或 Provider 路由。无实际字段变化的保存不生成空版本。
- `DELETE /api/rooms/{room_id}/members/{member_id}`：请求体必须携带 `expected_version`，语义是可恢复归档，不删除历史关系；归档成员不再参与调度或新点名。
- `POST /api/rooms/{room_id}/members/{member_id}/restore`：按归档后的精确版本恢复，并恢复归档前的启用/暂停状态。
- `POST /api/rooms/{room_id}/members/reorder`：同时提交新顺序 `member_ids` 与页面读取到的完整旧顺序 `expected_member_ids`；重复、缺失、未知成员或并发变化都会失败关闭。排序不伪造新的身份版本。

### 共享资料解析

- `POST /api/rooms/{room_id}/materials/import-file`：上传 Base64 文件并提取文本，单文件上限 2 MB。
- `POST /api/rooms/{room_id}/materials/fetch-url`：抓取公开 HTTP/HTTPS 来源，响应上限 1.5 MB。
- `POST /api/rooms/{room_id}/materials/freeze-official-evidence`：从可信 SEC/IR 目录创建可审计资料；精确证据 ID 去重，不接受客户端伪造来源层级或正文。
- `GET /api/rooms/{room_id}/materials/{material_id}/versions`：查看最多 30 个版本的时间、哈希和提取方式。
- `GET /api/rooms/{room_id}/materials/{material_id}/versions/{version}`：读取完整历史快照。

PDF 使用 `pypdf`，需要安装 `requirements.txt`；扫描型 PDF 当前不做 OCR，会明确返回“没有可提取文本”。详细边界见 `docs/material_ingest.md`。

### 群聊消息与结构化点名

- `POST /api/rooms/{room_id}/messages/stream`：提交 `content`、最多 8 个 `{member_id, expected_member_version}` 结构化点名、可选 `expected_round_id` 和幂等 `client_message_id`。空闲点名流式返回目标成员回复；活跃轮次只快速持久化插话，由原轮次流在安全调度边界继续处理。前端默认发送 `skip_providers=["openai"]`。
- `POST /api/rooms/{room_id}/chat-requests/{request_id}/resume/stream`：继续尚未终态的空闲点名请求；仅接受 `idle_mention`，轮内插话必须恢复对应讨论轮次。接口先回收已过期租约，再使用请求中已持久化的 Provider 跳过策略，不由客户端临时改写路由。
- `POST /api/rooms/{room_id}/rounds/{round_id}/pause`：幂等记录服务端暂停请求并立即返回；当前在途成员完成后，编排器把最新检查点与 `PAUSED` 状态原子提交。它不宣称能够强制取消已经发给外部 Provider 的 HTTP 请求。
- `message_mentions / chat_requests / chat_request_targets / chat_request_attempts` 保存点名顺序、空闲或轮内请求、逐目标状态、租约尝试和响应消息 ID。`client_message_id` 与消息内容、目标及身份版本、预期轮次和 Provider 跳过策略共同形成路由指纹；同 ID 改变任一语义会冲突，合法重放不会重复写入已经完成的回复。
- 恢复语义是“外部模型调用可能重试、持久化终态回复至多一个”，不是端到端 exactly-once：进程可能在上游已接收请求、但本地尚未提交终态时退出，恢复后因此可能再次调用模型；claim token 只保证旧尝试不能覆盖新尝试或重复落库。
- 纯文本 `@名字` 只负责显示；服务端只接受结构化成员 ID。非正式点名回复不会生成观察提案、会议产物、胜率统计或最终决策。

### 会议产物

- `POST /api/rooms/{room_id}/artifacts/generate`：绑定轮次时先按稳定生成键检查幂等重放；已有产物直接返回且调用次数为 0。新产物必须复用该轮 `scope=round` 调用账本、跳过策略和通过完整性封印的冻结整理路由；请求不能替换成员、Provider 或模型。在途调用尚未终结时返回 `ARTIFACT_ROUND_LEDGER_BUSY`，不会把它标为遗留；账本用尽时不调用 Provider，只生成明确标记 `PROVIDER_CALL_BUDGET_EXCEEDED` 的诚实模板。未绑定轮次且本机存在已配置 Provider 时拒绝模型生成；只有完全没有已配置 Provider 时才允许本地空白框架。
- `PATCH /api/rooms/{room_id}/artifacts/{artifact_id}`：保存新版本，使用 `expected_version` 防止覆盖并发修改。
- `GET /api/rooms/{room_id}/artifacts/{artifact_id}/versions`：读取按新到旧排列的冻结版本摘要、当时证据核验计数、决定数量与完整性状态，不重新计算成当前资料状态。
- `GET /api/rooms/{room_id}/artifacts/{artifact_id}/versions/{version}`：读取精确历史快照、快照/决定绑定哈希与该版用户决定。损坏快照返回 `409 ARTIFACT_VERSION_CORRUPT`，不会回退成当前版本；前端可按稳定条目 ID 对比字段、顺序与证据关系变化。
- `POST /api/rooms/{room_id}/artifacts/{artifact_id}/confirm`：由用户确认；任何非空摘要、需求、风险、结论、分歧、待验证事项或待办缺少证据时都会拒绝确认。已确认需求、已处理风险还有额外完整性门禁。
- `GET /api/rooms/{room_id}/artifacts/{artifact_id}/evidence-sources`：返回 `artifact_evidence_sources_v2` 权威来源投影，包括本轮 manifest 中的精确资料版本、同轮且不晚于产物创建时间的可见消息，以及唯一冻结市场快照；不请求当前实时行情。历史资料缺版、损坏或同版本重复时进入 `unresolved`，绝不回退最新版。`source_identity_exact=true` 只代表来源身份精确；只有预览非空、未截断、未脱敏且未耗尽总预算时，`preview_complete=true` 才允许标记为已核对。正式轮次产物前端只使用这份来源集合，避免用户勾选轮后资料后被服务端静默丢弃。`round_market_snapshot` 关系仍由服务端绑定轮次、证据版本与 SHA，模型或前端不能伪造，也始终从 `context + unreviewed` 开始。
- `GET /api/rooms/{room_id}/artifacts/{artifact_id}/evidence-sources/{source_type}/{source_id}`：仅为该产物权威目录中的 `message` 或 `round_market_snapshot` 按需读取完整冻结内容；再次复核房间、轮次、产物时间边界、成员版本或市场快照 revision/SHA，绝不访问实时行情。响应严格限制为 300 KiB 并继续清理凭证；超过上限、内容为空或发生脱敏时仍不能核验。资料继续复用精确历史版本接口。
- 新建或显式保存绑定有效市场快照的轮次产物时，服务端会把该轮唯一的 `round_market_snapshot` 精确 revision/SHA 自动加入摘要证据；确认前必须由用户把这条关系核验为 `source_checked` 或 `corroborated`。读取、装饰或确认旧产物不会静默写入新版本。
- `POST /api/rooms/{room_id}/artifacts/{artifact_id}/user-decision`：创建 `artifact_user_decision_v2`。三种动作都提交 `expected_version / action / rationale`；`support` 还必须显式提交 `selected_option_id`，治理适用时同时提交候选 revision、来源/最新消息与治理证明令牌，服务端重放并封印精确选择。`hold` / `return` 必须完全省略五个选择字段。未知字段及账户、订单、价格、数量字段全部拒绝；该记录不具有任何执行或资金权限。
- `GET /api/rooms/{room_id}/decision-packages`：读取 `decision_package_v2` 锚点、AI 首选与用户选择、版本化谱系、并行验证事件和本地完整性状态。
- 确认门禁也会拒绝未核验关系、缺少已核对支持证据、或没有说明的反证/争议；`CONFIRMED` 只代表用户确认会议记录与证据标注，不代表事实已被系统独立证实。
- 关联组合的历史滚动回放按钮只在组合为 `CONFIRMED`、属于当前有效支持决定链且精确版本存在 `confirms` 事件时启用；后端继续执行相同的失败关闭检查。
- 代码门禁已经要求：产物绑定完整轮次、只使用同轮消息、manifest 中冻结的材料版本与同轮市场快照、保留稳定条目 ID、拒绝空白纪要，并把阻断性开放分歧与项目风险挡在候选方案收敛之前。快照 revision/SHA 漂移会保留旧 pin、清除核验状态并阻断确认，不能静默重新钉住。正式 12 角色轮次已经持久化豆包生成的绑定草稿；该迁移前草稿仍保持 `DRAFT v1`，现有 43 条关系（42 条消息、1 条资料）全部待用户核验。为了保留审计历史，单纯读取或尝试确认不会改写它；用户下一次显式保存时，服务端会新增草稿版本并自动加入第 44 条未核验的冻结市场快照关系，完成核验前仍不能确认、形成用户最终决定或进入模拟组合验证。

完整状态、证据和导出规则见 `docs/artifact_workflow.md`。

### 通用多 AI 动态决策板验收

`scripts/run_isolated_generic_room_e2e.py` 以 SQLite `query_only` 读取正式 `room_plan` 的四份可编辑身份，在临时数据库中验证“主持定界 → 证据与反方 → 候选方案整合”的动态流程。隔离克隆原样保留源房间的领域能力包，不再注入 `structured_turn_contract_v1`；脚本必须证明合同来自新轮自身冻结的核心协议。它不会改动正式源房间；既有轮次不会被迁移，暂停恢复也不会改用新协议。

验收要求每次正式发言都有合格 `turn_contract_v1`、四位不同成员全部被合格合同覆盖且可见消息不泄漏隐藏合同块。除本轮首位成功 AI 外，每条合同必须以 `supports / challenges / qualifies / questions` 回应至少一条同轮此前成功的正式 AI 消息；服务端把第一条合同回应同步为群聊 `reply_to_message_id`，持久化重放会再次核验合同边、回复 ID 与回复人名一致。方案整合身份必须在合同中比较至少两个真实候选，唯一选择一个可撤回的条件化首选并保留理由、反证、失效条件和用户核验边界。会议产物的候选 ID、状态、首选和理由随后由持久化合同账本确定性投影并覆盖整理模型对应字段；脚本会把投影前后的核心决策板逐项比较，因此整理模型不能凭空指定或删掉“最佳方案”。dry-run 还会在临时库中模拟用户逐条核验证据、处理开放风险与分歧、确认精确产物版本，并要求最终状态仍停在 `READY_FOR_USER_DECISION`，不能由 AI 自主决定。

```powershell
python scripts/run_isolated_generic_room_e2e.py --dry-run
$env:AI_STUDIO_SKIP_LOCAL_ENV = "0"
python scripts/run_isolated_generic_room_e2e.py `
  --execute-real `
  --acknowledge-paid-calls MAX_16_PROVIDER_CALLS `
  --report-file runtime\acceptance\generic-room-real-YYYYMMDD-HHMMSS.json
```

真实模式同样要求调用进程显式设置 `AI_STUDIO_SKIP_LOCAL_ENV=0`；未设置时不会读取源房间或发起 Provider/Futu 请求。

历史隔离报告 `runtime/acceptance/generic-room-real-20260801-decision-stage.json` 曾在合同升级前通过：四位不同成员完成五次发言，绑定草稿包含两个候选、首选与理由，OpenAI 零调用且正式数据库未改变。随后用于验证豆包 JSON Object 传输的报告 `runtime/acceptance/generic-real-20260801-165626.json` 中，4/4 成员讨论仍成功、11 次 DeepSeek/豆包调用均无失败或重试，但整理模型没有形成候选板，因而以 `ARTIFACT_GATE_FAILED` 诚实失败。两份都是升级前历史证据；当前合同投影修复已通过零网络 dry-run，但仍需用户再次明确授权付费调用，才能生成一份合同版真实验收报告。任何报告都不等于用户已核验或系统可自主决定。

### 隔离 12 角色验收脚本

`scripts/run_isolated_12_role_e2e.py` 把正式 `room_storage` 的当前 12 份身份快照通过 SQLite `query_only` 复制到系统临时数据库，并把正式房间显式配置的 `moderator_member_id` 精确映射到对应克隆成员，再验证 Futu 四股门禁、DeepSeek/豆包会前检查、动态主持、完整轮次、检查点和绑定该轮的会议产物。dry-run 随后只在临时库中由明确标注的 `isolated_fixture_user` 完成证据复核、精确版本确认、`support` 决定和安全纸面组合，再调用真实收敛与样板验收服务；未复核产物、过期产物版本和过期决定版本都必须失败关闭。真实模式不会模拟用户，也不会调用确认接口。缺少显式主持、主持不属于当前十二位启用成员、检查点主持漂移或任一主持调用使用了其他成员都会失败关闭。正式数据库只读；脚本比较运行前后的主库与 WAL 内容指纹，并要求只读连接 `total_changes=0`。临时政策把成功成员数强化为 12、显式要求 `data_guardian`、把追问预算限制为 1。

先运行完全本地的 dry-run；它使用本地 Provider 与行情夹具，不访问 Futu 或任何模型网络：

```powershell
python scripts/run_isolated_12_role_e2e.py --dry-run
python scripts\run_backend_tests_isolated.py tests.test_isolated_12_role_e2e --verbosity 2
```

dry-run 的单行 JSON 必须显示：12 位不同成员完成、首轮 DeepSeek 9 / 豆包 3、一次行情快照、两个唯一模型路由、27 次本地调用、外部网络与 OpenAI 调用均为 0、正式显式主持已映射且所有主持尝试与检查点均指向该克隆成员、检查点成功成员 12；并显示产物最终为 `CONFIRMED v3`、证据全部复核、fixture 用户决定精确绑定当前版本、纸面组合风险门通过、真实收敛状态为 `USER_SUPPORTED`、样板状态为 `accepted / research_sample_ready=true`。报告还必须注明 fixture 用户不代表真人、不能自主决定，并证明复核阶段 Provider 调用增量为 0。对应自动测试会在子进程加载脚本前直接阻断 `socket.create_connection` 与 `socket.socket.connect`；任何旁路联网尝试都会使测试失败，而不只依赖报告中的调用计数。

真实模式不会默认启动，必须同时显式设置 `AI_STUDIO_SKIP_LOCAL_ENV=0` 并确认最多 28 次 Provider 调用；未设置该环境变量时，即使确认短语正确也会在读取源房间前失败关闭。该数值与当前 UI 中存储委员会的推荐拆分一致，但不是所有房间的固定上限：

```powershell
$env:AI_STUDIO_SKIP_LOCAL_ENV = "0"
python scripts/run_isolated_12_role_e2e.py `
  --execute-real `
  --acknowledge-paid-calls MAX_28_PROVIDER_CALLS `
  --report-file runtime\12-role-e2e-report-YYYYMMDD-HHMMSS.json
```

真实模式只注册 DeepSeek 与豆包，OpenAI 路由会在网络请求前硬拒绝；不重试、不跨 Provider 回退，Futu 强制抓取一次且必须 MU、SNDK、WDC、STX 4/4 `ready`。逐行还会失败关闭明确的 `suspended=true` 或异常 `security_status`；历史记录缺少该字段保持兼容，只有 `NORMAL` 或以 `.NORMAL` 结尾的枚举文本被视为显式正常。在任何 DeepSeek/豆包会前探测或付费生成调用前，该快照的 `evidence` 还必须是字典、`evidence.state=ready`，且递归检查所有嵌套层级时不得有任何非空 `source_errors`；否则立即以 `MARKET_GATE_FAILED` 停止，Provider 调用计数保持为 0。任一成员失败会停止轮次并跳过产物生成；产物只生成一次，禁止 `template_fallback`，脚本不会调用任何确认接口。标准输出和可选 `--report-file` 都只包含不带提示、正文、上游错误体或密钥的 JSON 摘要；报告文件必须使用尚不存在的 `.json` 路径，脚本不会覆盖旧审计记录。

## 验证

```powershell
python scripts\run_backend_tests_isolated.py --layer migration --verbosity 2
python scripts\run_backend_tests_isolated.py --layer domains --verbosity 2
python scripts\run_backend_tests_isolated.py --layer full --verbosity 1
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run build
```

前端测试入口按文件逐一创建受监控进程；每个文件使用单并发、`--test-isolation=none`、2 GiB old-space、3 GiB private-memory 守卫、64 MiB 输出守卫和 120 秒超时。输出以临时文件流式承接而不是堆在 runner 内存中；超限或 runner 自身发生异常时都会回收整棵测试进程树并删除临时文件，避免 Windows 本机因失控测试耗尽提交内存。不要绕过该入口直接运行 `node --test`；定向验证单个文件时使用 `npm.cmd --prefix frontend run test:file -- tests/<name>.test.js`。`roomInspectorNestedLazy.dom.test.js` 已改为无 Vite/JSDOM 副作用的静态契约回归，禁止重新引入顶层 Vite server 或未决 lazy-module gate。

不要直接运行未设置完整隔离环境的 `python -m unittest discover`；测试入口会在导入应用前创建系统临时 runtime、设置显式临时 SQLite、清除 Provider 密钥并把 Futu 指向不可用的回环测试端点。

## 目录

- `backend/templates.py`：房间与成员模板。
- `backend/store.py`：SQLite 数据和身份版本。
- `backend/orchestrator.py`：动态主持、发言和安全回退。
- `backend/round_contexts.py`：领域无关的版本化正式轮上下文注册、授权、冻结与恢复。
- `backend/football_research.py` / `backend/stock_research.py`：闭合的足球与通用股票只读合同。
- `backend/turn_envelope.py`：纯 JSON 发言封套、严格解析与 schema 哈希。
- `backend/providers/output.py`：Provider 输出能力协商与单次分发。
- `backend/store.py`：同时保存轮次检查点、恢复计数和冻结上下文。
- `backend/market/`：富途只读行情、共同证据快照、确定性技术指标和研究提示上下文。
- `backend/market/research_analytics.py`：非重叠历史基准率与等权模拟组合风险，不接触账户或订单。
- `backend/observation_service.py`：用户确认、真实基准、交易日到期结算与防未来数据验证。
- `backend/store.py`：同时保存结果绑定的反思草稿、版本历史和用户确认状态。
- `backend/material_ingest.py`：安全网页抓取、文件文本解析、哈希和来源元数据。
- `backend/store.py`：同时负责资料版本与消息引用关系。
- `backend/artifact_service.py`：会议纪要生成、结构校验和无模型时的诚实回退。
- `backend/providers/`：模型适配器。
- `frontend/`：React 客户端。
- `docs/architecture_v2.md`：当前目标架构与升级顺序。
- `docs/open_source_upgrade_map.md`：TradingAgents、AI Hedge Fund、FinRobot、LangGraph 等方案的机制复用与许可边界。
- `docs/industry_proxy_boundary.md`：官方月度行业代理、派生口径与不能外推到单家公司或即时报价的边界。
