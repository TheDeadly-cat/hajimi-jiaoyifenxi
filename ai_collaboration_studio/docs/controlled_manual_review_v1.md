# 已导入 ChatGPT 结果的受控三项复核

2026-09-20，本阶段实现可执行的豆包复核链，复用 `ManualChatGPTService`、数据库所有权、正常启动检查和永久调用账本。没有重新执行导入或连通性试点，没有新增真实模型请求。此前的 [复核草案](manual_chatgpt_review_proposal_20260920.md) 保留为历史记录；实际调用须批准本入口在最终干净源码上生成的完整摘要。

## 已实现的边界

专用命令 `scripts/run_controlled_manual_review.py` 只接受已有 `ChatGPTTrial…/data/studio.sqlite3`，拒绝重解析路径，持有数据库 owner，使用正常 schema 检查及退出 checkpoint。准备和执行均不启动 HTTP 服务、来源轮询或行情连接；不修改正式库或迁移旧数据。

当前只开放豆包 `doubao-seed-2-1-pro-260915` 的原生 JSON Responses 路径，其他 Provider 禁用，OpenAI 硬禁用保持。方舟 GLM 在普通手工复核服务中的方法选择缺口仍明确不在此入口的可执行范围，不以失败后切换方法重试来兼容。

每个 standard 会话最多三次：事实核对、反方审查、风险审查。每次上限 1,400 输出 Token，总上限 4,200。三次都发送冻结证据和真实导入结果，各次不接收其他复核的答案；这是同一模型的三次不同请求，不是三家模型投票。

准备摘要绑定以下内容：

- 干净源码 SHA、独立数据库路径、房间和会话、bundle/context/result 哈希。
- 三份完整生成参数及 HTTP 请求体、逐份哈希和字节数、固定官方 URL、JSON 模式、关闭 thinking、`store=false`、240 秒网络超时参数。
- 明确的价格来源、核对日期、人民币单价、每次保守输入估算与预留金额、总消费计划。
- 最多 30 分钟的起止时间；摘要自身的 SHA-256。

执行前必须提供完全相符的 `--approve-plan-sha256`。缺少批准、摘要变化、结果变化、源码变化、时间无效或三次规划总金额过高均不进入付费执行。每次实际 HTTP 发送之前再次检查干净源码、时间和剩余消费计划，同时校验真实请求 URL、完整 body 哈希、字节上限与超时；禁止重定向和同一次调用内重发。

账本的 client request ID 绑定授权摘要哈希，同一会话仍只能有一个复核运行。重复点击、并发重复和进程丢失不会建立另一批收费调用；STARTED 预约不退款、不静默清零。发送前被拦截也保留对应预约，回执明确 `http_attempted=false`，不能把预约数当成成功网络请求数。

超时、截断、无效合同、错误返回模型、未知用量、超出输出/消费计划等立即停止后续任务；不增大额度、切换模型或自动补跑。原始返回、模型元数据、用量、消费估算及回执哈希存于独立副本的 `data/controlled-review-receipts/<session>/<plan>/`，与持久账本核对。缺失或漂移的回执不能被重复执行修复。密钥回显会被清除，回执记录拒绝原因。

消费计算按完整 HTTP UTF-8 字节数加 256 预留输入、固定输出上限、全部输入按未命中缓存价格计算。供应商返回的实际用量若比预留高，会在下一次发送前重新检查剩余预算；未知用量停止，费用保持未知。这是项目执行计划，不是供应商 tokenizer 或账单硬上限，免费额度和优惠不预先抵扣。

## 本机操作

先在独立手工副本外部配置文件中明确版本、`pilot_id`、候选 SHA、数据库、房间、会话、预期结果 SHA、模型、输出上限、请求字节上限、价格、金额及有效窗口；配置不包含密钥。具体字段由 `ControlledManualReview.prepare()` 严格校验。

```powershell
<managed-python> -X utf8 -B scripts/run_controlled_manual_review.py --config <config.json> --prepare --output <new-preview.json>
```

准备结果是待批准摘要，调用数为零。用户批准该摘要后才可以使用：

```powershell
<managed-python> -X utf8 -B scripts/run_controlled_manual_review.py --config <config.json> --execute --approve-plan-sha256 <approved-sha256> --password-dialog --output <new-result.json>
```

密钥只在本机圆点密码框输入。窗口显示最多 3 次及每次输出上限，取消不调用。网页里的普通复核按钮不替代这个受控入口，也不能用旧试点授权启动本次请求。执行后仅生成待处理的决定卡，用户确认对象保持空白；不会自动采纳、交易、合并或部署。

## 本阶段验证

修复回归涉及 artifacts、director budget、manual official evidence、provider preflight、round launch HTTP、round launch plan；受控执行及相邻合同涉及 controlled review、execution boundary、API pilot、manual ChatGPT、Ark GLM。11 个模块合并一次定向运行：**226 项通过，0 失败、0 错误、0 跳过，188.977 秒**。该组包含 21 项受控复核检查；不与此前重叠批次相加。

其中完整 CLI 测试在两个独立进程中通过正常 owner/schema/checkpoint 路径准备并执行合成数据的三项复核，拦截最低 HTTP transport，逐份核对实际发送体。源码身份检查在合成夹具中替换，真实材料准备阶段必须另用最终干净 checkout 实际核验，不能将夹具当成真实用户研究或真实调用。

拒绝检查覆盖未授权、计划变化、实际 HTTP 体及 URL 漂移、源码变化、请求间过期、源码核对过程中跨过到期时刻、发送前金额不足、输出超限、超时、截断、无效合同、返回模型错误、未知用量、重复执行、并发重复、进程丢失、回执缺失及密钥回显。正常复核的导入、分歧保留、决定冻结逻辑仍由原服务负责。

测试使用临时 SQLite 和模拟响应。网络审计为随机回环连接 114、外网阻断尝试 0、子进程阻断尝试 0；未触碰正式 8770 或 11111。完整远端回归及后续交付检查必须看最终 HEAD 的 Actions 终态；定向通过不是整体发布依据。

## 失败记录和价格核对

保留所有首轮记录，包括旧候选 CI 的 2,236 项 / 27 失败 / 3 错误、本机五类代表性失败、首次夹具修正结果、新增到期测试对 409 的错误预期，以及新 CLI 初始化顺序错误。测试修正没有削弱生产防篡改、预算或候选到期判断；没有把生产核验日期改成今天。

2026-09-20 07:41 北京时间，重新获取[方舟官方产品页](https://www.volcengine.com/product/ark)及该页直接引用的当前公开资源，核对 Seed 2.1 Pro 价格行：输入 CNY 6 / 百万 Token、输出 CNY 30 / 百万 Token。主页面 SHA-256 `7da68b9fa3985fc8a90df1ddd3660430b66c5b27b498d7a5d04e164a91b8b7aa`，引用资源 SHA-256 `1fa7e42f80b32cd059ede320c74a7694868b1a31c724e81044eacfa51eadbf2f`。此为公开价格依据，未核查账户账单、免费额度或余额。

本机证据位于父目录的 `controlled-review-closeout-20260920`，不上传完整材料、模型回复、数据库、日志、价格网页资源或密钥。真实会话继续等待新授权；本文件及旧预算都不是付费授权。
