# 新闻来源与正文读取：机制复用记录

日期：2026-10-29。只迁移职责分工和设计思路，本次没有复制下列第三方项目代码，也没有新增框架依赖。

| 参考 | 本项目采用的机制 | 当前许可核对 |
|---|---|---|
| [RSSHub CNBC 路线](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/cnbc/rss.ts) | 先发现条目，再按正文链接读取；本项目再加来源许可、独立任务和保存版本 | 当前仓库 [LICENSE](https://github.com/DIYgod/RSSHub/blob/master/LICENSE) 为 AGPL-3.0，不能把旧有 MIT 印象当成本次复制许可；未复制代码 |
| [Scrapy 架构](https://docs.scrapy.org/en/latest/topics/architecture.html) | 发现、下载、解析和持久化分工；正文请求不挤进 RSS 发现循环 | [BSD 三条款](https://github.com/scrapy/scrapy/blob/master/LICENSE)；未复制代码，未引入 Scrapy |
| [NewsNow](https://github.com/newsnext/newsnow) | 聚合时保留来源和缓存状态；缓存命中与内容新鲜分开 | [MIT](https://github.com/newsnext/newsnow/blob/main/LICENSE)；未复制代码，不把默认抓取间隔解释成金融实时能力 |

## 本地复用

`ir_source_catalog.py` 从原 IR 适配器移动原有四家官方端点元数据，旧适配器仍导出 `IR_FEEDS`；新闻注册表无需加载行情服务或本机配置。STX 沿用已有 seagate 来源，不重复注册同一 RSS。新增 MU/SNDK/WDC IR 和四家 SEC 来源默认关闭，需要用户在候选界面明确启用。

SEC 条目沿用 `SecEdgarAdapter._normalize_recent_filings` 的表单及申报路径规范化，Ticker/CIK 来自固定官方映射。缺失带时区接收时间时保留日期和未知时刻。新闻请求与旧 SEC 研究适配器共享进程内请求锁及至少 0.11 秒间隔；多进程部署仍需要外部共享限速，不声称跨主机限速。

正文下载只接受注册来源 HTTPS 主机和受控 SEC 路径，重定向在发送下一次请求前检查同主机、协议和路径。请求有时间、字节及附件数量上限；页面脚本从未执行。已有来源冷却仍由 `NewsPlatform` 的同一来源状态管理，手动读取也不能绕过期限。

正文保留段落、表格行或 PDF 页定位及抽取版本。HTML 未确认正文容器、缺字或截断时为 `partial_text`；200 验证页不算正文。PDF 使用既有锁定 pypdf 依赖，最多 80 页，仅提取文字，明确为 `partial_text`，不宣称 OCR、图像或表格布局已核验。附件单独标注，最多读取三个；未允许主机的附件显示受限，未读取材料不会进入证据包。HTTP 410 仅说明资源不可得，不自动认定公告撤回。

## 当前范围和未证明事项

注册表逐源给出覆盖、时间语义、访问约束和历史能力。RSS 仅覆盖当前窗口；当前候选的 `news_sec_history.advance_history` 已接入有界归档分页、持久游标和周期重查，最近申报与归档页均按原始时间处理。真实断线回补、档案完整性及连续覆盖仍未确认。MU 新闻入口已改为固定季度目录快照，不能据此称旧 RSS 恢复。正文读取实现和离线示例不是源站当前可用、真实模型质量、部署或长期运行证明。真实来源访问、更多附件主机与历史回补需单独取得证据。
