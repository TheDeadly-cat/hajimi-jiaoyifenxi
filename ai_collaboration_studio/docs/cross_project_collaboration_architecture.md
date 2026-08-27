# 跨项目协作内核路线图

> 状态：源码合同与隔离测试已更新；正式数据库迁移、正式服务重启、Provider 调用、市场访问、动态代码加载和自动执行仍未获本文授权。

## 结论

Studio 已把调用方身份、便携请求、结果档案、版本协商和权限范围提升为源码级一等合同。它仍不是一个已自动启用的多租户平台：正式实例需要单独迁移、配置项目 capability 签名秘密并重启后，外部调用才会实际可用。

`GET /api/integration/manifest` 现在返回 `studio_integration_manifest_v2`。它只描述编译期能力和固定安全边界，不读取 SQLite、Provider、市场、环境秘密或本地文件，也不表示 capability secret、MCP 或 Provider 已经就绪。`/api/bootstrap` 的前端会话凭据明确不能用于项目调用。

## 必须长期保持的边界

- 主宿主只监听回环地址；不为集成放开 iframe 或跨域浏览器写入。
- 能力包和适配器保持 `execution_capability=none`、`live_trading_allowed=false`、`can_autonomously_decide=false`。
- 第三方项目不能向宿主注入 Python、JavaScript、HTML、模块 URL 或回调；扩展先采用声明式数据合同和宿主白名单渲染器。
- ChatGPT 人工导入只提供建议；独立 API 审查、确定性校验和用户最终确认仍是不同阶段。
- MCP 保持独立进程、只读、房间与轮次绑定、短 TTL；结果导入和任何写入仍回到主宿主。
- 外部项目不得复用 `/api/bootstrap` 返回的进程级前端令牌。项目 capability 单独绑定调用方、项目、房间、动作 allowlist、audience、TTL、jti、请求哈希和幂等请求身份。

## 正确的三层集成面

| 层 | 用途 | 当前状态 | 权限 |
| --- | --- | --- | --- |
| 发现与协商 | 查询版本、能力包、端口、schema hash 和安全边界 | `studio_integration_manifest_v2` 与 plugin catalog v3 已提供 | 纯静态只读 |
| 上下文读取 | 为已授权的冻结轮次读取有界上下文、证据块、状态和导入合同 | 独立只读 MCP 已提供 | room + round + TTL，只读 |
| 请求与结果 | 幂等创建跨项目房间并读取机器可读结果 | 源码已实现，正式授权运行态未探测 | caller + project + request + room + action + TTL |

前两个面不能被解释为第三个面的授权。发现成功不代表调用已获准，MCP 可读也不代表可以创建房间、导入结果或触发 Provider。

## 不同领域真正需要的合同

| 领域 | 建议结果档案 | 确定性能力 | 特殊边界 |
| --- | --- | --- | --- |
| 算命 / 命理研究 | `research_report_v1` | 历法、时区、规则包版本和排盘结果由确定性引擎生成 | 出生信息是敏感个人数据；模型解释不能改写排盘事实，也不能冒充确定命运 |
| 交易研究 | `decision_v1` + 研究证据包 | 点时行情封印、复权/公司行动、回放与纸面评估 | 永久研究只读；不连接交易账户、不解锁、不下单，不把模型置信度写成胜率 |
| PPT / 文档共创 | `artifact_draft_v1` | 页面结构、引用关系、导出清单、render/verify 收据 | 输出路径由用户选择；需要 PPTX 输入提取、机器可读结果包和渲染验收，不允许隐藏覆盖文件 |
| 足球研究 | `research_report_v1` 或 `decision_v1` | 比赛身份、开球 UTC、阵容与资料截止时间封印 | 赔率只作代理信息；不生成未经校准的未来胜率，不投注、不接钱包 |

`collaboration_result_v1` 已支持 `decision_v1`、`research_report_v1` 和 `artifact_draft_v1`，并把独立 API 审查和用户最终决定保持为不同字段。没有已验证结果时返回合法的 pending/withheld 档案，不生成伪结论。Manual ChatGPT 仍是人工复制/导入，模型身份声明不被当作真实性证明。

## 尚未闭合的高优先缺口

### 已闭合：外部调用者的最小权限

`project_invocation_envelope_v1` 与 `project_invocation_capability_v1` 已包含：

- `client_request_id` 与确定性请求哈希；
- `caller_id / project_id / source item / source revision`；
- 输入内容哈希、数据分类与保留策略；
- 请求的能力包、结果档案、预算上限和用户确认边界；
- 允许动作、目标 workspace/room、audience、TTL、jti；
- 幂等重放返回同一结果，语义漂移必须冲突。

写入口固定为 `POST /api/integration/project-invocations`，结果入口固定为 `GET /api/integration/project-invocations/{client_request_id}/result`。两者只接受独立 Bearer capability，bootstrap UI token 或双凭据均失败；写入范围仅为项目 intake、专属房间和初始房间版本，不触发 Provider、市场读取或执行能力。

