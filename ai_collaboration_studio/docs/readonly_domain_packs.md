# 版本化只读领域能力包

宿主通过 versioned capability pack、domain adapter、host-owned UI contribution 和通用 round-context provider 接入领域能力。通用 orchestrator 只负责聚合、哈希、持久化与恢复，不包含足球或股票字段，也不把领域逻辑并入 `TradingAgents`。

## Football research v1

- 能力包：`football_research_readonly`
- 端口：`core.football.match_context/v1`
- 合同：`football_research_contract_v1`
- 只读检查：`POST /api/rooms/{room_id}/football-research/inspect`

合同封印联赛、赛季、比赛 ID、开球 UTC、场地、双方精确历史赛程、7/14 日密度窗口、旅行、主客场序列、阵容、伤停、停赛、战术、近期表现和统一数据截止时间。每条证据只允许 `official_fact`、`media_report`、`model_inference`、`odds_proxy` 四类之一，并绑定精确材料版本、内容哈希、快照哈希、发布时间或推断依赖。

赔率仅是带抓取时间的代理证据。v1 固定 `probability_state=withheld_no_calibration`、`future_probability_available=false`、`probability_metrics_visible=false` 和 `odds_are_proxy_only=true`；没有独立、真实、足球任务匹配的校准能力包时，不生成未来胜率或概率指标。

## Stock research v1

- 能力包：`stock_research_readonly`
- 端口：`core.market.readonly_context/v1`
- 合同：`stock_research_contract_v1`
- 房间股票池：`stock_room_scope_v1`
- 只读检查：`POST /api/rooms/{room_id}/stock-research/inspect`

股票房间必须由用户显式保存 1–64 个规范 `MARKET:TICKER` 标的。合同逐标的、逐项封印 Futu、SEC、公司 IR、复权和公司行动五项预检，以及统一数据截止时间。每项要么绑定精确本地材料版本与哈希并标为 ready，要么明确 unavailable；服务不会在检查或正式轮准备时访问真实 Futu、SEC、IR 或其他网络来源。

该包复用宿主证据图、治理、行动台和用户决定链，但不声明 `simulation.paper_portfolio`、存储候选产物 contribution 或 candidate experiment action。`storage_research_readonly` v1 的合同和专用四股样板保持不变；通用股票包不会扩写或替代它。

## 正式轮授权与恢复

检查成功只生成预览，不会自动用于下一轮。用户必须在宿主 UI 中明确选择“用于下一轮”；前端提交 `round_context_authorizations`，启动计划把精确合同 SHA、材料/股票池封印、registry snapshot 和授权一起纳入 `round_launch_plan_v5` 哈希。

服务端在 Provider 预检或调用前，于同一个 SQLite 只读事务中重新验证能力包、adapter、port、UI contribution、材料版本和领域合同，随后持久化通用 `round_context_prepared_set_v1`。暂停恢复只读取冻结记录并重算完整性，不回读可变材料或股票池。缺少授权、版本漂移、材料漂移、集合多出或缺失、哈希损坏都会在 Provider、行情或网络调用之前失败关闭。

两个能力包都不执行投注或交易，不连接钱包/账户，不创建订单，不自动下注，也不替代用户决定。冻结产物只要包含足球或通用股票能力包，即使房间之后移除该包，也不能进入存储股票 candidate experiment。

## 隔离验证

```powershell
python scripts\run_backend_tests_isolated.py --layer domains --verbosity 2
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run build
```

后端入口始终使用系统临时 runtime 和显式临时 SQLite；分层说明见 [`backend_test_layers.md`](backend_test_layers.md)。
