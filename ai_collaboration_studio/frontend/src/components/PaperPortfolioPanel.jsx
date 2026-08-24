import { AlertTriangle, BarChart3, CheckCircle2, GitBranch, History, Pencil, Plus, RefreshCw, ShieldCheck, Unlink, X } from "lucide-react";
import { memo, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  WALK_FORWARD_LOCKED_SCENARIOS,
  WALK_FORWARD_SCENARIO_SET_VERSION,
  walkForwardScenarioView,
} from "../walkForwardScenarioView";
import { buildPortfolioLineageIndex, decisionPackageSource } from "../decisionLineageView";
import { CandidateComparisonPanel } from "./CandidateComparisonPanel";
import { useModalFocus } from "../useModalFocus";
import "../styles/paper-portfolio.css";


const SYMBOLS = ["US.MU", "US.SNDK", "US.WDC", "US.STX"];
const CANDIDATE_SIMULATION_CONFIRMATION_VERSION = "candidate_simulation_confirmation_v1";
const CANDIDATE_SIMULATION_RULE_ID = "fixed_candidate_direction_replay_v1";
const SIDE_OPTIONS = [
  ["FLAT", "观望"],
  ["LONG", "模拟做多"],
  ["SHORT", "模拟做空"],
];
const BUDGET_FIELDS = [
  ["max_gross_exposure_pct", "总敞口上限", 100],
  ["max_net_exposure_pct", "净敞口上限", 100],
  ["max_single_name_pct", "单一标的上限", 35],
  ["max_annualized_volatility_pct", "年化波动上限", 40],
  ["max_historical_var_95_1d_pct", "单日 VaR 上限", 4],
  ["max_drawdown_pct", "最大回撤上限", 30],
  ["max_worst_5d_loss_pct", "最差 5 日损失上限", 15],
  ["max_stress_loss_pct", "压力损失上限", 15],
];
const DEFAULT_BUDGETS = Object.freeze(
  Object.fromEntries(BUDGET_FIELDS.map(([field, _label, fallback]) => [field, fallback])),
);
const DEFAULT_SCENARIOS = [
  {
    id: "broad_storage_selloff",
    name: "存储板块同步回撤",
    shocks: Object.fromEntries(SYMBOLS.map((symbol) => [symbol, -10])),
  },
  {
    id: "memory_price_downcycle",
    name: "DRAM / NAND 下行周期",
    shocks: { "US.MU": -18, "US.SNDK": -18, "US.WDC": -8, "US.STX": -8 },
  },
  {
    id: "hdd_demand_shock",
    name: "HDD 需求冲击",
    shocks: { "US.MU": -7, "US.SNDK": -7, "US.WDC": -18, "US.STX": -18 },
  },
];
const DEFAULT_WALK_FORWARD_CONFIG = Object.freeze({
  train_days: 99,
  test_days: 20,
  step_days: 20,
});
const WALK_FORWARD_CONFIG_FIELDS = [
  ["train_days", "训练窗口", 2, 1000, 1, "日"],
  ["test_days", "测试窗口", 1, 260, 1, "日"],
  ["step_days", "步进", 1, 260, 1, "日"],
];
const USD_FORMATTER = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});
const EMPTY_LIST = Object.freeze([]);


function blankPlan() {
  return {
    name: "存储产业模拟组合",
    positions: SYMBOLS.map((symbol) => ({
      symbol,
      side: "FLAT",
      weight_pct: 0,
      thesis: "",
      invalidation: "",
    })),
    budgets: { ...DEFAULT_BUDGETS },
    stress_scenarios: DEFAULT_SCENARIOS.map((scenario) => ({
      ...scenario,
      shocks: { ...scenario.shocks },
    })),
  };
}


function portfolioPlan(portfolio) {
  if (!portfolio?.id) {
    const seed = portfolio?.lineage_source?.candidate_simulation_seed;
    if (seed?.applicable === true && seed?.ready === true) {
      const plan = blankPlan();
      return {
        ...plan,
        name: `${String(seed.candidate_snapshot?.title || "候选方案")} · 模拟映射`,
        positions: plan.positions.map((position) => (
          position.symbol === seed.symbol
            ? {
              ...position,
              side: seed.target_side,
              weight_pct: 25,
              thesis: String(seed.thesis || ""),
              invalidation: String(seed.invalidation || ""),
            }
            : position
        )),
      };
    }
    return blankPlan();
  }
  return {
    name: portfolio.name || "存储产业模拟组合",
    positions: SYMBOLS.map((symbol) => {
      const position = (portfolio.positions || []).find((item) => item.symbol === symbol);
      return {
        symbol,
        side: position?.side || "FLAT",
        weight_pct: position?.weight_pct ?? 0,
        thesis: position?.thesis || "",
        invalidation: position?.invalidation || "",
      };
    }),
    budgets: {
      ...DEFAULT_BUDGETS,
      ...(portfolio.budgets || {}),
    },
    stress_scenarios: (portfolio.stress_scenarios?.length
      ? portfolio.stress_scenarios
      : DEFAULT_SCENARIOS
    ).map((scenario) => ({
      id: scenario.id,
      name: scenario.name,
      shocks: Object.fromEntries(SYMBOLS.map((symbol) => [symbol, scenario.shocks?.[symbol] ?? 0])),
    })),
  };
}


function finiteNumber(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}


function percent(value, digits = 1) {
  const number = finiteNumber(value);
  return number === null ? "—" : `${number.toFixed(digits)}%`;
}


function sideLabel(value) {
  return SIDE_OPTIONS.find(([side]) => side === value)?.[1] || value;
}

function ratioPercent(value) {
  const number = finiteNumber(value);
  return number === null ? "—" : percent(number * 100);
}


function walkForwardConfigValid(config) {
  return WALK_FORWARD_CONFIG_FIELDS.every(([field, _label, minimum, maximum]) => {
    const value = finiteNumber(config[field]);
    return value !== null && Number.isInteger(value) && value >= minimum && value <= maximum;
  });
}


function usd(value) {
  const number = finiteNumber(value);
  return number === null ? "—" : USD_FORMATTER.format(number);
}


function capacityGapValue(value) {
  if (!value || typeof value !== "object") return finiteNumber(value);
  return finiteNumber(
    value.amount_usd
    ?? value.capacity_gap_usd
    ?? value.capacity_shortfall_usd
    ?? value.value_usd,
  );
}


function compactObject(value, limit = 180) {
  if (!value || typeof value !== "object" || !Object.keys(value).length) return "未返回";
  try {
    const text = JSON.stringify(value);
    return text.length > limit ? `${text.slice(0, limit)}…` : text;
  } catch (_error) {
    return "无法显示";
  }
}


function boundedText(value, fallback = "", limit = 1000) {
  const text = typeof value === "string"
    ? value.trim()
    : value instanceof Error
      ? value.message.trim()
      : "";
  return (text || fallback).slice(0, limit);
}


function shortHash(value) {
  const text = typeof value === "string" ? value.trim() : "";
  return text.length >= 16 ? `${text.slice(0, 8)}…${text.slice(-8)}` : text || "未返回";
}


function blockerSummary(value) {
  if (!value || typeof value !== "object") return "未返回可定位的阻断证据";
  const parts = [
    value.symbol,
    value.phase || value.execution_phase,
    value.fold_id,
    value.reason_code,
  ].filter((item) => typeof item === "string" && item.trim());
  return parts.length ? parts.join(" · ") : "已记录容量阻断证据";
}


