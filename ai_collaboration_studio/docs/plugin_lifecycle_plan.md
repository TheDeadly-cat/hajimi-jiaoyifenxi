# Capability pack / domain adapter / UI contribution 生命周期合同

> 状态：P25 阶段 1、P26 阶段 2 的首个只读竖切，以及 P27 的第二个真实非交易端口均已离线实现；阶段 3–4 仍是后续计划。本文不授权在线安装、动态代码加载、真实交易、Provider 调用或外部写入。

## 目标与硬边界

插件体系服务于通用多 AI 共创内核。能力包只声明领域能力与依赖；domain adapter 只实现明确端口；UI contribution 只能挂载宿主编译期组件。核心主持、共享记录、治理证明和用户最终决定不属于插件，也不能被替换。

TradingAgents 只作为未来交易流程包的设计参考，不复制进通用 orchestrator，不修改其目录，不获得账户、下单、支付、钱包或资金动作能力。

所有合同继续固定：

- `execution_capability=none`
- `live_trading_allowed=false`
- `can_autonomously_decide=false`
- `can_replace_user_decision=false`
- `arbitrary_code_loading_allowed=false`
- `user_final_decision_required=true`

## 阶段 1：追加式生命周期账本

P25 已新增独立、只追加的 lifecycle event 账本、不可变注册记录和 CAS current head，不直接改写历史 snapshot。状态分为两个正交轴：

- activation：`enabled | disabled | quarantined`。
- catalog：`active | deprecated | tombstoned`。

`enabled + active` 允许新房间或下一轮解析；`disabled` 与 `quarantined` 禁止新解析并关闭插件动作；`deprecated` 保留精确旧版本只读并禁止新选择；`tombstoned` 是不可恢复终态，保留 ID、kind、version、manifest/contract hash、时间、原因和 replacement，不运行卸载脚本，也不删除历史业务数据。

每次变更都绑定 `client_request_id`、可重算的规范请求语义封印、预览封印、期望 head sequence/hash、原因和两项用户确认。`plugin_lifecycle_impact_preview_v2` 在提交前封印事件时实现可用性，使仍运行旧前端的页面在服务升级后先拒绝新预览，不会出现“服务端已经变更、旧界面却误报失败”。`plugin_lifecycle_event_v2` 再把 `implementation_available_at_event` 纳入事件哈希，并把规范请求语义作为独立持久封印；读取会核对事件的目标、动作、原因、replacement、sequence 和前置 head。旧 v1 事件继续按原 payload 验证且请求封印列必须为空，不会因迁移改写 hash。相同 ID 与相同语义返回原事件时的冻结结果；同 ID 改语义或事件与原请求漂移都会失败关闭。事件与 head 在一个 `BEGIN IMMEDIATE` 事务内提交，故障不得留下孤立事件或漂移 head。

房间、正式轮次和产物分别冻结 closure-specific lifecycle resolution，并绑定当时真实存在的历史 ledger head；当前 catalog 变化不会改写历史 resolution。运行时读取另行叠加 current lifecycle overlay，只读历史与当前可执行性不得混为一体。

读取时分别返回 `integrity_ok` 与 `runtime_available`。精确封印仍完整但本机实现缺失时显示 `implementation_unavailable`，不得用最新版替算。

replacement 在 v1 中只是可审计提示，不触发自动迁移：只允许同一 stable plugin ID 的另一个已登记精确版本，并继续核对 adapter/UI/pack 的合同族与挂载边界。后续 disable、enable、quarantine 或 clear 不会清空已声明 replacement；reinstate 清除，tombstone 可显式覆盖。读取会重验历史 replacement 合同、缺失或损坏目标及 effective replacement 图环，同时投影 replacement 当前是否仍完整、可用于新绑定，并固定 `automatic_migration_performed=false`。

`implementation_unavailable` 的旧精确版本仍可执行安全退休动作 `disable | quarantine | deprecate | tombstone`，因此真实宿主升级后可以把旧 stable ID 版本指向新精确版本；`enable | clear_quarantine | reinstate` 仍要求本机存在完全匹配的实现。读取和幂等回放使用 v2 事件封印的 event-time 可用性，不会随之后的宿主升级漂移。

## 阶段 2：按声明端口拆分 domain adapter

把当前偏市场型的统一 adapter 接口拆为公共身份/安全合同与可选端口：

- `core.artifact.projection/v1`
- `core.round.context/v1`
- `core.turn.payload/v1`
- `core.market.readonly_context/v1`
- `core.simulation.local/v1`

注册器只验证 manifest 声明的端口。非交易 adapter 不再实现虚假的行情、时间线或 preflight no-op。每个端口单独声明输入/输出 schema、读面、本地写面、Provider/市场预算、失败关闭策略和版本范围。

P26 已完成这一阶段的最小闭环：旧 `capability_pack_manifest_v1`、`domain_adapter_contract_v1`、`ui_contribution_contract_v1` 与 `plugin_registry_snapshot_v1` 保持原字节语义；新 `project_readiness_review` 单独使用 manifest/adapter/UI contribution v2 和 `plugin_registry_snapshot_v2`。snapshot v2 允许未参与端口解析的 v1 adapter 与 v2 port-only adapter 共存，只把被 `port_resolutions` 精确引用的实现绑定到端口、schema、handler、预算与失败策略。

