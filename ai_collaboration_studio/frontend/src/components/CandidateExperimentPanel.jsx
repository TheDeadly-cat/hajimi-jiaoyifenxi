import {
  AlertTriangle,
  CheckCircle2,
  FlaskConical,
  ShieldCheck,
} from "lucide-react";
import { memo, useEffect, useId, useMemo, useRef, useState } from "react";
import { api } from "../api";
import {
  buildCandidateExperimentRequest,
  candidateExperimentAuthorizationGate,
  candidateExperimentControlState,
  candidateExperimentErrorMessage,
  candidateExperimentRequestIdentity,
  candidateExperimentSelectionFingerprint,
  candidateExperimentView,
} from "../candidateExperiment";
import { createLatestRequestCoordinator } from "../latestRequest";
import "../styles/candidate-experiment.css";
import "../styles/candidate-experiment-refinement.css";


const SCENARIO_LABELS = Object.freeze({
  baseline: "基准摩擦",
  stressed: "压力摩擦",
  severe: "极端摩擦",
});
const SCENARIO_ENTRIES = Object.freeze(Object.entries(SCENARIO_LABELS));
const CANDIDATE_SELECTOR_PAGE_SIZE = 24;
const TABLE_ROW_HEADER_WIDTH = 148;
const TABLE_ARM_WIDTH = 210;


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
  const normalized = String(value || "").trim();
  return /^[0-9a-f]{64}$/i.test(normalized)
    ? `${normalized.slice(0, 8)}…${normalized.slice(-6)}`
    : "—";
}


function formatSymbol(value) {
  const normalized = typeof value === "string" ? value.trim().slice(0, 48) : "";
  return normalized ? normalized.replace(/^US\./, "") : "—";
}