function generatedPositionLabel(position) {
  if (!position || typeof position !== "object") return "";
  const symbol = String(position.symbol || "").replace("US.", "");
  const side = String(position.side || position.direction || "").toUpperCase();
  const weight = finiteNumber(position.weight_pct ?? position.weight);
  return [symbol, side, weight === null ? "" : `${weight.toFixed(1)}%`].filter(Boolean).join(" ");
}


function walkForwardScenarioStateLabel(row) {
  if (row.blocked) return "容量阻断";
  if (!row.available) return "未返回";
  if (row.state === "sufficient") return "窗口数达门槛";
  if (row.state === "insufficient") return "窗口数不足";
  return row.state || "待确认";
}


function LockedFrictionScenarios() {
  return (
    <aside className="walk-forward-locked-model" aria-label="服务端锁定三档纸面摩擦假设">
      <header>
        <span><strong>服务端锁定三档摩擦</strong><small>不可在前端改写</small></span>
        <em>{WALK_FORWARD_SCENARIO_SET_VERSION.replace("storage_friction_", "")}</em>
      </header>
      <div className="walk-forward-locked-scenarios">
        {WALK_FORWARD_LOCKED_SCENARIOS.map(({ id, label, assumptions }) => (
          <article key={id}>
            <strong>{label}</strong>
            <span>纸面参考规模 {usd(assumptions.paper_reference_notional_usd)}</span>
            <small>
              佣金 {assumptions.commission_bps_per_side} bps/边 · 滑点 {assumptions.entry_slippage_bps}/{assumptions.exit_slippage_bps} bps
            </small>
            <small>
              借券费 {assumptions.short_borrow_fee_bps_annual} bps/年 · 日成交额参与上限 {assumptions.max_daily_turnover_participation_pct}%
            </small>
          </article>
        ))}
      </div>
      <p>
        “纸面参考规模”仅用于容量压力测试，不是账户资金；成交额与容量都是历史代理估算，不代表真实成交、券源或可下单数量。
      </p>
    </aside>
  );
}


function walkForwardSourceLabel(result) {
  if (result?.source === "futu_qfq_daily_history") return "富途 QFQ 日线";
  return result?.source || "历史数据待确认";
}


function timestampValue(value) {
  if (value === null || value === undefined || value === "") return "";
  const numericValue = Number(value);
  if (Number.isFinite(numericValue)) {
    if (numericValue <= 0) return null;
    return numericValue < 1_000_000_000_000 ? numericValue * 1000 : numericValue;
  }
  const parsed = Date.parse(String(value));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}


function readableTimestamp(value) {
  const timestamp = timestampValue(value);
  if (timestamp === null || timestamp === "") return "";
  const date = new Date(timestamp);
  return date.toLocaleString("zh-CN", { hour12: false });
}


function WalkForwardScenarioResults({ rows, positiveRateLabel = "历史测试正收益窗口比例（非未来胜率）" }) {
  return (
    <div className="walk-forward-scenario-results" aria-label="三档纸面摩擦场景结果">
      {rows.map((row, index) => {
        const gap = capacityGapValue(row.capacityGap);
        return (
          <article
            className={`walk-forward-scenario-result${row.blocked ? " blocked" : ""}${row.available ? "" : " unavailable"}`}
            key={JSON.stringify([row.id, row.label, index])}
          >
            <header>
              <strong>{row.label}</strong>
              <em>{walkForwardScenarioStateLabel(row)}</em>
            </header>
            {row.blocked ? (
              <div className="walk-forward-capacity-gap">
                <span>容量缺口</span>
                <strong>{gap === null ? "无法量化" : usd(gap)}</strong>
                <small>{blockerSummary(row.firstBlocker)}</small>
                <small>该场景整体阻断；不展示收益、正窗口率或回撤。</small>
              </div>
            ) : row.available && row.metricsVisible ? (
              <dl>
                <div><dt>组合窗口复利</dt><dd>{percent(row.portfolioCumulativeReturnPct)}</dd></div>
                <div><dt>{positiveRateLabel}</dt><dd>{ratioPercent(row.historicalPositiveFoldRatio)}</dd></div>
                <div><dt>最差窗口回撤</dt><dd>{percent(row.maxDrawdownPct)}</dd></div>
              </dl>
            ) : (
              <p>未返回该场景的完整可审计结果，指标保持隐藏。</p>
            )}
          </article>
        );
      })}
    </div>
  );
}


function WalkForwardMethodCard({ view }) {
  const method = view.evaluation || {};
  const contract = view.strategyContract;
  return (
    <section className={`walk-forward-method-card ${method.kind || "invalid"}`} aria-label="历史回放方法与规则合同">
      <header>
        <span><strong>{method.label || "评估语义待验证"}</strong><small>{method.kind === "prospective" ? "训练窗专属规则" : "固定方案历史追溯"}</small></span>
        {view.ruleContractVisible && contract ? <em>{contract.version}</em> : null}
      </header>
      {view.ruleContractVisible && contract ? (
        <dl>
          <div><dt>规则</dt><dd title={contract.ruleId}>{contract.ruleId}</dd></div>
          <div><dt>信号 / 排序</dt><dd>{contract.signal || "未返回"} · {contract.ranking || "未返回"}</dd></div>
          <div><dt>拟合范围</dt><dd>{contract.fitScope || "未返回"} · 测试数据不参与拟合</dd></div>
          <div><dt>多 / 空数量</dt><dd>{contract.longCount ?? "—"} / {contract.shortCount ?? "—"}</dd></div>
          <div><dt>多 / 空预算</dt><dd>{percent(contract.longBudgetPct)} / {percent(contract.shortBudgetPct)}</dd></div>
          <div><dt>权重 / 调仓</dt><dd>{contract.weighting || "未返回"} · {contract.rebalance || "未返回"}</dd></div>
          <div><dt>标的范围</dt><dd>{(Array.isArray(contract.universe) ? contract.universe : []).map((symbol) => symbol.replace("US.", "")).join(" / ") || "未返回"}</dd></div>
          <div><dt>合同哈希</dt><dd title={contract.sha256}>{shortHash(contract.sha256)}</dd></div>
        </dl>
      ) : null}
      <p>{method.detail}</p>
    </section>
  );
}


function WalkForwardGates({ view }) {
  return (
    <div className="walk-forward-gates" aria-label="历史测试数据门和容量门">
      <section className={view.dataGate.ready ? "ready" : "blocked"}>
        <span>数据门</span>
        <strong>{view.dataGate.label}</strong>
        <small>{view.dataGate.detail}</small>
      </section>
      <section className={view.capacityGate.ready ? "ready" : "blocked"}>
        <span>容量门</span>
        <strong>{view.capacityGate.label}</strong>
        <small>{view.capacityGate.detail}</small>
      </section>
    </div>
  );
}