`plugin_registry_catalog_v2` 额外登记宿主端口与宿主视图模型 schema。readiness UI contribution 显式声明 required/one 的 source port 与 host-owned view model；浏览器只渲染编译期白名单组件，不接受插件 JSX、HTML、模块 URL 或 callback。历史解析不依赖当前“最新版”，而由 P25 已持久化的 lifecycle target snapshot/ledger 锚定；当前实现缺失时保留历史只读并关闭新调用，不静默换算。

P27 以追加式 `project_round_focus` 包验证第二种真实数据生命周期：port-only adapter 只实现 `core.round.context/v1`，把宿主已封印的项目就绪度投影和当前房间目标转换为“下一轮补缺清单”。没有已确认产物时必须显式输出 `artifact_binding.status=none`、零缺口和当前安全目标，不得自动吸附后来出现的产物，也不得伪造空成功。

用户先读取 `project_round_focus_preview_v1`，再用独立授权确认其精确来源和预览封印；只有启用该包的房间使用 `round_launch_plan_v4`，授权字节进入计划哈希。未启用的房间继续保持 v3 与旧哈希闭集。正式启动在同一 `BEGIN IMMEDIATE` 事务内重建来源、端口、registry、lifecycle 和请求语义，再原子写入 round 与唯一 `round_domain_context_v1`。历史读取会重算 context 全部封印、round 独立锚及 append-only execution trace anchor 链；任一漂移整组隐藏，但核心轮次仍可读。合法暂停恢复只使用冻结 context；当前插件停用后历史仍可读并明确为不可执行。

RoomInspector 只显示精确来源、缺口计数、最多三条焦点和建议目标。“填入下一轮目标”仅填入可编辑文本；用户仍可修改并亲自打开启动确认单。该端口固定 Provider=0、market reads=0、adapter business writes=0，不点名成员、不修改流程、不排名、不产生 approval，也不进入或替代用户最终决定。

## 阶段 3：独立插件迁移账本

插件迁移使用独立命名空间与账本：

- 只允许宿主内置、可审计的确定性迁移。
- 每次迁移绑定 plugin ID/version、代码内置 migration ID、前后 schema hash 与结果 hash。
- 禁止插件提交任意 SQL、脚本、网络下载或 cleanup hook。
- 迁移失败整体回滚；不得修改其他插件表、核心决定链或用户文件。
- tombstone 不触发数据删除；清理由用户另行明确授权。

## 阶段 4：冲突与升级策略

解析新 snapshot 前检查：

- 依赖闭包与有限内核版本范围。
- pack、adapter、UI contribution 的双向绑定。
- singleton slot 冲突、端口版本冲突与本地表命名空间冲突。
- replacement 是否提供显式、可审计的迁移路径。

禁止静默升级、降级或换算。旧 snapshot 只按精确合同读取；新版本必须由用户在房间设置中明确选择，并只作用于下一正式轮次。

## 非交易验证路径

`structured_project_research` 继续作为无 adapter、无市场读取、无 Provider、无外部写入的静态基础包，通过 `project_research.artifact_workspace/v1` 验证冻结 UI contribution。

P26 新增的 `project_readiness_review` 是第一个 port-only 非交易 adapter：它只接收宿主提供的精确已确认 artifact version 与证据关系快照，生成确定性的需求、证据、风险与阻断缺口投影。它不接收 `StudioStore`、Provider、市场服务、orchestrator 或任意执行句柄；固定 Provider=0、market reads=0、business writes=0，不排名、不推荐赢家、不产生 approval，也不进入或修改用户最终决定 slot。

P27 新增的 `project_round_focus` 是第二个 port-only 非交易 adapter：它只接收宿主给出的冻结就绪度投影与严格房间上下文，通过 `core.round.context/v1` 生成下一轮目标预填建议。adapter 本身不写数据库；宿主只在用户确认正式启动时原子写入一条不可变轮次谱系。无产物 bootstrap、精确产物、当前停用但历史可读三种状态均有独立投影，任何状态都不会自动启动讨论或改变用户决定。

## 验收门

1. 新增无关 pack 不改变既有房间的所选依赖闭包封印。
2. disabled/deprecated/tombstoned 后历史仍可读，但所有新动作失败关闭。
3. pack、adapter、UI contract 任一版本/hash/反向绑定漂移均不可激活。
4. 迁移故障无部分写入、无跨命名空间写入、无历史删除。
5. 最终决定组件始终由内核渲染；插件不能贡献、隐藏或替换它。
6. 离线验收 Provider=0、市场读取=0（除明确只读市场端口测试）、正式数据库/WAL 不变。
7. 1280/760/390 下 ready/read-only/legacy/unavailable/integrity-failed 状态均可辨认且无横向溢出。
8. 第二端口的新房间使用 v4 计划并原子生成一个 round 与一个 context；源漂移或 context 写入故障必须留下零个部分轮次、孤立 context 或 Provider 账本。

只有生命周期账本、端口拆分、迁移隔离和上述验收全部通过后，才讨论第三方包分发或签名；在线插件商店与动态代码加载不在当前路线内。
