import {
  AlertTriangle,
  CheckCircle2,
  FlaskConical,
  ShieldCheck,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import {
  buildCandidateExperimentRequest,
  candidateExperimentAuthorizationGate,
  candidateExperimentRequestIdentity,
  candidateExperimentSelectionFingerprint,
  candidateExperimentView,
} from "../candidateExperiment";
import { createLatestRequestCoordinator } from "../latestRequest";


const SCENARIO_LABELS = Object.freeze({
  baseline: "基准摩擦",
  stressed: "压力摩擦",
  severe: "极端摩擦",
});


function percent(value, digits = 1) {
  return typeof value === "number" && Number.isFinite(value)
    ? `${value.toFixed(digits)}%`
    : "—";
}


function ratioPercent(value) {
  return typeof value === "number" && Number.isFinite(value)
    ? percent(value * 100)
    : "—";
}


function shortHash(value) {
  const normalized = String(value || "");
  return normalized.length === 64
    ? `${normalized.slice(0, 8)}…${normalized.slice(-6)}`
    : "—";
}


function displayValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.map(displayValue).filter((item) => item !== "—").join(" / ") || "—";
  if (typeof value === "object") {
    return String(value.label || value.name || value.id || value.version || "—");
  }
  if (typeof value === "boolean") return value ? "是" : "否";
  return String(value);
}


function firstValue(source, keys) {
  for (const key of keys) {
    if (source?.[key] !== null && source?.[key] !== undefined && source?.[key] !== "") {
      return source[key];
    }
  }
  return null;
}


function commonSpecRows(spec, datasetSeal) {
  return [
    ["历史截止", firstValue(spec, ["cutoff_date", "cutoff"])],
    ["复权口径", firstValue(spec, ["price_adjustment"])],
    ["共同交易日历", firstValue(spec, [
      "trading_calendar_sha256",
      "trading_calendar",
      "calendar_id",
    ])],
    ["研究期限", firstValue(spec, ["horizon_days", "test_days"])],
    ["纸面权重", firstValue(spec, ["target_weight_pct", "paper_weight_pct"])],
    ["训练 / 测试 / 步长", [spec?.train_days, spec?.test_days, spec?.step_days]],
    ["历史引擎", firstValue(spec, ["engine_version", "engine"])],
    ["评估规则", firstValue(spec, ["evaluation_rule", "evaluation_rule_id"])],
    ["摩擦集合", firstValue(spec, ["friction_scenario_set", "friction_scenarios_version"])],
    ["不可成交政策", firstValue(spec, ["unfillable_policy"])],
    ["冻结区间", [
      firstValue(datasetSeal, ["actual_start", "start_date"]),
      firstValue(datasetSeal, ["actual_end", "end_date"]),
    ]],
    ["共同交易日", firstValue(datasetSeal, ["common_trading_days", "trading_day_count"])],
  ].filter(([, value]) => displayValue(value) !== "—");
}


function blockerText(blocker) {
  if (!blocker || typeof blocker !== "object") return "服务端记录了不可成交容量阻断。";
  const message = String(
    blocker.message
    || blocker.reason
    || blocker.detail
    || blocker.code
    || "服务端记录了不可成交容量阻断。",
  ).trim();
  const locator = [blocker.symbol, blocker.date].filter(Boolean).join(" · ");
  return locator ? `${message}（${locator}）` : message;
}


function ScenarioMetrics({ scenario }) {
  if (!scenario) return <span className="candidate-experiment-missing">未返回</span>;
  if (scenario.blocked) {
    return (
      <span className="candidate-experiment-capacity-block">
        <strong>容量阻断</strong>
        {scenario.capacityGapUsd !== null
          ? <small>缺口 ${Math.round(scenario.capacityGapUsd).toLocaleString("en-US")} USD</small>
          : null}
        <small>{blockerText(scenario.firstBlocker)}</small>
      </span>
    );
  }
  if (!scenario.metricsVisible) {
    return <span className="candidate-experiment-missing">整组指标已隐藏</span>;
  }
  return (
    <span className="candidate-experiment-metric-stack">
      <small>累计历史收益 <strong>{percent(scenario.cumulativeReturnPct)}</strong></small>
      <small>历史正收益窗 <strong>{ratioPercent(scenario.historicalPositiveWindowRatio)}</strong></small>
      <small>最大回撤 <strong>{percent(scenario.maxDrawdownPct)}</strong></small>
      <small>平均窗口收益 <strong>{percent(scenario.meanWindowReturnPct)}</strong></small>
      <small>最差窗口收益 <strong>{percent(scenario.worstWindowReturnPct)}</strong></small>
    </span>
  );
}


