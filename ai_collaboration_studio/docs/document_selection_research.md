# 正文选段加入研究房间

在「来源收件箱 → 官方正文证据」选择明确的版本，点「选择段落加入研究房间」，逐段勾选并选择房间，再点「预览选段与研究包范围」。预览显示原文/已选/未选段落数、选段长度、含引用包装长度和实际研究包摘录。先完成原有「已阅，不代表事实确认」，再勾选本次选段确认并保存。不会替用户记录已阅。

在目标房间打开「ChatGPT 协作」，点「预览实际研究资料」，展开资料核对完整摘录后才能冻结新任务包。该步只产生 BUNDLE_READY，不发送到 ChatGPT、不运行 Provider/API 审查或正式 round。旧「附加到房间」仍附加原事件，旧 round-draft 流程不变。

## 来源与保存

- 预览和保存仅消费数据库中已经存在的不可变正文版本，不抓官网或附件。未定位正文时拒绝。
- 客户端只提交事件路径、document_version_id、paragraph_ids、room_id、expected_state_version；保存另需 confirmation 和 preview_sha256。任意正文、URL、跨事件/跨版本/不存在的段落均不接受。
- 服务端校验事件、版本和来源，按原文顺序生成一份材料。source_url 进入现有结构化字段；metadata.document_selection（document_selection_v1）保留事件/版本/解析器、已选与未选段落 ID、原始/正文/选段哈希、读取时间、范围及警告。材料标为 unverified，不赋予事实确认或执行权限。
- 版本与关键引用、来源、哈希及未选/附件警告同时写入实际选段文本，现有 compact_room_bundle_v2 的 evidence_index.source_url / excerpt 可以直接携带。用户选旧版后不会因新版到达而替换。
- 材料 ID 由房间、正文版本和规范排序的段落 ID 确定；现有主键及 BEGIN IMMEDIATE 事务使并发/重发复用同一资料。材料、material_versions 与房间时间戳原子提交；失败一起回滚，不改事件或正文。
- 该来源身份不能从通用材料 API 伪造。选段材料不可改写标题、内容或身份；需要修改选段时重新选择。可通过现有 expected_version + active 操作停用/启用；重复加入不自动启用已停用材料。

## 长度与旧任务包

每份材料仍最多 1,600 字符，包含来源、段落引用、哈希和警告。超限预览明确显示实际纳入 0 字符，拒绝保存，要求重新选段。不会按字符切断段落、表格行或 URL，也不会自动拆成多份。完整证据仍留在正文版本库；未选段落 ID 在材料元数据中保留，实际摘录明确说明其余正文与所有附件未纳入。

新任务包冻结前检查现有投影的 40 份材料窗口与每份 1,600 字符边界，拒绝任何活跃材料被省略/截断的情况。界面预览的 evidence_sha256 随冻结请求提交；材料在预览后变化时须重新预览。直接调用旧创建 API 也不能绕过长度检查。资料整理仍受现有投影顺序影响；被排到窗口之外的活跃材料需要整理或改选研究房间，不能默认为已纳入。

旧 compact_room_bundle_v2 的字段、截取算法、冻结记录和哈希语义不批量改写；新增拒绝门仅用于新包创建，旧包按原有读取、完整性和上下文过期策略继续处理。旧长材料仍可保存和阅读，但新冻结必须先处理超限。

## 验证与兼容

定向验证覆盖实际 material → evidence_index 字符相等、尾部风险、超限、旧版本、非法 ID、并发/重发、事务回滚、凭据与确认门、通用材料 API 身份伪造及旧冻结包保持不变。前端覆盖无默认选段、完整预览、确认/超限门、旧房间迟到响应、切换房间后重新确认和新正文到达时保持旧版本。

新增接口：POST `/api/monitoring/events/{item}/document/selection/preview`、POST `/api/monitoring/events/{item}/document/selection`；GET `/api/rooms/{room}/chatgpt-collaborations/evidence-preview`。均复用现有本机请求保护；选段 POST 还要求当前数据库 owner。新冻结的可选 expected_evidence_sha256 不改变旧包导入合同。

没有新表、索引、迁移标记或依赖。已有 0c5a83b 代码能读取普通材料文本，但不认识新选段身份保护和新冻结拒绝门，不能声称具备相同保障；代码回退应使用对应的试用前数据快照，不能删 WAL/新表或把读取能力当成新功能验证。原正式库与既有采集入口不迁移。

本任务只复用已取得的历史 Micron 样本进行离线操作验收。SEC 仍未完成主 HTML 真实验收；自然新增公告、附件、长期无人值守、模型分析和正式发布不由此获得验证。最终测试数量、实机入口和 CI 以交付记录中的实际候选及终态为准。

### 2026-09-13 实际验收

运行代码提交 `8c3735e2a63a63feaa17996b129a2a816db3dae1`。Windows / Edge headless（现有 Playwright；Browser 插件未安装）在 1440×1000 和 390×844 中完成既有 Micron 历史样本的真实 UI 操作：全选超限明确拒绝；未阅时不能保存；通过原有按钮显式已阅后，选择不连续的第 1、5 段。原文 529 字符，含来源、引用及警告 1,448 字符，其余 14 段明确未选。重复确认返回已存在材料，没有第二份资料。

选段资料在目标房间可见，ChatGPT 协作中的实际资料预览与保存内容逐字相同；冻结后 evidence_index.excerpt / source_url 仍相同，状态 BUNDLE_READY。未对外发送、未调用 Provider 或正式轮次。控制台、页面脚本和外部请求记录均无异常；桌面和手机无页面横向溢出。

独立数据来自上轮已验证快照的副本。旧正文/任务/材料/材料版本/人工任务包及事件内容均保持，schema 无变化；仅副本中显式已阅、新建房间、一份选段材料和一份任务包。32 个原数据/入口/说明文件指纹保持不变。宿主正常 Ctrl+C 后记录 stopped 与 checkpoint，端口关闭、owner 可再次取得，WAL/SHM/journal 无残留。

本机后端 92 项定向回归通过；补充格式门和 HTTP 存储失败场景后，相关 12 项再次通过。前端 6 个文件共 62 项通过；补充所选版本不可用时拒绝替换后，相关 2 个文件共 13 项再次通过。生产构建与 7 项静态基线通过。首轮新测试有一个 HTTP 夹具缺少 request_with_headers 的错误，已补齐并保留失败日志；收尾辅助脚本一度未关闭自身只读连接而被 Windows 独占预检拒绝，关闭连接后正常通过。以上重复组不相加为独立覆盖。

关键命令（项目目录）：

```powershell
.\runtime\bootstrap\python\Scripts\python.exe -X utf8 -B scripts/run_backend_tests_isolated.py tests.test_document_selection tests.test_document_evidence tests.test_materials tests.test_manual_chatgpt tests.test_source_inbox_http --verbosity 1
npm.cmd --prefix frontend test -- tests/documentSelection.dom.test.js tests/documentEvidence.dom.test.js tests/manualChatGPTHistory.dom.test.js tests/manualChatGPT.test.js tests/sourceInbox.dom.test.js tests/materials.test.js
npm.cmd --prefix frontend run build
```

GitHub 完整 CI 仍须以最终推送提交的实际终态另行记录；不能由上述定向或浏览器结果推定。