function WalkForwardFoldAudit({ rows }) {
  if (!rows.length) return null;
  return (
    <details className="walk-forward-fold-audit">
      <summary>逐折审计 · {rows.length} 个非重叠历史测试窗口</summary>
      <div className="walk-forward-fold-list">
        {rows.map((fold, foldIndex) => {
          const decision = fold.strategyDecision || {};
          const positions = (decision.generatedPositions || [])
            .map(generatedPositionLabel)
            .filter(Boolean);
          return (
            <article key={JSON.stringify([fold.id, fold.index, fold.testStart, foldIndex])}>
              <header>
                <strong>{fold.id || `fold_${fold.index || "?"}`}</strong>
                <small>决策截止 {fold.decisionCutoff || "未返回"} · 纸面入场 {fold.scheduledEntryDate || "未返回"}</small>
              </header>
              <div className="walk-forward-fold-timeline">
                <span><small>训练窗</small><strong>{fold.trainStart || "—"} → {fold.trainEnd || "—"}</strong></span>
                <span><small>历史测试窗</small><strong>{fold.testStart || "—"} → {fold.testEnd || "—"}</strong></span>
              </div>
              <dl className="walk-forward-fold-decision">
                <div><dt>规则参数</dt><dd title={compactObject(decision.selectedParameters, 1000)}>{Object.keys(decision.selectedParameters || {}).length ? compactObject(decision.selectedParameters) : "服务端固定，无逐折调参"}</dd></div>
                <div><dt>训练证据</dt><dd title={compactObject(decision.trainSelectionEvidence, 1000)}>{compactObject(decision.trainSelectionEvidence)}</dd></div>
                <div><dt>生成纸面仓位</dt><dd>{positions.join(" · ") || "未返回"}</dd></div>
                <div><dt>决策输入哈希</dt><dd title={decision.decisionInputHash}>{shortHash(decision.decisionInputHash)}</dd></div>
              </dl>
              <div className="walk-forward-fold-scenarios" aria-label={`${fold.id} 三档摩擦状态`}>
                {fold.scenarios.map((scenario, scenarioIndex) => (
                  <span className={scenario.state} key={JSON.stringify([scenario.id, scenario.name, scenarioIndex])} title={blockerSummary(scenario.blocker)}>
                    <strong>{scenario.name}</strong><small>{scenario.label}</small>
                  </span>
                ))}
              </div>
            </article>
          );
        })}
      </div>
    </details>
  );
}


function WalkForwardSummary({ run, actionableNow = true }) {
  const result = run?.result || {};
  const summary = result.summary || {};
  const scenarioView = walkForwardScenarioView(run);
  const integrity = scenarioView.integrity;
  const resultVersion = scenarioView.resultVersion
    || result.version
    || run?.result_version
    || "walk_forward_result";
  const isV3 = resultVersion === "walk_forward_result_v3";
  const isV4 = resultVersion === "walk_forward_result_v4";
  const nonOverlappingFoldCount = finiteNumber(summary.non_overlapping_test_fold_count)
    ?? finiteNumber(summary.independent_fold_count)
    ?? 0;
  const configuredMinimumFoldCount = finiteNumber(summary.minimum_non_overlapping_test_folds);
  const minimumFoldCount = Number.isInteger(configuredMinimumFoldCount)
    && configuredMinimumFoldCount > 0
    ? configuredMinimumFoldCount
    : 20;
  const adequacyStatus = summary.adequacy_status || summary.status || result.state || "insufficient";
  const semanticsReady = scenarioView.evaluation?.ready === true;
  const safetyBoundaryReady = result.execution_capability === "none"
    && result.live_trading_allowed === false
    && result.provider_calls_total === 0
    && result.openai_calls === 0;
  const displayReady = scenarioView.metricsVisible && semanticsReady && safetyBoundaryReady;
  const scenarioSetComplete = !(isV3 || isV4) || scenarioView.rows.every((row) => row.available);
  const hasBlockedScenario = (isV3 || isV4)
    && displayReady
    && scenarioView.rows.some((row) => row.blocked);
  const sufficient = displayReady
    && scenarioSetComplete
    && !hasBlockedScenario
    && adequacyStatus === "sufficient"
    && nonOverlappingFoldCount >= minimumFoldCount;
  const insufficient = displayReady && !sufficient;
  const historicalPositiveFoldRatio = summary.historical_positive_fold_ratio
    ?? summary.positive_fold_rate;
  const portfolioCumulativeReturn = displayReady
    ? finiteNumber(summary.portfolio_cumulative_return_pct)
    : null;
  const benchmarkCumulativeReturn = displayReady
    ? finiteNumber(summary.benchmark_cumulative_return_pct)
    : null;
  const createdAt = readableTimestamp(run?.created_at);
  const currentlyActionable = actionableNow !== false && scenarioView.actionableNow !== false;
  const statusLabel = !scenarioView.metricsVisible
    ? integrity.label
    : !displayReady
      ? "记录不可用"
      : hasBlockedScenario
        ? isV4 ? scenarioView.capacityGate.label : "容量约束阻断"
        : !scenarioSetComplete
          ? "场景结果不完整"
          : insufficient
            ? "insufficient"
            : "窗口数达最低门槛";

  return (
    <section className="walk-forward-result" aria-label="固定纸面组合历史滚动回放结果">
      <header>
        <span>
          <strong>{isV4 ? "逐折规则历史 walk-forward" : "固定纸面组合历史滚动回放"}</strong>
          <small>
            {createdAt || "时间待确认"}
            {scenarioView.metricsVisible
              ? ` · ${walkForwardSourceLabel(result)} · ${result.price_adjustment || "QFQ"}`
              : " · 审计指标不可用"}
          </small>
        </span>
        <em className={sufficient ? "sufficient" : "insufficient"}>{statusLabel}</em>
      </header>

      {scenarioView.metricsVisible ? (
        <>
          {scenarioView.legacyFrictionWarning ? (
            <p className="walk-forward-legacy-warning">
              <AlertTriangle aria-hidden="true" size={12} /> {scenarioView.legacyFrictionWarning.message}
            </p>
          ) : null}
          {isV3 || isV4 ? (
            <dl className="walk-forward-metrics walk-forward-common-metrics">
              <div><dt>全部 / 非重叠窗口</dt><dd>{displayReady ? `${summary.fold_count ?? 0} / ${nonOverlappingFoldCount}` : "—"}</dd></div>
              <div><dt>无摩擦等权基准复利</dt><dd>{percent(benchmarkCumulativeReturn)}</dd></div>
              <div><dt>摩擦场景集</dt><dd title={WALK_FORWARD_SCENARIO_SET_VERSION}>scenarios_v1</dd></div>
              <div><dt>结果版本</dt><dd title={resultVersion}>{resultVersion.replace("walk_forward_", "")}</dd></div>
            </dl>
          ) : (
            <dl className="walk-forward-metrics">
              <div><dt>全部 / 非重叠窗口</dt><dd>{displayReady ? `${summary.fold_count ?? 0} / ${nonOverlappingFoldCount}` : "—"}</dd></div>
              <div><dt>历史测试正收益窗口比例（非未来胜率）</dt><dd>{ratioPercent(displayReady ? historicalPositiveFoldRatio : null)}</dd></div>
              <div><dt>组合窗口复利</dt><dd>{percent(portfolioCumulativeReturn)}</dd></div>
              <div><dt>等权基准窗口复利</dt><dd>{percent(benchmarkCumulativeReturn)}</dd></div>
              <div><dt>最差窗口回撤</dt><dd>{percent(displayReady ? summary.max_drawdown_pct : null)}</dd></div>
              <div><dt>结果版本</dt><dd title={resultVersion}>{resultVersion.replace("walk_forward_", "")}</dd></div>
            </dl>
          )}

          {isV4 ? <MemoWalkForwardGates view={scenarioView} /> : null}
          <MemoWalkForwardMethodCard view={scenarioView} />
          {scenarioView.scenarioMetricsVisible && displayReady ? (
            <MemoWalkForwardScenarioResults
              rows={scenarioView.rows}
              positiveRateLabel={scenarioView.evaluation?.positiveRateLabel}
            />
          ) : null}
          {isV4 && displayReady ? <MemoWalkForwardFoldAudit rows={scenarioView.foldRows} /> : null}
          {insufficient && !hasBlockedScenario ? (
            <p className="walk-forward-insufficient">
              {nonOverlappingFoldCount < minimumFoldCount
                ? `仅有 ${nonOverlappingFoldCount} 个非重叠测试窗口，少于最低 ${minimumFoldCount} 个；数据充分性只能标记为 insufficient。`
                : "结果状态或三档场景未完整满足 sufficient 校验门槛，暂按 insufficient 处理。"}
            </p>
          ) : null}
          <p className="walk-forward-data-status">
            数据状态：{walkForwardSourceLabel(result)}；{isV3
              ? "三档摩擦包含佣金、滑点、借券费和容量代理；等权基准明确保持无摩擦。"
              : isV4
                ? "规则逐折仅由训练窗生成；三档摩擦包含佣金、滑点、借券费和容量代理，等权基准保持无摩擦。"
                : "每个测试窗口内按固定纸面买入持有规则回放。"}
          </p>
          <p className={safetyBoundaryReady ? "walk-forward-safe-note" : "walk-forward-safe-note blocked"}>
            <ShieldCheck aria-hidden="true" size={12} />
            {safetyBoundaryReady
              ? "安全边界：本次模型调用 0、OpenAI 调用 0；仅历史研究与纸面评估，无账户连接、无下单能力、禁止实盘。"
              : "安全字段不完整或出现模型调用：该结果不可用于确认或任何执行。"}
          </p>
          {!currentlyActionable ? (
            <p className="walk-forward-actionability-note">
              当前决定已过期或不再可行动；该历史记录仍保留，其历史完整性状态不因此改变。
            </p>
          ) : null}
        </>
      ) : (
        <p className="walk-forward-method-note blocked" role="alert">
          <AlertTriangle aria-hidden="true" size={12} /> {integrity.detail}
        </p>
      )}
      <p className="walk-forward-interpretation">
        历史测试正收益窗口比例（非未来胜率）；容量和成交额均为代理，不是投资建议或真实成交承诺。
      </p>
    </section>
  );
}


