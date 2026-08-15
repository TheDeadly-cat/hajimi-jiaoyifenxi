# 公司投资者关系新闻边界

## 固定来源

- `US.MU`：Micron Technology Investor Relations 新闻稿 RSS。
- `US.SNDK`：Sandisk Corporation Investor Relations 新闻稿 RSS。
- `US.WDC`：Western Digital Corporation Investor Relations 新闻稿 RSS。
- `US.STX`：Seagate Technology Investor Relations 新闻稿 RSS。

适配器只访问代码内固定的 HTTPS 端点和白名单官方域名。RSS 重定向、条目链接或用户请求一旦离开对应官方域名就被拒绝；不会把 Business Wire、GlobeNewswire、搜索结果或第三方镜像冒充公司一手来源。

## 数据质量

- 每家公司独立抓取、独立报错和独立缓存，一个来源失败不会阻断其他来源。
- 单个响应最大 1 MB；XML 无法解析、发布时间无效、发布时间晚于当前时点、官方链接缺失或重复条目均不会进入共同证据。
- `http` 条目链接只有在主机仍属于固定白名单时才升级为 `https`；URL 片段被移除。
- 保留标题、UTC 发布时间、发布日期、官方链接和最多 800 字符的纯文本摘要。HTML 仅作为不可信数据转成纯文本，不执行脚本或页面指令。
- RSS 未提供可靠事件类型时保持 `other`，不使用标题关键词伪造事件分类或情绪分数。

## 与 SEC 的关系

同一标的的 IR 新闻稿与 SEC 申报日期相差不超过一天时，只生成 `possible_sec_matches`。这是一条待核验线索，不是已确认重复项；两份证据都保留，直到用户或后续证据流程核对标题、事项和原文。

用户可在房间中把选中的 IR 新闻稿冻结为版本化资料。冻结请求必须再次命中当前官方 RSS 中的精确 URL，资料会明确写入“公司一手自述、不是独立核验”的边界；重复冻结同一 URL 返回原资料。只有完成这一步后，智能体才能用真实房间资料 ID 引用该新闻稿。

## 解释边界

公司 IR 新闻稿是一级来源，但属于公司自述。新闻与情绪角色必须把它与监管文件、第三方报道和反证分开，不得因为来源为一级就视为内容无误，也不得仅凭标题生成利好、利空、胜率或仓位。