### 部分闭合：项目隔离和可恢复性

- 增加 `workspace_id / project_id` 命名空间、配额、审计和删除/导出范围。
- `no_payload_retention` 已对全部数据分类统一去除房间标题/目标正文；`ephemeral_24h / bounded_days` 会持久化精确到期时间、从创建起也不落这两项正文，并在结果读取到期时返回 410。哈希审计元数据继续保留，项目级物理删除/法律留置仍属后续治理。
- Manual ChatGPT 已增加任务列表与历史任务切换，避免只显示 latest；房间切换的迟到响应不会覆盖新房间状态。
- API 审查已增加严格的零调用孤儿恢复与显式重新授权；恢复保留不可变快照，不静默退款或自动重试，下一次审查必须使用新的 `client_request_id`。
- 在审查前、决定卡发布前和最终冻结前重验冻结上下文；角色、材料、候选或历史决定变化时失败关闭。

### 已闭合基础：长期版本兼容

- 受信任目录已新增 `(kind, stable_id, exact_version) -> immutable contract` 的 v3 历史链和 sealed latest alias；既有 v2 房间快照保持兼容。
- 历史房间必须按冻结的旧合同验证，不能用今天的 current 重建替换。
- 对外清单只发布真实已实现端口；planned port 与 implemented port 分开。
- 每个请求、结果、端口和工具都发布 schema version、schema SHA 和有限兼容范围，未知版本失败关闭。

### 部分闭合：便携输入与输出

- 提供本地 Python SDK/CLI，但由集成清单生成或验证合同，不复制内部 URL 与 bootstrap 逻辑。
- 已增加带 provenance/hash 的 `collaboration_result_v1`，来源项目可精确关联请求、源修订、房间和结果。
- 已增加安全 PPTX ZIP/XML 提取、宏/OLE/ActiveX/嵌入对象拒绝、外链不抓取、render package 与人工验收 receipt。XLSX 尚未接入本合同。
- 增加宿主拥有的通用 schema-card renderer，避免每个领域都修改 React 内核，同时继续禁止第三方 JSX/HTML。

### P2：运营与治理

- 调用方级速率、并发、预算、超时、取消、重试和熔断收据。
- 继续补齐可审计物理删除、项目导出、法律留置和日志脱敏；当前 intake 已执行最短保留与到期拒读，但算命个人数据与金融数据仍不能沿用普通材料默认策略。
- 端到端 correlation ID、阶段事件、错误分类和无秘密诊断包。
- 每个领域的 golden fixtures、兼容矩阵、畸形输入、重放、崩溃恢复和可访问性验收。

## 仍然容易忽略、且尚未闭合的缺口

1. **原始输入传输**：v1 项目调用只接收内容哈希和字节数，不接收或抓取原始 payload。这保护算命出生资料和金融资料，但意味着调用方仍需通过人工材料导入或未来受控 adapter 提供内容；清单明确标为 `hash_manifest_only`。
2. **capability 发放与撤销**：签名秘密只应留在可信宿主/代理进程。当前不提供网络 mint endpoint；需要后续的操作员工具、轮换、撤销表和审计，而不是把签名秘密复制给每个业务项目。
3. **异步任务生命周期**：现在有幂等创建和结果轮询，但还没有通用 queued/running/cancelled/expired 状态机、取消收据、死信队列或回调签名。
4. **项目级删除/导出**：caller/project 命名空间已有持久化身份，但尚无经过授权的整项目导出、删除、保留期清理和法律留置流程。
5. **领域确定性内核**：足球/交易已保持只读，PPT 已有结构与渲染收据；算命仍需要真正版本化的历法、时区、节气和排盘引擎 receipt，不能用 LLM 解释代替。
6. **版本协商 SDK**：manifest 和 exact-version catalog 已存在，但 Python/TypeScript 消费 SDK、兼容矩阵与 contract test kit 尚未提供。
7. **正式运行证据**：源码测试和 build 不证明桌面快捷方式对应的正式实例已迁移、已配置 secret、已重启或通过浏览器验收。

## 建议实施顺序

1. 以 `studio_integration_manifest_v2` 作为唯一发现入口，为调用方增加 contract test kit。
2. 先以临时数据库完成正式迁移预演，再由用户单独决定是否迁移并重启桌面实例。
3. 为 capability 建立可信发放、轮换、撤销和审计，不新增匿名 mint API。
4. 先接 PPT 作为 artifact/render/export 样板，再接带确定性 receipt 的算命内核；交易和足球继续只读。
5. 增加受控输入 adapter、异步状态/取消和项目级保留治理。
6. 最后再提供 Python/TypeScript SDK 和声明式适配器分发；不以放宽本机安全策略换取“易接入”。
