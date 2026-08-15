# P28 独立插件迁移账本计划

> 状态：仅规划，尚未实现。本文不授权执行任何迁移、安装脚本、任意 SQL、网络下载、数据删除、Provider 调用、市场访问或正式数据库变更。

## 为什么 P28 现在只做计划

P26 的 `project_readiness_review` 是只读产物投影，P27 的 `project_round_focus` 也只生成纯上下文；唯一持久化的 `round_domain_context_v1` 由宿主作为核心轮次谱系原子写入，不是插件自有可变数据。当前没有一个真实的插件私有 schema v1→v2 用例，直接实现迁移框架只会得到不可验证的空壳。

P28 的实施触发条件是：出现第一个确实需要升级、且不能只靠新增只读 projection 解决的插件自有状态。届时先用该真实数据做一条宿主内置迁移，再决定是否抽象第二种迁移形态。

## 目标与不做项

目标是为一个精确插件 stable ID 的宿主托管数据提供可预览、可确认、可重算、可回滚和可审计的版本迁移。迁移只能改变被宿主明确分配给该插件的命名空间，不能改变房间核心记录、共享消息、治理证明、用户最终决定或其他插件数据。

本阶段明确不做：

- 不接收插件提供的 SQL、Python、PowerShell、二进制、URL 或 cleanup hook。
- 不动态加载第三方迁移代码，也不从网络下载 migration。
- 不做静默升级、自动 replacement、自动 tombstone 或卸载清理。
- 不删除旧历史、不原地改写不可变记录、不回填 legacy 数据。
- 不给予 Store、SQLite connection、Provider、市场服务、orchestrator、账户或执行句柄。

固定安全字段继续为：

- `execution_capability=none`
- `live_trading_allowed=false`
- `can_autonomously_decide=false`
- `can_replace_user_decision=false`
- `arbitrary_code_loading_allowed=false`
- `provider_calls_performed=0`
- `market_reads_performed=0`
- `user_final_decision_required=true`

## 宿主内置 migration 合同

每条迁移由编译期宿主注册表声明，不由插件 manifest 自授权限：

```json
{
  "version": "host_plugin_migration_contract_v1",
  "migration_id": "project_notes.schema/v1-to-v2",
  "plugin_stable_id": "project_notes",
  "from_plugin_version": "1.0.0",
  "to_plugin_version": "2.0.0",
  "from_schema_version": "project_notes_state_v1",
  "to_schema_version": "project_notes_state_v2",
  "input_schema_sha256": "...",
  "output_schema_sha256": "...",
  "namespace": "plugin.project_notes",
  "allowed_source_tables": ["plugin_project_notes_state_v1"],
  "allowed_target_tables": ["plugin_project_notes_state_v2"],
  "max_rows": 10000,
  "max_input_bytes": 8388608,
  "failure_policy": "rollback_all",
  "migration_code_sha256": "..."
}
```

所有字段使用严格闭集。版本范围不能代替精确版本；`migration_code_sha256` 只标识随宿主发布、可静态审阅的纯函数实现。注册表拒绝通配符、OR 范围、未知表、跨 namespace 表和任何动态执行字段。

迁移函数只接收经过 schema 校验的冻结纯数据，不接收数据库对象。函数返回完整、确定性的目标行和声明式诊断；宿主再次验证输出 schema、唯一键、行数、字节预算及安全字段后才允许写入。

## 持久账本

最小 schema 由宿主拥有：

1. `plugin_migration_registrations`
   - 不可变登记 exact migration contract 与 contract hash。
   - 同一 `migration_id + migration_code_sha256` 唯一。
2. `plugin_migration_events`
   - 只追加成功事件；保存 client request ID、规范请求语义、preview seal、from/to exact binding、输入数据集封印、输出聚合封印、前置 head、事件 hash 与时间。
   - 失败不得留下半个事件或“已迁移”标记。
3. `plugin_migration_heads`
   - 每个 `plugin_stable_id + namespace` 一个 CAS current head。
   - head 保存当前 schema version、最后 sequence/hash 和 exact plugin binding。
4. `plugin_schema_bindings`
   - 保存新绑定从哪一迁移事件产生，以及当前读路径应使用的精确 schema。
   - 历史 artifact/round/room 仍读取各自冻结 binding，不跟随 current head。

所有事件、registration 和 schema binding 禁止 UPDATE/DELETE。head 只能在提交事务内按期望 sequence/hash 做 CAS 更新。

## 预览、授权与提交

建议 API：

