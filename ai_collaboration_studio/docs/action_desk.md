# 会议行动台 v1

## 目的

会议产物中的待办已经包含稳定 ID、负责人、期限、状态和证据，但确认后不应为了推进状态而反复改写产物版本。行动台把“经用户确认的会议记录”和“持续变化的执行进度”分成两条可追溯记录：

- 产物继续保存当时确认的事实、证据和待办原文；
- 行动台只保存用户明确采纳后的负责人、期限、状态和备注变化；
- 两者通过精确 `artifact_id + artifact_version + action_id + action_snapshot_sha256` 绑定。

行动台是通用共创内核的一部分，不属于交易执行能力，也不会让 AI 代替用户创建、指派、完成或外发任务。

## 用户流程

1. 系统只读列出每个产物最新的、封印完整的 `CONFIRMED` 精确版本中的待办。
2. 候选默认未采纳。用户核对原文、来源版本、负责人和期限后，点击“采纳到行动台”。
3. 采纳后，用户可更新负责人、期限、状态和备注；每次更新都追加事件并使用预期 revision 防止并发覆盖。
4. 产物产生新确认版本时，旧行动项继续指向旧精确版本，不自动迁移。新版本中的同 ID 待办仍需用户重新选择是否采纳。
5. 行动台状态不修改产物内容，不创建或修改 `artifact_user_decision_v2`，也不自动启动下一轮讨论。

## 数据与事务边界

- `artifact_action_events` 保存追加式事件、客户端请求 ID、请求语义哈希、前序事件哈希和事件哈希。
- `artifact_action_heads` 只保存每个精确来源行动的当前链头和 revision。
- `artifact_action_anchors` 与 `artifact_action_anchor_heads` 形成独立交叉锚：逐次绑定请求语义、事件 SHA、该 revision 的行动链头 SHA、计数和前一锚；只重写并重封事件与行动链头仍会失败关闭。
- 采纳或更新在一个 `BEGIN IMMEDIATE` 事务内再次核验：房间、产物版本封印、`CONFIRMED` 状态、待办快照、预期 revision、客户端请求语义、事件链、行动链头与独立锚链，并原子写入四张表。
- 同一客户端请求 ID、同一语义返回原事件；同一 ID 改变语义时冲突关闭。
- 任一来源、事件链、行动链头或独立锚完整性失败时，不返回该行动的可编辑字段；孤立事件或孤立锚仍占用精确来源，不会退回未封印数据或把它重新列为候选。

## API v1

- `GET /api/rooms/{room_id}/action-desk`
  - 返回未采纳候选、已采纳行动、状态计数、完整性状态和固定安全边界。
- `POST /api/rooms/{room_id}/action-desk/transitions`
  - `transition=adopt`：显式采纳精确来源行动。
  - `transition=update`：以 `expected_revision` 更新负责人、期限、状态或备注。
- `GET /api/action-desk/overview`
  - 在一个共同 SQLite 读事务中重算所有房间的行动台完整性，只汇总已采纳行动，不返回候选详情，也不接受客户端来源字段。
  - 健康房间可继续显示；任一失败房间整组隐藏行动内容和状态计数，只保留房间身份与复核提示。全局孤立谱系会显式降低总览完整性，不会被投影成行动项。
  - v1 不接受查询参数、不分页或静默截断；搜索和状态筛选只在已验证的完整本地投影上进行。
  - 为保持完整投影边界，超过 v1 的房间/单房行动/总行动上限时服务端返回 `ACTION_DESK_OVERVIEW_LIMIT_EXCEEDED`，不会返回部分结果；分页属于后续版本合同。

## 跨房间行动总览

总览是只读工作区视图，不是第二套行动写入接口。它提供状态、负责人、精确产物版本和所在房间的统一浏览，并允许用户返回对应房间的行动台继续处理。总览不能采纳、批量更新、自动分配、启动讨论或调用外部任务系统；活动时间排序也不表示优先级、排名或系统建议。

## 旧版行动到新版行动的显式延续

当同一产物产生更高的 `CONFIRMED` 精确版本时，旧行动不会静默迁移。用户可以在房间行动台中逐项选择新版确认待办，并点击“确认建立延续关系”。服务端只接受同一 `artifact_id`、更高版本、两端行动快照哈希和旧行动当前 revision；新版行动仍是未采纳候选。

关系绑定的是建立时的旧 revision；旧行动之后仍可追加自己的进度事件，但这些事件不会转移到新版行动，也不会改写关系的来源封印。

- `GET /api/rooms/{room_id}/action-desk/continuations` 只读重算延续关系；关系损坏时隐藏全部关系，但不改写旧行动台状态。
- `POST /api/rooms/{room_id}/action-desk/continuations` 使用独立 `artifact_action_continuation_v1` 请求、客户端请求 ID、语义哈希和四表追加式关系封印；同 ID 同语义返回相同关系，同 ID 改语义冲突。
- 关系只记录“旧精确行动 → 新精确行动”的用户确认谱系，不复制负责人、期限、状态或备注，不自动采纳新版、不自动开启讨论、不创建或修改 `artifact_user_decision_v2`。
- 关系提交失败时事件、关系头、独立锚和锚头全部回滚；篡改任一关系输入或封印只隐藏关系，不能被当前产物或相邻行动静默替代。

## 固定安全声明

```text
execution_capability=none
external_write=false
can_autonomously_decide=false
can_replace_user_decision=false
user_final_decision_required=true
```

跨房间总览另外固定返回 `ranking_produced=false` 与 `winner_claim=false`；按更新时间保持稳定展示只用于定位，不表示优先级或推荐。

v1 不包含录音、转录、提醒、周期任务、外部 Planner/Slack/Linear 写入、账户动作、交易、投注、支付或任意插件代码执行。

## 后续而非本阶段

- 将行动文字预填到下一轮目标，但不自动授权或启动；
- 当真实数据模型出现 v1 到 v2 的升级需求时，再以 `plugin_migration_ledger_plan.md` 的 preview / commit / CAS 合同实现迁移账本。