function displayValue(value, depth = 0) {
  if (value === null || value === undefined || value === "") return "—";
  if (depth > 1) return "—";
  if (Array.isArray(value)) {
    return value
      .slice(0, 8)
      .map((item) => displayValue(item, depth + 1))
      .filter((item) => item !== "—")
      .join(" / ") || "—";
  }
  if (typeof value === "object") {
    for (const key of ["label", "name", "id", "version"]) {
      const candidate = value?.[key];
      if (typeof candidate === "string" || typeof candidate === "number") {
        return displayValue(candidate, depth + 1);
      }
    }
    return "—";
  }
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "—";
  return typeof value === "string" ? value.trim().slice(0, 400) || "—" : "—";
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


const ScenarioMetrics = memo(function ScenarioMetrics({ scenario }) {
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
});


const EvidenceList = memo(function EvidenceList({ items, emptyLabel }) {
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
});


function expectedContext(roomId, payload) {
  return {
    roomId,
    artifactId: payload.artifact_id,
    artifactVersion: payload.expected_artifact_version,
    clientRequestId: payload.client_request_id,
    attestationSha256: payload.expected_governance_attestation_sha256,
    candidateSelections: payload.candidate_selections,
  };
}


export const CandidateExperimentPanel = memo(function CandidateExperimentPanel({
  room,
  artifact,
  readOnly = false,
  readOnlyReason = "",
}) {
  const titleId = useId();
  const instructionId = useId();
  const tableHintId = useId();
  const resultTitleId = useId();
  const roomId = typeof room?.id === "string" ? room.id.trim().slice(0, 240) : "";
  const gate = useMemo(
    () => candidateExperimentAuthorizationGate(artifact),
    [artifact],
  );
  const runtimeGateReady = gate.ready && Boolean(roomId);
  const exactContext = useMemo(() => JSON.stringify([
      roomId,
      gate.artifactId,
      gate.artifactVersion,
      gate.attestationSha256,
      gate.candidates.map((candidate) => [
        candidate.id,
        candidate.revision,
        candidate.snapshotSha256,
      ]),
    ]), [gate, roomId]);
  const [selectedCandidateIds, setSelectedCandidateIds] = useState([]);
  const [candidateLimit, setCandidateLimit] = useState(CANDIDATE_SELECTOR_PAGE_SIZE);
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
  const selectionFingerprint = useMemo(
    () => candidateExperimentSelectionFingerprint(artifact, selectedCandidateIds),
    [artifact, selectedCandidateIds],
  );
  selectionRef.current = selectionFingerprint;
  const selectedCandidateSet = useMemo(
    () => new Set(selectedCandidateIds),
    [selectedCandidateIds],
  );
  const visibleCandidates = useMemo(
    () => gate.candidates.filter((candidate, index) => (
      index < candidateLimit || selectedCandidateSet.has(candidate.id)
    )),
    [candidateLimit, gate.candidates, selectedCandidateSet],
  );
  const hiddenCandidateCount = gate.candidates.length - visibleCandidates.length;
  const view = useMemo(
    () => (result ? candidateExperimentView(result.experiment, result.expected) : null),
    [result],
  );
  const controls = useMemo(
    () => candidateExperimentControlState({
      gateReady: runtimeGateReady,
      readOnly,
      loading,
      acknowledged,
      selectedCandidateIds,
      selectionFingerprint,
      resultReady: view?.ready === true,
    }),
    [
      acknowledged,
      loading,
      readOnly,
      runtimeGateReady,
      selectedCandidateIds,
      selectionFingerprint,
      view?.ready,
    ],
  );

  useEffect(() => {
    coordinatorRef.current.cancel();
    requestIdentityRef.current = { fingerprint: "", clientRequestId: "" };
    setSelectedCandidateIds([]);
    setCandidateLimit(CANDIDATE_SELECTOR_PAGE_SIZE);
    setAcknowledged(false);
    setError("");
    setResult(null);
    return () => coordinatorRef.current.cancel();
  }, [exactContext, readOnly]);

  useEffect(() => {
    coordinatorRef.current.cancel();
    if (requestIdentityRef.current.fingerprint !== selectionFingerprint) {
      requestIdentityRef.current = { fingerprint: "", clientRequestId: "" };
    }
    setAcknowledged(false);
    setError("");
    setResult(null);
  }, [selectionFingerprint]);

  const toggleCandidate = (candidateId) => {
    if (loading || readOnly || !runtimeGateReady) return;
    setSelectedCandidateIds((current) => {
      if (current.includes(candidateId)) return current.filter((id) => id !== candidateId);
      if (current.length >= 6) return current;
      return [...current, candidateId];
    });
  };

  const runExperiment = () => {
    if (!controls.canRun || coordinatorRef.current.inFlight) return;
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
      setError(candidateExperimentErrorMessage(requestError, "无法构造精确候选实验请求。"));
      setResult(null);
      return;
    }
    const expected = expectedContext(roomId, payload);
    setError("");
    setResult(null);
    coordinatorRef.current.run({
      request: async (signal) => {
        const created = await api.createCandidateExperiment(roomId, payload, signal);
        const experiment = created?.experiment || {};
        const cohortId = typeof experiment.id === "string" ? experiment.id.trim().slice(0, 240) : "";
        if (
          !cohortId
          || (typeof experiment.room_id !== "string" ? "" : experiment.room_id.trim()) !== roomId
          || (typeof experiment.artifact_id !== "string" ? "" : experiment.artifact_id.trim()) !== payload.artifact_id
          || !Number.isSafeInteger(experiment.artifact_version)
          || experiment.artifact_version !== payload.expected_artifact_version
          || (typeof experiment.client_request_id !== "string" ? "" : experiment.client_request_id.trim()) !== payload.client_request_id
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
              : candidateExperimentErrorMessage(requestError),
        );
      },
      onLoadingChange: setLoading,
    });
  };

  const rows = useMemo(
    () => (view?.ready ? commonSpecRows(view.commonSpec, view.datasetSeal) : []),
    [view],
  );

  return (
    <section
      className="candidate-experiment-panel candidate-experiment-workbench"
      data-control-phase={controls.phase}
      aria-labelledby={titleId}
      aria-busy={loading}
    >
      <header className="candidate-experiment-heading">
        <div className="candidate-experiment-heading-copy">
          <small className="candidate-experiment-kicker">CONTROLLED HISTORICAL LAB</small>
          <strong id={titleId}><FlaskConical size={17} aria-hidden="true" />决定前 A/B/C 原子历史实验</strong>
          <p>一次授权，以同一冻结数据和服务端共同规格并列复算当前候选；授权不表示支持任何候选。</p>
        </div>
        <em><span>SEALED SET</span>{gate.candidates.length} 个精确候选</em>
      </header>

      <ol className="candidate-experiment-workflow" aria-label="候选实验四阶段">
        {controls.steps.map((step) => (
          <li
            key={step.key}
            data-step-state={step.status}
            aria-current={step.status === "active" ? "step" : undefined}
          >
            <b aria-hidden="true">{step.order}</b>
            <span>{step.label}</span>
            <em>
              {step.status === "complete"
                ? "完成"
                : step.status === "active"
                  ? "当前"
                  : step.status === "locked"
                    ? "锁定"
                    : "待处理"}
            </em>
          </li>
        ))}
      </ol>

      {readOnly ? (
        <p className="candidate-experiment-readonly" role="note">
          <ShieldCheck size={13} aria-hidden="true" />
          {readOnlyReason || "该产物的冻结插件合同当前仅可查看，不能发起新的历史实验。"}
        </p>
      ) : null}

      {!runtimeGateReady ? (
        <p className="candidate-experiment-empty" role="status">
          <AlertTriangle size={13} aria-hidden="true" />
          {!roomId ? "房间身份缺失，不能创建或读取候选历史实验。" : gate.reason || "当前产物不满足联合实验授权条件。"}
        </p>
      ) : (
        <>
          <fieldset
            className="candidate-experiment-selector"
            disabled={loading || readOnly}
            aria-describedby={instructionId}
          >
            <legend>选择 2–6 个当前治理候选，列顺序按你的选择顺序保留</legend>
            {visibleCandidates.map((candidate) => {
              const selected = selectedCandidateIds.includes(candidate.id);
              const selectionOrder = selectedCandidateIds.indexOf(candidate.id) + 1;
              const disabled = loading || (!selected && selectedCandidateIds.length >= 6);
              return (
                <label
                  className={selected ? "selected" : ""}
                  data-selection-order={selected ? selectionOrder : undefined}
                  key={candidate.id}
                >
                  <input
                    type="checkbox"
                    checked={selected}
                    disabled={disabled}
                    onChange={() => toggleCandidate(candidate.id)}
                  />
                  <b className="candidate-experiment-order" aria-hidden="true">
                    {selected ? String(selectionOrder).padStart(2, "0") : "·"}
                  </b>
                  <span>
                    <strong>{candidate.title}</strong>
                    <small>
                      {formatSymbol(candidate.symbol)} · {candidate.direction}
                      {` · r${candidate.revision} · ${candidate.horizonDays} 日`}
                    </small>
                    <small title={candidate.snapshotSha256}>快照 {shortHash(candidate.snapshotSha256)}</small>
                    <small className="candidate-experiment-selector-invalidation">失效条件：{candidate.invalidation}</small>
                  </span>
                </label>
              );
            })}
          </fieldset>
          <p className="candidate-experiment-candidate-status" role="status" aria-live="polite">
            展示 {visibleCandidates.length} / {gate.candidates.length} 个精确治理候选；所有已选候选始终可见。
          </p>
          {hiddenCandidateCount ? (
            <button
              type="button"
              className="secondary compact candidate-experiment-more"
              disabled={loading || readOnly}
              onClick={() => setCandidateLimit((current) => current + CANDIDATE_SELECTOR_PAGE_SIZE)}
            >
              再显示 {Math.min(CANDIDATE_SELECTOR_PAGE_SIZE, hiddenCandidateCount)} 个候选
            </button>
          ) : null}
          <div
            className="candidate-experiment-selection-status"
            data-selection-state={controls.selectionReady ? "ready" : "incomplete"}
            id={instructionId}
            role="status"
            aria-live="polite"
          >
            <span><strong>{controls.selectedCount}/6</strong><small>已选候选</small></span>
            <p>{controls.instruction}</p>
          </div>
          <label className="candidate-experiment-acknowledgement">
            <input
              type="checkbox"
              checked={acknowledged}
              disabled={!controls.canAcknowledge}
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
            disabled={!controls.canRun}
            aria-busy={loading}
            aria-describedby={instructionId}
            onClick={runExperiment}
          >
            <FlaskConical size={13} aria-hidden="true" />
            {controls.actionLabel}
          </button>
        </>
      )}

      {error ? (
        <p className="candidate-experiment-error" role="alert"><AlertTriangle size={13} aria-hidden="true" />{error}</p>
      ) : null}
      {view && !view.ready ? (
        <div className="candidate-experiment-result blocked" role="alert">
          <AlertTriangle size={15} aria-hidden="true" />
          <span>
            <strong>整组完整性未通过，所有 arm 历史指标已隐藏</strong>
            <small>{view.issues?.[0]?.message || "输入、arm、结果或聚合封印不一致。"}</small>
          </span>
        </div>
      ) : null}
      {view?.ready ? (
        <div
          className="candidate-experiment-result ready"
          role="region"
          aria-labelledby={resultTitleId}
        >
          <header className="candidate-experiment-result-heading">
            <div className="candidate-experiment-proof">
              <CheckCircle2 size={17} aria-hidden="true" />
              <span>
                <small role="status" aria-live="polite">SEALED COHORT / INTEGRITY PASS</small>
                <strong id={resultTitleId}>原子 cohort 完整性已通过</strong>
                <span className="candidate-experiment-proof-boundary">仅核验本次封印输入与历史结果，不表示候选获得支持。</span>
                <span>cohort {view.cohortId} · 请求 {view.clientRequestId}</span>
                <span>数据封印 {shortHash(view.datasetSealSha256)} · 共同规格 {shortHash(view.specSha256)}</span>
              </span>
            </div>
            <em><span>SEALED ARMS</span>{String(view.arms.length).padStart(2, "0")}</em>
          </header>
          <div className="candidate-experiment-neutrality-ledger" role="list" aria-label="实验中立性边界">
            <span role="listitem"><small>封印候选</small><strong>{view.arms.length}</strong></span>
            <span role="listitem"><small>共同摩擦情景</small><strong>{SCENARIO_ENTRIES.length}</strong></span>
            <span role="listitem"><small>排名 / 赢家</small><strong>均不产生</strong></span>
          </div>
          {rows.length ? (
            <dl className="candidate-experiment-common-spec">
              {rows.map(([label, value]) => (
                <div key={label}><dt>{label}</dt><dd>{displayValue(value)}</dd></div>
              ))}
            </dl>
          ) : null}
          <ol className="candidate-experiment-arm-index" aria-label="候选比较顺序">
            {view.arms.map((arm, index) => (
              <li key={arm.candidateId}>
                <b aria-hidden="true">{String(index + 1).padStart(2, "0")}</b>
                <span>
                  <strong>{arm.title}</strong>
                  <small>{formatSymbol(arm.symbol)} · {arm.direction}→{arm.side}</small>
                </span>
                <code>r{arm.candidateRevision}</code>
              </li>
            ))}
          </ol>
          <p className="candidate-experiment-table-hint" id={tableHintId}>
            比较表是独立横向滚动区域；候选列严格保留你的选择顺序，不按历史结果排序。
          </p>
          <div
            className="candidate-experiment-table-wrap"
            role="region"
            aria-label="候选历史实验比较表"
            aria-describedby={tableHintId}
            tabIndex={0}
          >
            <table
              className="candidate-experiment-table"
              style={{ minWidth: `${TABLE_ROW_HEADER_WIDTH + (view.arms.length * TABLE_ARM_WIDTH)}px` }}
            >
              <caption>相同冻结历史数据、服务端共同规格和三档摩擦下的候选并列结果</caption>
              <thead>
                <tr>
                  <th scope="col">只读比较维度</th>
                  {view.arms.map((arm) => (
                    <th scope="col" key={arm.candidateId}>
                      <strong>{arm.title}</strong>
                      <small>{formatSymbol(arm.symbol)} · {arm.direction}→{arm.side}</small>
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
                {SCENARIO_ENTRIES.map(([scenarioId, label]) => (
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
            <ShieldCheck size={14} aria-hidden="true" />
            不产生排名、赢家、未来胜率或自动决定；不会修改下方用户最终决定，用户仍可选择任一有效候选。
          </p>
        </div>
      ) : null}
    </section>
  );
});