- `POST /api/plugin-lifecycle/migrations/preview`
- `POST /api/plugin-lifecycle/migrations`
- `GET /api/plugin-lifecycle/migrations/{event_id}`

预览只读且不持久化业务数据。请求必须绑定 exact source target、exact destination target、migration ID、当前 lifecycle head 和 schema head。响应只展示影响数量、被阻断的反向依赖、只读样例摘要、输入/输出封印和失效条件，不返回原始私有行。

提交体使用严格闭集：

```json
{
  "version": "plugin_migration_authorization_v1",
  "client_request_id": "...",
  "migration_id": "...",
  "expected_source_head_sequence": 3,
  "expected_source_head_sha256": "...",
  "preview_sha256": "...",
  "user_confirmed_exact_migration": true,
  "user_confirmed_no_automatic_cleanup": true
}
```

提交必须在同一 `BEGIN IMMEDIATE` 中：

1. 重验全局 lifecycle/registry 完整性、source/target exact registration 和 replacement 合同。
2. 确认命名空间已由宿主置于无并发写者状态；quarantined、tombstoned、损坏或未知状态全部阻断。
3. 重建输入数据集、规范请求语义和 preview；任何漂移都要求重新预览。
4. 在内存中运行宿主内置纯函数，并严格验证输出。
5. 以 copy-on-write 方式写目标 schema；旧不可变行保留。
6. 原子写 registration 引用、event、schema binding 和 CAS head。

任一步失败都回滚目标行、event、binding 和 head。不得留下部分版本、孤立目标行或漂移 head。

## 幂等与并发

- 同一 `client_request_id` 与同一规范语义返回原成功事件的冻结结果。
- 同一 ID 改语义返回 typed conflict，不重新运行 migration。
- 不同 ID 同时迁移同一 namespace 时，只允许一个匹配前置 head 的事务成功。
- 事件重放必须从已封印 request semantics 重建，不能用当前 catalog 或当前 implementation 改写历史结果。
- 读取时重算 registration、请求语义、输入、输出、事件链和 head；任一不一致只隐藏该插件 namespace 的迁移投影，不破坏核心房间和用户决定读取。

## 生命周期与历史语义

- `enabled + active` 可以预览，但提交前必须取得宿主的 namespace 写入静默证明。
- `disabled` 可读取历史和预览恢复路径，但不能绕过用户确认提交。
- `quarantined`、`tombstoned` 或 integrity-failed 一律禁止新迁移。
- `deprecated` source 只有在 replacement 指向 exact、兼容、已登记 target 时才可预览。
- replacement 永远不自动触发 migration；tombstone 永远不触发清理。
- legacy schema 无 seal 时继续只读标记 `legacy_unsealed`，不得因打开页面而回填或升级。

迁移状态只放在房间设置或插件管理面板。Artifact、Round Inspector 与用户最终决定区只显示当时冻结的 schema/binding 和只读完整性状态，不提供迁移按钮，也不因 current migration head 改写历史内容。

## 首个实施用例的验收门

P28 真正开始实现时，至少同时满足：

1. 有一个真实 plugin-owned schema v1 和可观察的 v2 用户价值，不以测试专用空表充数。
2. v1 历史和 hash 在 migration 功能加入前后逐字不漂移，legacy 不回填。
3. preview Provider=0、market=0、business writes=0。
4. unknown migration、contract/code/schema/hash/namespace/table 漂移全部在写入前失败关闭。
5. migration 函数拿不到 raw Store/SQLite/Provider/market/orchestrator；越权 mutation intent 为零写。
6. 输入 N 行必须产生同一冻结输入对象、确定性输出和一个 dataset seal；超预算整体失败。
7. 故障注入覆盖目标行中途、event 前、head CAS 前；每种情况都无部分目标、孤立 event 或漂移 head。
8. 同 ID 同语义精确幂等，同 ID 改语义冲突；两 Store 实例并发只有一个成功。
9. 绕过 immutable trigger 后篡改 registration、输入、输出、event、binding 或 head，整组迁移指标隐藏。
10. disabled/quarantined/tombstoned/replacement unavailable 状态矩阵正确，且不自动迁移或删除。
11. 用户最终决定、核心聊天、正式轮次与其他插件 namespace 前后完全不变。
12. 全程系统临时 SQLite、随机回环端口、正式 8770 离线、正式数据库/WAL 与 Provider 账本哈希前后不变。

满足这些验收后，才能讨论第二种迁移形态；在线插件商店、第三方动态迁移和卸载清理仍不在 P28 范围。