function walkForwardPortfolioVersion(run) {
  const direct = Number(run?.portfolio_version);
  if (Number.isInteger(direct) && direct > 0) return direct;
  const nested = Number(run?.result?.portfolio_version);
  return Number.isInteger(nested) && nested > 0 ? nested : 0;
}


function portfolioLineageMeta(entries, portfolio) {
  if (!entries?.length) {
    return {
      tone: "unlinked",
      label: "未关联候选",
      detail: "不会计入当前决策谱系",
      source: null,
      conflict: false,
      exactVersion: false,
      exactConfirmed: false,
    };
  }
  const packageIds = new Set(entries.map(({ decisionPackage }) => decisionPackage.package_id));
  const conflict = packageIds.size > 1;
  const exactEntries = entries.filter(({ event }) => (
    Number(event.resource_revision || event.resource_snapshot?.version || 0) === Number(portfolio.version)
  ));
  const confirmedEntry = exactEntries.find(({ event }) => (
    event.relation_type === "confirms"
    && String(event.resource_state || event.resource_snapshot?.status || "").toUpperCase() === "CONFIRMED"
  ));
  const exactEntry = confirmedEntry || exactEntries[0];
  const sourceEntry = exactEntry || entries[0];
  const decisionPackage = sourceEntry.decisionPackage;
  const source = decisionPackageSource(decisionPackage, sourceEntry.event);
  const integrityReady = decisionPackage.integrity_ok === true
    && decisionPackage.anchor?.integrity_ok === true
    && sourceEntry.event.integrity_ok === true;
  if (conflict || !integrityReady || decisionPackage.state === "chain_broken") {
    return {
      tone: "broken",
      label: conflict ? "来源冲突" : "谱系校验失败",
      detail: "仅供历史审计，不能作为当前链",
      source,
      conflict,
      exactVersion: Boolean(exactEntry),
      exactConfirmed: false,
    };
  }
  if (!exactEntry) {
    return {
      tone: "stale",
      label: "谱系版本未覆盖",
      detail: `当前组合 v${portfolio.version} 没有对应事件`,
      source,
      conflict: false,
      exactVersion: false,
      exactConfirmed: false,
    };
  }
  const active = decisionPackage.state === "active" && decisionPackage.anchor?.current === true;
  return {
    tone: active ? "active" : "stale",
    label: active ? "当前决策谱系" : "历史决策谱系",
    detail: `${source.selected_option_title} · 事件 ${sourceEntry.event.relation_type} v${source.resource_revision || "?"}`,
    source,
    conflict: false,
    exactVersion: true,
    exactConfirmed: Boolean(confirmedEntry),
  };
}


const MemoLockedFrictionScenarios = memo(LockedFrictionScenarios);
const MemoWalkForwardScenarioResults = memo(WalkForwardScenarioResults);
const MemoWalkForwardMethodCard = memo(WalkForwardMethodCard);
const MemoWalkForwardGates = memo(WalkForwardGates);
const MemoWalkForwardFoldAudit = memo(WalkForwardFoldAudit);
const MemoWalkForwardSummary = memo(WalkForwardSummary);


