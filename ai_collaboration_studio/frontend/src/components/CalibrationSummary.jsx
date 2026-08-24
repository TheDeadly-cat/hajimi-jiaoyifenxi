import { memo } from "react";
import { calibrationMetricGate, canShowBrierScore } from "../calibrationGate";

function formatPercent(value, digits = 1, signed = false) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${signed && number > 0 ? "+" : ""}${number.toFixed(digits)}%`;
}

function hasFiniteNumber(value) {
  return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
}

function nonNegativeCount(value) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? Math.floor(number) : 0;
}


function ScoreCell({ label, value, detail = "" }) {
  return <span><small>{label}</small><strong>{value}</strong>{detail ? <em>{detail}</em> : null}</span>;
}


export const CalibrationSummary = memo(function CalibrationSummary({ scorecard }) {
  const overall = scorecard?.overall || {};
  const peer = overall.peer_relative || {};
  const last20 = scorecard?.rolling?.last_20 || {};
  const last50 = scorecard?.rolling?.last_50 || {};
  const overallGate = calibrationMetricGate(overall);
  const peerGate = calibrationMetricGate(peer);
  const last20Gate = calibrationMetricGate(last20);
  const last50Gate = calibrationMetricGate(last50);
  const independence = scorecard?.independence || {};
  const scoringPopulation = scorecard?.scoring_population || {};
  const lineageExcluded = nonNegativeCount(scoringPopulation.excluded_from_scoring_count);
  const methodologyRows = Object.entries(scorecard?.by_methodology || {});
  const comparisonRows = Object.entries(scorecard?.by_comparison_group || {});
  const agentMethodologyRows = Array.isArray(scorecard?.by_agent_methodology) ? scorecard.by_agent_methodology : [];
  const confidenceRows = Array.isArray(scorecard?.confidence_calibration) ? scorecard.confidence_calibration : [];
  const populatedBands = confidenceRows.filter((row) => row.sample_count > 0);
  return (
    <>
      <div className="observation-score three-column">
        <ScoreCell
          label={overall.metric_label || (overallGate.qualified ? "统计胜率" : "独立样本")}
          value={overallGate.qualified ? formatPercent(overall.hit_rate_pct) : overallGate.progressLabel}
          detail={overallGate.qualified
            ? `95% 区间 ${formatPercent(overall.wilson_low_pct)}–${formatPercent(overall.wilson_high_pct)} · ${lineageExcluded} 条未通过决策谱系未计分`
            : overall.mixed_conditions
              ? `${overall.comparison_group_count || 0} 组不同标的/周期/方向/阈值，不合并称为胜率`
              : `${independence.excluded_count || 0} 条重复/重叠未计入 · ${lineageExcluded} 条未通过决策谱系未计分`}
        />
        <ScoreCell
          label={peerGate.qualified ? "同行相对胜率" : "同行样本"}
          value={peerGate.qualified ? formatPercent(peer.hit_rate_pct) : peerGate.progressLabel}
          detail={peerGate.qualified && hasFiniteNumber(peer.average_relative_return_pct) ? `平均 ${formatPercent(peer.average_relative_return_pct, 1, true)}` : ""}
        />
        <ScoreCell
          label="近 20 次"
          value={last20Gate.qualified ? formatPercent(last20.hit_rate_pct) : last20Gate.progressLabel}
          detail={last20Gate.qualified ? `${last20Gate.sampleCount} 个样本` : `${last20Gate.reason}，暂不显示命中率`}
        />
      </div>
      {(last50.sample_count || populatedBands.length || methodologyRows.length || comparisonRows.length || agentMethodologyRows.length) ? (
        <details className="calibration-details">
          <summary>独立样本、AI 身份与校准</summary>
          <div className="rolling-score-row">
            <span><small>近20</small><strong>{last20Gate.qualified ? formatPercent(last20.hit_rate_pct) : last20Gate.progressLabel}</strong><em>{last20Gate.qualified ? `${last20Gate.sampleCount} 样本` : last20Gate.reason}</em></span>
            <span><small>近50</small><strong>{last50Gate.qualified ? formatPercent(last50.hit_rate_pct) : last50Gate.progressLabel}</strong><em>{last50Gate.qualified ? `${last50Gate.sampleCount} 样本` : last50Gate.reason}</em></span>
          </div>
          {agentMethodologyRows.length ? <>
            <h4 className="calibration-subheading">AI 身份版本 × 观察方法</h4>
            <div className="calibration-band-list agent-calibration-list">
              {agentMethodologyRows.slice(0, 12).map((row) => {
                const gate = calibrationMetricGate(row);
                const showBrier = canShowBrierScore(row);
                return (
                  <span
                    key={`${row.member_id}@${row.member_version}:${row.methodology_id}@${row.methodology_version}`}
                  >
                    <small>{row.member_name || "历史成员"} · 身份 v{row.member_version || "?"}</small>
                    <strong>{gate.qualified ? formatPercent(row.hit_rate_pct) : gate.progressLabel}</strong>
                    <em>{row.methodology_id || "方法未记录"}@v{row.methodology_version || "?"}{showBrier && hasFiniteNumber(row.brier_score) ? ` · Brier ${Number(row.brier_score).toFixed(3)}` : ` · ${gate.qualified ? "AI 置信样本不足" : gate.reason}`}</em>
                    <em className="calibration-agent-provenance">{row.identity || "历史 AI 身份"} · {row.provider || "执行器未记录"} / {row.model || "模型未记录"}</em>
                  </span>
                );
              })}
            </div>
            {agentMethodologyRows.length > 12 ? <p className="calibration-projection-note">当前摘要展示前 12 个 AI 身份与观察方法组合，另有 {agentMethodologyRows.length - 12} 个未展开。</p> : null}
          </> : null}
          {methodologyRows.length ? <div className="calibration-band-list">
            {methodologyRows.map(([key, row]) => {
              const gate = calibrationMetricGate(row);
              const showBrier = canShowBrierScore(row);
              return (
                <span key={key}>
                  <small>{key}</small>
                  <strong>{gate.qualified ? formatPercent(row.hit_rate_pct) : gate.progressLabel}</strong>
                  <em>{showBrier && hasFiniteNumber(row.brier_score) ? `Brier ${Number(row.brier_score).toFixed(3)}` : gate.qualified ? "AI 置信样本不足" : gate.reason}</em>
                </span>
              );
            })}
          </div> : null}
          {comparisonRows.length ? <>
            <h4 className="calibration-subheading">可直接比较的固定条件组</h4>
            <div className="calibration-band-list">
              {comparisonRows.slice(0, 12).map(([key, row]) => {
                const gate = calibrationMetricGate(row);
                return (
                  <span key={key} title={key}>
                    <small>{key}</small>
                    <strong>{gate.qualified ? formatPercent(row.hit_rate_pct) : gate.progressLabel}</strong>
                    <em>{gate.qualified ? `95% ${formatPercent(row.wilson_low_pct)}–${formatPercent(row.wilson_high_pct)}` : gate.reason}</em>
                  </span>
                );
              })}
            </div>
            {comparisonRows.length > 12 ? <p className="calibration-projection-note">当前摘要展示前 12 个固定条件组，另有 {comparisonRows.length - 12} 个未展开。</p> : null}
          </> : null}
          {populatedBands.length ? <div className="calibration-band-list">
            {populatedBands.map((row) => {
              const gate = calibrationMetricGate(row);
              return (
                <span key={row.band}>
                  <small>置信 {row.band}</small>
                  <strong>{gate.qualified ? formatPercent(row.hit_rate_pct) : gate.progressLabel}</strong>
                  <em>{gate.qualified ? `${gate.sampleCount} 样本 · 差值 ${formatPercent(row.calibration_gap_pp, 1, true)}` : `${gate.reason}，暂不显示命中率与校准差`}</em>
                </span>
              );
            })}
          </div> : null}
          <p>{independence.definition || "只统计不重叠的独立观察窗口。"} AI 身份版本或方法变化后分开计分；用户输入的置信度不进入 AI Brier 校准；每组少于 20 个样本时只显示样本量。</p>
        </details>
      ) : null}
    </>
  );
});