function EvidenceList({ items, emptyLabel }) {
  if (!items.length) return <span className="candidate-experiment-missing">{emptyLabel}</span>;
  return (
    <ul className="candidate-experiment-evidence-list">
      {items.map((item, index) => (
        <li key={`${item.id || item.label}:${index}`}>
          <span>{item.label}</span>
          {item.detail ? <small>{item.detail}</small> : null}
        </li>
      ))}
    </ul>
  );
}


function expectedContext(roomId, artifact, payload) {
  return {
    roomId,
    artifactId: artifact.id,
    artifactVersion: artifact.version,
    clientRequestId: payload.client_request_id,
    attestationSha256: payload.expected_governance_attestation_sha256,
    candidateSelections: payload.candidate_selections,
  };
}


export function CandidateExperimentPanel({ room, artifact, readOnly = false, readOnlyReason = "" }) {
  const roomId = String(room?.id || "");
  const gate = useMemo(
    () => candidateExperimentAuthorizationGate(artifact),
    [artifact],
  );
  const candidateContext = gate.candidates.map((candidate) => (
    `${candidate.id}:${candidate.revision}:${candidate.snapshotSha256}`
  )).join("|");
  const exactContext = `${roomId}|${gate.artifactId}|${gate.artifactVersion}|${gate.attestationSha256}|${candidateContext}`;
  const [selectedCandidateIds, setSelectedCandidateIds] = useState([]);
  const [acknowledged, setAcknowledged] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);
  const coordinatorRef = useRef(null);
  const contextRef = useRef(exactContext);
  const selectionRef = useRef("");
  const requestIdentityRef = useRef({ fingerprint: "", clientRequestId: "" });
  if (!coordinatorRef.current) coordinatorRef.current = createLatestRequestCoordinator();
  contextRef.current = exactContext;
  const selectionFingerprint = candidateExperimentSelectionFingerprint(
    artifact,
    selectedCandidateIds,
  );
  selectionRef.current = selectionFingerprint;
  const view = useMemo(
    () => (result ? candidateExperimentView(result.experiment, result.expected) : null),
    [result],
  );

  useEffect(() => {
    coordinatorRef.current.cancel();
    requestIdentityRef.current = { fingerprint: "", clientRequestId: "" };
    setSelectedCandidateIds([]);
    setAcknowledged(false);
    setError("");
    setResult(null);
    return () => coordinatorRef.current.cancel();
  }, [exactContext, readOnly]);

  useEffect(() => {
    if (requestIdentityRef.current.fingerprint !== selectionFingerprint) {
      requestIdentityRef.current = { fingerprint: "", clientRequestId: "" };
    }
    setAcknowledged(false);
    setError("");
    setResult(null);
  }, [selectionFingerprint]);

  const toggleCandidate = (candidateId) => {
    if (loading || readOnly) return;
    setSelectedCandidateIds((current) => {
      if (current.includes(candidateId)) return current.filter((id) => id !== candidateId);
      if (current.length >= 6) return current;
      return [...current, candidateId];
    });
  };

  const runExperiment = () => {
    if (
      loading
      || readOnly
      || !gate.ready
      || !acknowledged
      || selectedCandidateIds.length < 2
      || selectedCandidateIds.length > 6
      || !selectionFingerprint
    ) return;
    const requestContext = exactContext;
    const requestSelection = selectionFingerprint;
    const requestIdentity = candidateExperimentRequestIdentity(
      requestIdentityRef.current,
      requestSelection,
    );
    requestIdentityRef.current = requestIdentity;
    let payload;
    try {
      payload = buildCandidateExperimentRequest(artifact, {
        candidateIds: selectedCandidateIds,
        clientRequestId: requestIdentity.clientRequestId,
      });
    } catch (requestError) {
      setError(requestError.message);
      setResult(null);
      return;
    }
    const expected = expectedContext(roomId, artifact, payload);
    setError("");
    setResult(null);
    coordinatorRef.current.run({
      request: async (signal) => {
        const created = await api.createCandidateExperiment(roomId, payload, signal);
        const experiment = created?.experiment || {};
        const cohortId = String(experiment.id || "").trim();
        if (
          !cohortId
          || String(experiment.room_id || "") !== roomId
          || String(experiment.artifact_id || "") !== String(artifact.id || "")
          || Number(experiment.artifact_version) !== Number(artifact.version)
          || String(experiment.client_request_id || "") !== payload.client_request_id
        ) {
          throw new Error("联合实验创建响应与本次精确授权上下文不一致，结果已隐藏。");
        }
        const reread = await api.candidateExperiment(roomId, cohortId, signal);
        return { experiment: reread?.experiment || null, expected };
      },
      onSuccess: (nextResult) => {
        if (
          contextRef.current !== requestContext
          || selectionRef.current !== requestSelection
        ) return;
        setResult(nextResult);
      },
      onError: (requestError) => {
        if (
          contextRef.current !== requestContext
          || selectionRef.current !== requestSelection
        ) return;
        setResult(null);
        setError(
          requestError?.status === 409
            ? "实验请求编号已绑定不同语义，服务端已拒绝运行；未自动改号或换算。"
            : requestError.message || "联合实验失败，未展示任何指标。",
        );
      },
      onLoadingChange: setLoading,
    });
  };

  const rows = view?.ready ? commonSpecRows(view.commonSpec, view.datasetSeal) : [];

  return (
    <section className="candidate-experiment-panel" aria-labelledby="candidate-experiment-title">
      <header className="candidate-experiment-heading">
        <span>
          <strong id="candidate-experiment-title"><FlaskConical size={15} />决定前 A/B/C 原子历史实验</strong>
          <small>一次授权，以同一冻结数据和服务端共同规格并列复算当前候选；授权不表示支持任何候选。</small>
        </span>
        <em>{gate.candidates.length} 个精确候选</em>
      </header>

      {readOnly ? (
        <p className="candidate-experiment-readonly" role="note">
          <ShieldCheck size={13} />
          {readOnlyReason || "该产物的冻结插件合同当前仅可查看，不能发起新的历史实验。"}
        </p>
      ) : null}

      {!gate.ready ? (
        <p className="candidate-experiment-empty" role="status">
          <AlertTriangle size={13} />{gate.reason || "当前产物不满足联合实验授权条件。"}
        </p>
      ) : (
        <>
          <fieldset className="candidate-experiment-selector" disabled={loading || readOnly}>
            <legend>选择 2–6 个当前治理候选，列顺序按你的选择顺序保留</legend>
            {gate.candidates.map((candidate) => {
              const selected = selectedCandidateIds.includes(candidate.id);
              const disabled = loading || (!selected && selectedCandidateIds.length >= 6);
              return (
                <label className={selected ? "selected" : ""} key={candidate.id}>
                  <input
                    type="checkbox"
                    checked={selected}
                    disabled={disabled}
                    onChange={() => toggleCandidate(candidate.id)}
                  />
                  <span>
                    <strong>{candidate.title}</strong>
                    <small>
                      {candidate.symbol.replace("US.", "")} · {candidate.direction}
                      {` · r${candidate.revision} · ${candidate.horizonDays} 日`}
                    </small>
                    <small title={candidate.snapshotSha256}>快照 {shortHash(candidate.snapshotSha256)}</small>
                    <small className="candidate-experiment-selector-invalidation">失效条件：{candidate.invalidation}</small>
                  </span>
                </label>
              );
            })}
          </fieldset>
          <label className="candidate-experiment-acknowledgement">
            <input
              type="checkbox"
              checked={acknowledged}
              disabled={loading || readOnly}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />
            <span>
              我授权对当前已确认产物 v{gate.artifactVersion} 的所选精确候选执行一次共同历史口径实验；
              这不是支持决定、未来胜率或交易指令。
            </span>
          </label>
          <button
            className="secondary compact candidate-experiment-run"
            type="button"
            disabled={readOnly || loading || !acknowledged || selectedCandidateIds.length < 2}
            aria-busy={loading}
            onClick={runExperiment}
          >
            <FlaskConical size={13} />
            {loading
              ? "正在原子复算并重读完整性…"
              : view?.ready
                ? `按同一请求编号重新核验 ${selectedCandidateIds.length} 个候选`
                : `运行所选 ${selectedCandidateIds.length} 个候选`}
          </button>
        </>
      )}

      {error ? (
        <p className="candidate-experiment-error" role="alert"><AlertTriangle size={13} />{error}</p>
      ) : null}
      {view && !view.ready ? (
        <div className="candidate-experiment-result blocked" role="alert">
          <AlertTriangle size={15} />
          <span>
            <strong>整组完整性未通过，所有 arm 历史指标已隐藏</strong>
            <small>{view.issues[0]?.message || "输入、arm、结果或聚合封印不一致。"}</small>
          </span>
        </div>
      ) : null}
      {view?.ready ? (
        <div className="candidate-experiment-result ready" aria-live="polite">
          <div className="candidate-experiment-proof">
            <CheckCircle2 size={16} />
            <span>
              <strong>原子 cohort 完整性已通过</strong>
              <small>
                cohort {view.cohortId} · 请求 {view.clientRequestId}
              </small>
              <small>
                数据封印 {shortHash(view.datasetSealSha256)} · 共同规格 {shortHash(view.specSha256)}
              </small>
            </span>
          </div>
          {rows.length ? (
            <dl className="candidate-experiment-common-spec">
              {rows.map(([label, value]) => (
                <div key={label}><dt>{label}</dt><dd>{displayValue(value)}</dd></div>
              ))}
            </dl>
          ) : null}
          <div className="candidate-experiment-table-wrap">
            <table
              className="candidate-experiment-table"
              style={{ minWidth: `${132 + (view.arms.length * 190)}px` }}
            >
              <caption>相同冻结历史数据、服务端共同规格和三档摩擦下的候选并列结果</caption>
              <thead>
                <tr>
                  <th scope="col">只读比较维度</th>
                  {view.arms.map((arm) => (
                    <th scope="col" key={arm.candidateId}>
                      <strong>{arm.title}</strong>
                      <small>{arm.symbol.replace("US.", "")} · {arm.direction}→{arm.side}</small>
                      <small>r{arm.candidateRevision} · 快照 {shortHash(arm.candidateSnapshotSha256)}</small>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                <tr>
                  <th scope="row">研究论点</th>
                  {view.arms.map((arm) => <td key={arm.candidateId}>{arm.thesis}</td>)}
                </tr>
                <tr>
                  <th scope="row">支持证据</th>
                  {view.arms.map((arm) => (
                    <td key={arm.candidateId}><EvidenceList items={arm.evidence} emptyLabel="未返回支持证据" /></td>
                  ))}
                </tr>
                <tr>
                  <th scope="row">反证 / 限制</th>
                  {view.arms.map((arm) => (
                    <td key={arm.candidateId}><EvidenceList items={arm.counterevidence} emptyLabel="未返回反证记录" /></td>
                  ))}
                </tr>
                <tr>
                  <th scope="row">失效条件</th>
                  {view.arms.map((arm) => <td key={arm.candidateId}>{arm.invalidation}</td>)}
                </tr>
                {Object.entries(SCENARIO_LABELS).map(([scenarioId, label]) => (
                  <tr key={scenarioId}>
                    <th scope="row">{label}</th>
                    {view.arms.map((arm) => (
                      <td key={arm.candidateId}>
                        <ScenarioMetrics scenario={arm.scenarios.find((scenario) => scenario.id === scenarioId)} />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="candidate-experiment-safety" role="note">
            <ShieldCheck size={14} />
            不产生排名、赢家、未来胜率或自动决定；不会修改下方用户最终决定，用户仍可选择任一有效候选。
          </p>
        </div>
      ) : null}
    </section>
  );
}
