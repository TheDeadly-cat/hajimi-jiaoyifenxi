# 公司投资者关系新闻边界

## 固定来源

- `US.MU`：Micron Technology Investor Relations 新闻稿 RSS。
- `US.SNDK`：Sandisk Corporation Investor Relations 新闻稿 RSS。
- `US.WDC`：Western Digital Corporation Investor Relations 新闻稿 RSS。
- `US.STX`：Seagate Technology Investor Relations 新闻稿 RSS。

适配器只访问代码内固定的 HTTPS 端点和白名单官方域名。RSS 重定向、条目链接或用户请求一旦离开对应官方域名就被拒绝；不会把 Business Wire、GlobeNewswire、搜索结果或第三方镜像冒充公司一手来源。

## 数据质量

- 每家公司独立抓取、独立报错和独立缓存，一个来源失败不会阻断其他来源。
- 单个响应最大 1 MB；XML 无法解析、发布时间无效、发布时间晚于当前时点或官方链接缺失的条目不会进入共同证据。原有按需读取接口继续按标题与链接去重；持续监控使用独立的未预去重视图，把全部规范化条目交给身份/投影冲突检查。
- `http` 条目链接只有在主机仍属于固定白名单时才升级为 `https`；URL 片段被移除。
- 保留标题、UTC 发布时间、发布日期、官方链接和最多 800 字符的纯文本摘要。HTML 仅作为不可信数据转成纯文本，不执行脚本或页面指令。
- 事件类型只使用版本固定的标题/摘要模式确定性分类为财报日程、财报结果、演示材料或 `other`；分类是路由元数据，不是事实核验、情绪分数或交易影响结论。模式无法可靠命中时保持 `other`。

## 与 SEC 的关系

同一标的的 IR 新闻稿与 SEC 申报日期相差不超过一天时，只生成 `possible_sec_matches`。这是一条待核验线索，不是已确认重复项；两份证据都保留，直到用户或后续证据流程核对标题、事项和原文。

用户可在房间中把选中的 IR 新闻稿冻结为版本化资料。冻结请求必须再次命中当前官方 RSS 中的精确 URL，资料会明确写入“公司一手自述、不是独立核验”的边界；重复冻结同一 URL 返回原资料。只有完成这一步后，智能体才能用真实房间资料 ID 引用该新闻稿。

## 解释边界

公司 IR 新闻稿是一级来源，但属于公司自述。新闻与情绪角色必须把它与监管文件、第三方报道和反证分开，不得因为来源为一级就视为内容无误，也不得仅凭标题生成利好、利空、胜率或仓位。

## 持续监控投影与修订

`source_monitoring.adapters.company_ir` 只注册上述四个代码内固定 feed，不能接收用户提供的 RSS URL，也不能动态放宽域名。身份优先使用 RSS GUID；缺少 GUID 时才使用规范化后的官方条目 URL。checkpoint 保存身份哈希及其最近一次 RSS 投影哈希：投影未变只记录 duplicate，同一身份的标题、摘要、发布时间或 URL 变化会生成一条明确标记 `is_revision=true` 的新 Source Inbox 事件。

如果同一次 RSS 响应中出现同一身份但互相冲突的两个投影，即使标题和链接相同、只有摘要不同，监控器也拒绝该身份的全部候选并增加 rejected 计数；Supervisor 将该 run 记为 `DEGRADED` 且不推进 checkpoint。这样即使 feed 下次倒序，也不会把同一批内的矛盾内容静默解释成两次时间有序修订。

RSS 投影哈希只覆盖规范化条目字段，不代表链接网页正文哈希。事件的官方网页来源保持空 `content_sha256`，另一个 feed 来源记录投影哈希，并在 `company_ir_v1.rss_hash_semantics` 明示 `normalized_rss_item_not_web_page_body`。Supervisor 只有在现有 Source Inbox 导入成功后才推进 checkpoint；Provider、市场和正式 round 调用均为零。

当前 checkpoint 可完整保存 250 个身份及其投影哈希。单次有效 RSS 窗口超过 250 个唯一身份时，适配器显式返回 `COMPANY_IR_CHECKPOINT_CAPACITY_EXCEEDED`，零导入并保持 started checkpoint；不会截断投影表后循环重放旧身份。更大窗口需要后续版本化水位/回填设计。