export const PaperPortfolioPanel = memo(function PaperPortfolioPanel({
  portfolios = EMPTY_LIST,
  loading,
  walkForwardRunsByPortfolio,
  walkForwardLoadingByPortfolio,
  walkForwardErrorsByPortfolio,
  candidateComparison = null,
  candidateComparisonLoading = false,
  candidateComparisonError = "",
  decisionPackages = EMPTY_LIST,
  onAdd,
  onEdit,
  onConfirm,
  onEvaluate,
  onRunWalkForward,
  onCompareCandidates,
}) {
  const [walkForwardConfigs, setWalkForwardConfigs] = useState({});
  const [visibleLimit, setVisibleLimit] = useState(4);
  const panelTitleId = useId();
  const cardTitleIdPrefix = useId();
  const portfolioRows = Array.isArray(portfolios) ? portfolios : EMPTY_LIST;
  const decisionPackageRows = Array.isArray(decisionPackages) ? decisionPackages : EMPTY_LIST;
  const portfolioCollectionKey = useMemo(
    () => JSON.stringify(portfolioRows.map((portfolio) => String(portfolio?.id || ""))),
    [portfolioRows],
  );
  useEffect(() => setVisibleLimit(4), [portfolioCollectionKey]);
  const visible = useMemo(
    () => portfolioRows.slice(0, visibleLimit),
    [portfolioRows, visibleLimit],
  );
  const hiddenPortfolioCount = Math.max(0, portfolioRows.length - visible.length);
  const portfolioStats = useMemo(() => portfolioRows.reduce((stats, portfolio) => ({
    confirmed: stats.confirmed + (portfolio?.status === "CONFIRMED" ? 1 : 0),
    riskReady: stats.riskReady + (portfolio?.evaluation?.risk_gate?.status === "PASS" ? 1 : 0),
  }), { confirmed: 0, riskReady: 0 }), [portfolioRows]);
  const lineageIndex = useMemo(
    () => buildPortfolioLineageIndex(decisionPackageRows),
    [decisionPackageRows],
  );
  const canAdd = typeof onAdd === "function";
  const canEdit = typeof onEdit === "function";
  const canConfirm = typeof onConfirm === "function";
  const canEvaluate = typeof onEvaluate === "function";
  const canRunWalkForward = typeof onRunWalkForward === "function";

  const updateWalkForwardConfig = (portfolioId, field, value) => {
    setWalkForwardConfigs((current) => ({
      ...current,
      [portfolioId]: {
        ...DEFAULT_WALK_FORWARD_CONFIG,
        ...(current[portfolioId] || {}),
        [field]: value,
      },
    }));
  };

  return (
    <section className="paper-portfolio-panel" aria-labelledby={panelTitleId} aria-busy={Boolean(loading)}>
      <div className="paper-portfolio-toolbar">
        <div className="paper-portfolio-toolbar-copy">
          <BarChart3 aria-hidden="true" size={18} />
          <span>
            <small>SIMULATION CONTROL / LOCAL ONLY</small>
            <h3 id={panelTitleId}>纸面组合与历史验证</h3>
            <p>用纸面权重和风险预算约束研究方案；不连接账户，不产生订单。</p>
          </span>
        </div>
        <button className="secondary compact" type="button" onClick={(event) => canAdd && onAdd({}, event.currentTarget)} disabled={loading || !canAdd}>
          <Plus aria-hidden="true" size={13} />新建方案
        </button>
      </div>
      <div className="paper-portfolio-summary">
        <dl aria-label="模拟组合工作台摘要">
          <div><dt>组合总数</dt><dd><data value={portfolioRows.length}>{portfolioRows.length}</data></dd></div>
          <div><dt>用户已确认</dt><dd><data value={portfolioStats.confirmed}>{portfolioStats.confirmed}</data></dd></div>
          <div><dt>风险门内</dt><dd><data value={portfolioStats.riskReady}>{portfolioStats.riskReady}</data></dd></div>
          <div><dt>当前展示</dt><dd><data value={visible.length}>{visible.length} / {portfolioRows.length}</data></dd></div>
        </dl>
        <small>计数只表示本地纸面状态；风险门内不代表推荐、批准或可执行。</small>
      </div>
      <CandidateComparisonPanel
        portfolios={portfolioRows}
        runsByPortfolio={walkForwardRunsByPortfolio}
        comparison={candidateComparison}
        loading={candidateComparisonLoading}
        error={candidateComparisonError}
        onCompare={onCompareCandidates}
      />
      {visible.length ? (
        <>
          <div className="paper-portfolio-list" role="list">
            {visible.map((portfolio, portfolioIndex) => {
            const evaluation = portfolio.evaluation || {};
            const gate = evaluation.risk_gate || {};
            const exposures = evaluation.exposures || {};
            const metrics = evaluation.metrics || {};
            const ready = gate.status === "PASS";
            const lineage = portfolioLineageMeta(lineageIndex.get(String(portfolio.id)), portfolio);
            const candidateBinding = portfolio.candidate_simulation_binding || {};
            const candidateContract = portfolio.candidate_simulation_contract || null;
            const contractHorizon = finiteNumber(candidateContract?.evaluation?.horizon_days);
            const strictCandidateMapping = candidateBinding.applicable === true;
            const candidateMappingReady = !strictCandidateMapping || candidateBinding.ready === true;
            const walkForwardConfig = {
              ...DEFAULT_WALK_FORWARD_CONFIG,
              ...(walkForwardConfigs[portfolio.id] || {}),
              ...(candidateMappingReady && contractHorizon !== null ? {
                test_days: contractHorizon,
                step_days: contractHorizon,
              } : {}),
            };
            const allWalkForwardRuns = walkForwardRunsByPortfolio?.[portfolio.id] || [];
            const currentWalkForwardRuns = allWalkForwardRuns
              .filter((run) => walkForwardPortfolioVersion(run) === Number(portfolio.version))
              .sort((left, right) => (timestampValue(right.created_at) || 0) - (timestampValue(left.created_at) || 0));
            const walkForwardRun = currentWalkForwardRuns[0] || null;
            const historicalWalkForwardCount = allWalkForwardRuns.length - currentWalkForwardRuns.length;
            const lineageLocked = Boolean(lineage.source && lineage.tone !== "active");
            const walkForwardBusy = Boolean(walkForwardLoadingByPortfolio?.[portfolio.id]);
            const walkForwardError = boundedText(walkForwardErrorsByPortfolio?.[portfolio.id]);
            const configReady = walkForwardConfigValid(walkForwardConfig);
            const walkForwardReady = portfolio.status === "CONFIRMED"
              && lineage.tone === "active"
              && lineage.exactConfirmed
              && candidateMappingReady;
            const walkForwardBlockedReason = portfolio.status !== "CONFIRMED"
              ? "必须先由用户确认当前组合版本"
              : lineage.tone !== "active"
                ? "必须绑定当前有效的支持决定谱系"
                : !lineage.exactConfirmed
                  ? "当前组合版本缺少精确的 confirms 事件"
                  : !candidateMappingReady
                    ? "候选模拟合同未通过精确语义校验"
                    : "";
            const portfolioTitle = boundedText(portfolio.name, "未命名模拟组合", 160);
            const cardTitleId = `${cardTitleIdPrefix}-${portfolioIndex}`;
            return (
              <article
                className="paper-portfolio-card"
                data-risk-state={ready ? "pass" : "blocked"}
                key={JSON.stringify([portfolio.id, portfolio.version, portfolioIndex])}
                role="listitem"
                aria-labelledby={cardTitleId}
              >
                <header>
                  <span>
                    <h4 id={cardTitleId}>{portfolioTitle}</h4>
                    <small>v{portfolio.version} · {portfolio.status === "CONFIRMED" ? "用户已确认" : "待确认草稿"}</small>
                  </span>
                  <span className={ready ? "paper-risk-status pass" : "paper-risk-status blocked"}>
                    {ready ? <CheckCircle2 aria-hidden="true" size={13} /> : <AlertTriangle aria-hidden="true" size={13} />}
                    {ready ? "预算内" : "未通过"}
                  </span>
                </header>
                <div className={`paper-lineage-source ${lineage.tone}`}>
                  {lineage.source ? <GitBranch aria-hidden="true" size={12} /> : <Unlink aria-hidden="true" size={12} />}
                  <span><strong>{lineage.label}</strong><small>{lineage.detail}</small></span>
                </div>
                <div className={`paper-lineage-source ${candidateBinding.ready ? "active" : "stale"}`}>
                  {candidateBinding.ready ? <ShieldCheck aria-hidden="true" size={12} /> : <AlertTriangle aria-hidden="true" size={12} />}
                  <span>
                    <strong>
                      {candidateBinding.ready
                        ? "已验证候选模拟映射"
                        : candidateBinding.status === "legacy_lineage_only"
                          ? "旧版仅有决定关联"
                          : candidateBinding.status === "unbound"
                            ? "手工模拟组合"
                            : "候选模拟合同待修复"}
                    </strong>
                    <small>
                      {candidateBinding.ready
                        ? `${String(candidateContract?.implementation?.target_symbol || "").replace("US.", "")} · ${candidateContract?.source?.candidate_snapshot?.direction || "?"}→${candidateContract?.implementation?.target_side || "?"} · ${percent(candidateContract?.implementation?.target_weight_pct, 0)} · ${candidateContract?.evaluation?.horizon_days || "?"} 日 · ${shortHash(candidateContract?.contract_sha256)}`
                        : candidateBinding.issues?.[0]?.message || "不计入正式候选语义比较。"}
                    </small>
                  </span>
                </div>
                <div className="paper-position-chips" role="list" aria-label="纸面方向与权重">
                  {(portfolio.positions || []).filter((position) => position.side !== "FLAT").map((position, positionIndex) => (
                    <span key={JSON.stringify([portfolio.id, position.symbol, positionIndex])} className={position.side.toLowerCase()} role="listitem">
                      {position.symbol.replace("US.", "")} · {sideLabel(position.side)} {percent(position.weight_pct, 0)}
                    </span>
                  ))}
                </div>
                <dl className="paper-risk-metrics">
                  <div><dt>总 / 净敞口</dt><dd>{percent(exposures.gross_exposure_pct)} / {percent(exposures.net_exposure_pct)}</dd></div>
                  <div><dt>年化波动</dt><dd>{percent(metrics.annualized_volatility_pct)}</dd></div>
                  <div><dt>历史最大回撤</dt><dd>{percent(metrics.max_drawdown_pct)}</dd></div>
                  <div><dt>压力最大损失</dt><dd>{percent(Math.max(0, ...(evaluation.stress_results || []).map((item) => Number(item.loss_pct) || 0)))}</dd></div>
                </dl>
                {!ready ? (
                  <p className="paper-risk-blocker">
                    {(gate.blockers || [])[0]?.title || "富途复权历史不足，尚不能确认风险结果。"}
                  </p>
                ) : null}
                <section className="walk-forward-control" aria-label="固定纸面组合历史滚动回放配置">
                  <div className="walk-forward-control-heading">
                    <span>
                      <strong>{candidateBinding.ready ? "固定候选方向历史回放" : "固定纸面组合历史滚动回放"}</strong>
                      <small>{candidateBinding.ready ? "候选期限与标的已锁定 · 历史 QFQ 日线" : "版本化纸面方案 · 历史 QFQ 日线"}</small>
                    </span>
                    <BarChart3 aria-hidden="true" size={15} />
                  </div>
                  <div className="walk-forward-config-grid">
                    {WALK_FORWARD_CONFIG_FIELDS.map(([field, label, minimum, maximum, step, unit]) => (
                      <label key={field}>
                        <span>{label}<small>{unit}</small></span>
                        <input
                          type="number"
                          min={minimum}
                          max={maximum}
                          step={step}
                          value={walkForwardConfig[field]}
                          disabled={candidateBinding.ready && ["test_days", "step_days"].includes(field)}
                          onChange={(event) => updateWalkForwardConfig(portfolio.id, field, event.target.value)}
                        />
                      </label>
                    ))}
                  </div>
                  <MemoLockedFrictionScenarios />
                  <button
                    className="secondary compact walk-forward-run"
                    type="button"
                    disabled={loading || walkForwardBusy || !configReady || !walkForwardReady || !canRunWalkForward}
                    aria-busy={walkForwardBusy}
                    title={walkForwardBlockedReason || "运行当前组合版本的历史回放"}
                    onClick={() => {
                      if (!canRunWalkForward) return;
                      onRunWalkForward(portfolio, {
                        train_days: Number(walkForwardConfig.train_days),
                        test_days: Number(walkForwardConfig.test_days),
                        step_days: Number(walkForwardConfig.step_days),
                      });
                    }}
                  >
                    <BarChart3 aria-hidden="true" size={12} />
                    {walkForwardBusy ? "正在读取历史并回放…" : "运行历史回放"}
                  </button>
                  {walkForwardBusy ? <p className="paper-request-status" role="status" aria-live="polite">正在读取历史数据并运行纸面回放。</p> : null}
                  {!configReady ? <p className="walk-forward-inline-error">请检查训练、测试和步进窗口。</p> : null}
                  {walkForwardBlockedReason ? <p className="walk-forward-inline-error">{walkForwardBlockedReason}，历史回放暂不可运行。</p> : null}
                  {walkForwardError ? <p className="walk-forward-inline-error" role="alert">{walkForwardError}</p> : null}
                </section>
                {walkForwardRun ? (
                  <MemoWalkForwardSummary run={walkForwardRun} actionableNow={walkForwardReady} />
                ) : (
                  <p className="walk-forward-empty">
                    尚无匹配组合 v{portfolio.version} 的版本化回放结果。运行后只生成历史研究记录，不改变组合，也不会产生订单。
                  </p>
                )}
                {historicalWalkForwardCount ? (
                  <p className="walk-forward-history-note"><History aria-hidden="true" size={11} />另有 {historicalWalkForwardCount} 条旧版本回放，未作为当前 v{portfolio.version} 结果展示。</p>
                ) : null}
                <footer>
                  <button
                    className="text-action"
                    type="button"
                    onClick={(event) => {
                      if (!canEdit) return;
                      onEdit({
                        ...portfolio,
                        ...(lineage.source ? { lineage_source: lineage.source } : {}),
                      }, event.currentTarget);
                    }}
                    disabled={loading || walkForwardBusy || lineageLocked || !canEdit}
                  >
                    <Pencil aria-hidden="true" size={12} />编辑
                  </button>
                  <button className="text-action" type="button" onClick={() => canEvaluate && onEvaluate(portfolio)} disabled={loading || walkForwardBusy || lineageLocked || !candidateMappingReady || !canEvaluate}>
                    <RefreshCw aria-hidden="true" size={12} />重新复算
                  </button>
                  {portfolio.status !== "CONFIRMED" ? (
                    <button className="text-action confirm" type="button" onClick={() => canConfirm && onConfirm(portfolio)} disabled={loading || walkForwardBusy || !ready || lineageLocked || !candidateMappingReady || !canConfirm}>
                      <ShieldCheck aria-hidden="true" size={12} />用户确认
                    </button>
                  ) : null}
                </footer>
              </article>
            );
            })}
          </div>
          {hiddenPortfolioCount ? (
            <button
              className="paper-portfolio-more"
              type="button"
              onClick={() => setVisibleLimit((current) => Math.min(current + 4, portfolioRows.length))}
            >
              <Plus aria-hidden="true" size={13} />
              再显示 {Math.min(4, hiddenPortfolioCount)} 个组合
              <small>仍有 {hiddenPortfolioCount} 个未显示</small>
            </button>
          ) : null}
        </>
      ) : (
        <div className="paper-portfolio-empty" role="note">
          <BarChart3 aria-hidden="true" size={20} />
          <span>
            <small>EMPTY SIMULATION LEDGER</small>
            <strong>尚未建立可供风控复核的模拟组合</strong>
            <p>可从用户支持的候选建立精确关联组合，也可使用上方入口创建独立纸面方案；任何结果仍需用户复核。</p>
          </span>
          <ul aria-label="空组合工作台边界">
            <li>不连接账户</li>
            <li>不产生订单</li>
            <li>不替代用户决定</li>
          </ul>
        </div>
      )}
    </section>
  );
});


export const PaperPortfolioDialog = memo(function PaperPortfolioDialog({ portfolio, open, onClose, onSubmit, restoreFocusRef }) {
  const dialogRef = useRef(null);
  const closeButtonRef = useRef(null);
  const dialogTitleId = useId();
  const dialogDescriptionId = useId();
  const [draft, setDraft] = useState(() => blankPlan());
  const [initializedPortfolio, setInitializedPortfolio] = useState(null);
  const [derivationNote, setDerivationNote] = useState("");
  const [mappingConfirmed, setMappingConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const requestSessionRef = useRef(0);
  const canClose = typeof onClose === "function";
  const canSubmit = typeof onSubmit === "function";

  useLayoutEffect(() => {
    requestSessionRef.current += 1;
    setBusy(false);
    if (!open || !portfolio) {
      setInitializedPortfolio(null);
      return;
    }
    setDraft(portfolioPlan(portfolio));
    setDerivationNote(portfolio?.id ? "" : String(portfolio?.lineage_source?.derivation_note || ""));
    setMappingConfirmed(false);
    setError("");
    setInitializedPortfolio(portfolio);
  }, [open, portfolio]);

  const surfaceOpen = Boolean(open && portfolio && initializedPortfolio === portfolio);
  const closeDialog = () => {
    if (!canClose) return;
    requestSessionRef.current += 1;
    setBusy(false);
    onClose();
  };
  const requestClose = () => {
    if (!busy) closeDialog();
  };
  useModalFocus({
    open: surfaceOpen,
    containerRef: dialogRef,
    initialFocusRef: closeButtonRef,
    restoreFallbackRef: restoreFocusRef,
    onClose: busy || !canClose ? null : requestClose,
  });
  useEffect(() => {
    if (surfaceOpen && busy) dialogRef.current?.focus({ preventScroll: true });
  }, [busy, surfaceOpen]);

  const activeCount = draft.positions.filter((position) => position.side !== "FLAT").length;
  const lineageSource = portfolio?.lineage_source || null;
  const persistedCandidateContract = portfolio?.candidate_simulation_contract || null;
  const persistedCandidateSnapshot = persistedCandidateContract?.source?.candidate_snapshot || {};
  const candidateSeed = lineageSource?.candidate_simulation_seed || (
    persistedCandidateContract ? {
      applicable: true,
      ready: portfolio?.candidate_simulation_binding?.ready === true,
      source_sha256: persistedCandidateContract?.source?.source_sha256,
      candidate_revision: persistedCandidateContract?.source?.candidate_revision,
      candidate_snapshot_sha256: persistedCandidateContract?.source?.candidate_snapshot_sha256,
      candidate_snapshot: persistedCandidateSnapshot,
      symbol: persistedCandidateContract?.implementation?.target_symbol,
      direction: persistedCandidateSnapshot.direction,
      target_side: persistedCandidateContract?.implementation?.target_side,
      horizon_days: persistedCandidateContract?.evaluation?.horizon_days,
      thesis: persistedCandidateSnapshot.thesis,
      invalidation: persistedCandidateSnapshot.invalidation,
      allowed_rules: [{ id: persistedCandidateContract?.evaluation?.rule_id }],
      issues: portfolio?.candidate_simulation_binding?.issues || [],
    } : null
  );
  const strictCandidateMapping = candidateSeed?.applicable === true;
  const strictCandidateReady = strictCandidateMapping && candidateSeed?.ready === true;
  const requiresDerivation = Boolean(!portfolio?.id && lineageSource?.user_decision_id);
  const lineageActionLabel = {
    support: "用户已支持",
    hold: "用户已保留",
    return: "用户已退回",
  }[lineageSource?.action] || "用户决定状态未知";
  if (!surfaceOpen) return null;

  const updatePosition = (symbol, patch) => {
    setMappingConfirmed(false);
    setDraft((current) => ({
      ...current,
      positions: current.positions.map((position) => (
        position.symbol === symbol
          ? {
            ...position,
            ...patch,
            ...(patch.side === "FLAT" ? { weight_pct: 0 } : {}),
          }
          : position
      )),
    }));
  };

  const updateScenario = (index, patch) => {
    setDraft((current) => ({
      ...current,
      stress_scenarios: current.stress_scenarios.map((scenario, scenarioIndex) => (
        scenarioIndex === index ? { ...scenario, ...patch } : scenario
      )),
    }));
  };

  const updateShock = (index, symbol, value) => {
    setDraft((current) => ({
      ...current,
      stress_scenarios: current.stress_scenarios.map((scenario, scenarioIndex) => (
        scenarioIndex === index
          ? { ...scenario, shocks: { ...scenario.shocks, [symbol]: value } }
          : scenario
      )),
    }));
  };

  const addScenario = () => {
    setDraft((current) => {
      const existingIds = new Set(current.stress_scenarios.map((scenario) => scenario.id));
      let nextNumber = current.stress_scenarios.length + 1;
      while (existingIds.has(`custom_scenario_${nextNumber}`)) nextNumber += 1;
      return {
        ...current,
        stress_scenarios: [
          ...current.stress_scenarios,
          {
            id: `custom_scenario_${nextNumber}`,
            name: `自定义情景 ${nextNumber}`,
            shocks: Object.fromEntries(SYMBOLS.map((symbol) => [symbol, 0])),
          },
        ],
      };
    });
  };

  const submit = async (event) => {
    event.preventDefault();
    if (busy) return;
    if (!canSubmit) {
      setError("当前未提供模拟组合保存处理器，不能提交。");
      return;
    }
    if (!activeCount) {
      setError("请至少选择一个模拟做多或模拟做空标的。");
      return;
    }
    if (requiresDerivation && derivationNote.trim().length < 3) {
      setError("请填写至少 3 个字的方案推导说明，说明候选如何转化为纸面权重与约束。");
      return;
    }
    if (strictCandidateMapping && !strictCandidateReady) {
      setError(candidateSeed?.issues?.[0]?.message || "当前候选模拟规格不可用，请刷新决定包。");
      return;
    }
    if (strictCandidateReady && !mappingConfirmed) {
      setError("请先确认候选到纸面规格的精确映射。");
      return;
    }
    const requestSession = requestSessionRef.current + 1;
    requestSessionRef.current = requestSession;
    setBusy(true);
    setError("");
    try {
      await onSubmit({
        ...draft,
        ...(portfolio?.id ? { id: portfolio.id, expected_version: portfolio.version } : {}),
        ...(requiresDerivation ? {
          user_decision_id: lineageSource.user_decision_id,
          derivation_note: derivationNote.trim(),
        } : {}),
        ...(strictCandidateReady ? {
          candidate_simulation_confirmation: {
            version: CANDIDATE_SIMULATION_CONFIRMATION_VERSION,
            expected_source_sha256: candidateSeed.source_sha256,
            expected_candidate_revision: candidateSeed.candidate_revision,
            expected_candidate_snapshot_sha256: candidateSeed.candidate_snapshot_sha256,
            expected_target_weight_pct: Number(
              draft.positions.find((position) => position.symbol === candidateSeed.symbol)?.weight_pct,
            ),
            strategy_rule_id: CANDIDATE_SIMULATION_RULE_ID,
            user_confirmed: true,
          },
        } : {}),
      });
      if (requestSessionRef.current === requestSession) closeDialog();
    } catch (requestError) {
      if (requestSessionRef.current === requestSession) {
        setError(boundedText(requestError, "模拟组合保存失败。"));
      }
    } finally {
      if (requestSessionRef.current === requestSession) setBusy(false);
    }
  };

  return (
    <div
      className="dialog-backdrop paper-portfolio-dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) requestClose();
      }}
    >
      <form
        ref={dialogRef}
        className="dialog paper-portfolio-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={dialogTitleId}
        aria-describedby={dialogDescriptionId}
        aria-busy={busy}
        tabIndex={-1}
        onSubmit={submit}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <span>
            <strong id={dialogTitleId}>{portfolio?.id ? "编辑模拟组合" : "新建模拟组合"}</strong>
            <small id={dialogDescriptionId}>保存时使用富途只读复权历史重新计算风险</small>
          </span>
          <button ref={closeButtonRef} className="icon-button" type="button" aria-label="关闭模拟组合设置" onClick={requestClose} disabled={busy || !canClose}><X aria-hidden="true" size={18} /></button>
        </header>
        <fieldset className="paper-portfolio-dialog-body" disabled={busy}>
          <legend className="paper-dialog-body-legend">模拟组合设置</legend>
          {error ? <div className="dialog-error" role="alert">{error}</div> : null}
          {lineageSource ? (
            <section className={`paper-dialog-lineage-source ${lineageSource.package_state || "stale"}`}>
              <GitBranch aria-hidden="true" size={17} />
              <span>
                <small>只读来源 · 不会随组合编辑而改写</small>
                <strong>{lineageSource.selected_option_title || "已选候选方案"}</strong>
                <em>
                  产物 {String(lineageSource.artifact_id || "").slice(-8) || "未记录"} · v{lineageSource.artifact_version || "?"}
                  {` · ${lineageActionLabel} · 决定 ${String(lineageSource.user_decision_id || "").slice(-8) || "未记录"}`}
                  {lineageSource.selected_option_id
                    ? ` · AI 首选 ${lineageSource.ai_preferred_option_id || "未记录"} · 用户选择 ${lineageSource.selected_option_id}`
                    : ""}
                  {lineageSource.resource_revision ? ` · 组合事件 v${lineageSource.resource_revision}` : ""}
                </em>
              </span>
            </section>
          ) : null}
          {strictCandidateMapping ? (
            <section className={`paper-dialog-lineage-source ${strictCandidateReady ? "active" : "stale"}`}>
              {strictCandidateReady ? <ShieldCheck aria-hidden="true" size={17} /> : <AlertTriangle aria-hidden="true" size={17} />}
              <span>
                <small>已封印候选 · 只读精确规格</small>
                <strong>
                  {String(candidateSeed?.candidate_snapshot?.title || lineageSource?.selected_option_title || "候选方案")}
                </strong>
                <em>
                  {String(candidateSeed?.symbol || "").replace("US.", "") || "标的缺失"}
                  {` · ${candidateSeed?.direction || "?"}→${candidateSeed?.target_side || "?"}`}
                  {` · ${candidateSeed?.horizon_days || "?"} 个交易日`}
                  {` · 候选 v${candidateSeed?.candidate_revision || "?"}`}
                  {` · ${shortHash(candidateSeed?.candidate_snapshot_sha256)}`}
                </em>
                <small>失效条件：{candidateSeed?.invalidation || "未提供"}</small>
                {!strictCandidateReady ? (
                  <small>{candidateSeed?.issues?.[0]?.message || "候选规格暂不可实施。"}</small>
                ) : null}
              </span>
            </section>
          ) : null}
          {requiresDerivation ? (
            <label className="paper-derivation-note">方案推导说明
              <textarea
                required
                minLength={3}
                maxLength={1000}
                value={derivationNote}
                onChange={(event) => setDerivationNote(event.target.value)}
                placeholder="说明如何把已支持候选转成 MU / SNDK / WDC / STX 的纸面方向、权重、风险预算和失效条件。"
              />
              <small>该说明会与用户决定 ID 一起写入不可变谱系事件；不能填写账户或订单指令。</small>
            </label>
          ) : null}
          <label className="paper-plan-name">方案名称
            <input value={draft.name} maxLength={80} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} />
          </label>

          <section className="paper-dialog-section">
            <div className="paper-dialog-heading">
              <span><strong>纸面权重</strong><small>{activeCount} 个非观望标的</small></span>
              <em>权重只用于风险复算，不代表真实持仓</em>
            </div>
            <div className="paper-position-editor">
              {draft.positions.map((position) => (
                <fieldset key={position.symbol}>
                  <legend>{position.symbol.replace("US.", "")}</legend>
                  <label>方向
                    <select
                      value={position.side}
                      disabled={strictCandidateReady}
                      onChange={(event) => updatePosition(position.symbol, { side: event.target.value })}
                    >
                      {SIDE_OPTIONS.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
                    </select>
                  </label>
                  <label>权重 %
                    <input
                      type="number"
                      min="0"
                      max="100"
                      step="0.5"
                      value={position.weight_pct}
                      disabled={position.side === "FLAT" || (strictCandidateReady && position.symbol !== candidateSeed.symbol)}
                      onChange={(event) => updatePosition(position.symbol, { weight_pct: event.target.value })}
                    />
                  </label>
                  <label className="wide">研究依据
                    <input disabled={strictCandidateReady} value={position.thesis} maxLength={1200} placeholder="为什么纳入该标的" onChange={(event) => updatePosition(position.symbol, { thesis: event.target.value })} />
                  </label>
                  <label className="wide">失效条件
                    <input disabled={strictCandidateReady} value={position.invalidation} maxLength={1200} placeholder="出现什么情况需要退回重评" onChange={(event) => updatePosition(position.symbol, { invalidation: event.target.value })} />
                  </label>
                </fieldset>
              ))}
            </div>
          </section>

          <section className="paper-dialog-section">
            <div className="paper-dialog-heading">
              <span><strong>风险预算</strong><small>超过任一上限则不能确认</small></span>
            </div>
            <div className="paper-budget-grid">
              {BUDGET_FIELDS.map(([field, label]) => (
                <label key={field}>{label} %
                  <input
                    type="number"
                    min="0.1"
                    max="200"
                    step="0.1"
                    value={draft.budgets[field]}
                    onChange={(event) => setDraft((current) => ({
                      ...current,
                      budgets: { ...current.budgets, [field]: event.target.value },
                    }))}
                  />
                </label>
              ))}
            </div>
          </section>

          <section className="paper-dialog-section">
            <div className="paper-dialog-heading">
              <span><strong>压力情景</strong><small>每个数字是假设价格冲击幅度</small></span>
              <button className="secondary compact" type="button" onClick={addScenario} disabled={draft.stress_scenarios.length >= 8}>
                <Plus aria-hidden="true" size={12} />添加情景
              </button>
            </div>
            <div className="paper-stress-editor">
              {draft.stress_scenarios.map((scenario, index) => (
                <fieldset key={scenario.id
                  ? JSON.stringify(["scenario_id", scenario.id])
                  : JSON.stringify(["scenario_index", index])}>
                  <legend>
                    <input value={scenario.name} maxLength={80} onChange={(event) => updateScenario(index, { name: event.target.value })} />
                    {draft.stress_scenarios.length > 1 ? (
                      <button type="button" aria-label={`移除 ${scenario.name}`} onClick={() => setDraft((current) => ({
                        ...current,
                        stress_scenarios: current.stress_scenarios.filter((_, scenarioIndex) => scenarioIndex !== index),
                      }))}><X aria-hidden="true" size={13} /></button>
                    ) : null}
                  </legend>
                  {SYMBOLS.map((symbol) => (
                    <label key={symbol}>{symbol.replace("US.", "")} %
                      <input
                        type="number"
                        min="-100"
                        max="100"
                        step="0.5"
                        value={scenario.shocks[symbol]}
                        onChange={(event) => updateShock(index, symbol, event.target.value)}
                      />
                    </label>
                  ))}
                </fieldset>
              ))}
            </div>
          </section>

          {strictCandidateReady ? (
            <label className="paper-safe-note candidate-mapping-confirmation">
              <input
                type="checkbox"
                checked={mappingConfirmed}
                onChange={(event) => setMappingConfirmed(event.target.checked)}
              />
              <span>
                我确认此纸面规格精确映射用户所选候选；固定规则为 {CANDIDATE_SIMULATION_RULE_ID}，不代表实盘指令。
              </span>
            </label>
          ) : null}
          <p className="paper-safe-note"><ShieldCheck aria-hidden="true" size={14} />仅研究、回测和模拟；没有账户、数量、价格指令或下单能力。</p>
        </fieldset>
        <footer>
          <button className="secondary" type="button" onClick={requestClose} disabled={busy || !canClose}>取消</button>
          <button
            className="primary"
            type="submit"
            disabled={busy
              || !canSubmit
              || (requiresDerivation && derivationNote.trim().length < 3)
              || (strictCandidateMapping && !strictCandidateReady)
              || (strictCandidateReady && !mappingConfirmed)}
          >{busy ? "正在复算…" : "保存并复算风险"}</button>
        </footer>
      </form>
    </div>
  );
});
