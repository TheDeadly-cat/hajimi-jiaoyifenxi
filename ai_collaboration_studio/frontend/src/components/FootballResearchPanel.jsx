import {
  AlertTriangle,
  ChevronDown,
  Database,
  FileJson2,
  LoaderCircle,
  MapPin,
  Search,
  ShieldCheck,
  Upload,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import "../styles/football-research.css";
import { api } from "../api.js";
import {
  FOOTBALL_EVIDENCE_CLASSES,
  buildFootballRoundContextAuthorization,
  footballEvidenceClaims,
  footballEvidenceValue,
  footballRoundContextAuthorizationState,
  normalizeFootballResearchResponse,
  parseFootballResearchJson,
} from "../footballResearch.js";

const EVIDENCE_LABELS = Object.freeze({
  official_fact: "官方事实",
  media_report: "媒体信息",
  model_inference: "模型推断",
  odds_proxy: "赔率代理",
});

function initialRequestState() {
  return { status: "idle", view: null, error: "" };
}

function textValue(value, fallback = "未提供") {
  if (typeof value === "string") return value.trim() || fallback;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return fallback;
}

function evidenceText(field, fallback = "未提供") {
  return textValue(footballEvidenceValue(field), fallback);
}

function evidenceNumber(field) {
  const value = footballEvidenceValue(field);
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function listValue(field) {
  const value = footballEvidenceValue(field, []);
  return Array.isArray(value) ? value : [];
}

function objectValue(field) {
  const value = footballEvidenceValue(field, {});
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function publicationText(field) {
  const publication = field?.source?.publication || {};
  if (publication.state === "published") {
    return `已发布 · ${textValue(publication.published_at_utc, "时间未提供")}`;
  }
  if (publication.state === "observed") {
    return `已观测 · ${textValue(publication.observed_at_utc, "时间未提供")}`;
  }
  return `未发布 · 截止 ${textValue(field?.as_of_utc, "时间未提供")}`;
}

function EvidenceBadge({ field }) {
  const evidenceClass = textValue(field?.evidence_class, "unknown");
  return (
    <span className={`football-evidence-badge ${evidenceClass}`}>
      {EVIDENCE_LABELS[evidenceClass] || "未分类"}
    </span>
  );
}

function MatchFacts({ contract }) {
  const match = contract.match_identity || {};
  return (
    <section className="football-match-card" aria-label="比赛封印">
      <header>
        <span><ShieldCheck size={16} /><strong>比赛身份已封印</strong></span>
        <small>所有时间均保持 UTC 原文</small>
      </header>
      <dl className="football-match-facts">
        <div><dt>联赛</dt><dd>{evidenceText(match.competition)}</dd></div>
        <div><dt>联赛 ID</dt><dd>{evidenceText(match.competition_id)}</dd></div>
        <div><dt>赛季</dt><dd>{evidenceText(match.season)}</dd></div>
        <div><dt>比赛 ID</dt><dd>{evidenceText(match.match_id)}</dd></div>
        <div><dt>开球 UTC</dt><dd>{evidenceText(match.kickoff_utc)}</dd></div>
        <div><dt>场地</dt><dd><MapPin size={13} />{evidenceText(match.venue)}</dd></div>
        <div><dt>场地 ID</dt><dd>{evidenceText(match.venue_id)}</dd></div>
        <div className="wide"><dt>数据截止 UTC</dt><dd>{textValue(contract.data_cutoff_utc)}</dd></div>
      </dl>
    </section>
  );
}

function FixtureHistory({ field }) {
  const fixtures = listValue(field);
  if (!fixtures.length) return <p className="football-empty">没有已封印赛程。</p>;
  return (
    <ol className="football-fixture-list">
      {fixtures.map((fixture) => (
        <li key={`${fixture.match_id}:${fixture.kickoff_utc}`}>
          <span><strong>{textValue(fixture.match_id)}</strong><small>{textValue(fixture.kickoff_utc)}</small></span>
          <span><em>{fixture.role === "home" ? "主场" : fixture.role === "away" ? "客场" : textValue(fixture.role)}</em><small>{textValue(fixture.venue?.venue_name)}</small></span>
        </li>
      ))}
    </ol>
  );
}

function AvailabilityRow({ label, field }) {
  const value = objectValue(field);
  const entries = Array.isArray(value.entries)
    ? value.entries
    : Array.isArray(value.players)
      ? value.players
      : [];
  return (
    <article className="football-availability-row">
      <header>
        <span><strong>{label}</strong><EvidenceBadge field={field} /></span>
        <small>{publicationText(field)}</small>
      </header>
      {entries.length ? (
        <ul>
          {entries.map((entry, index) => (
            <li key={`${entry.player_id || entry.player_name || label}:${index}`}>
              <strong>{textValue(entry.player_name || entry.player_id)}</strong>
              <span>{textValue(entry.selection_status || entry.status, "状态未提供")}</span>
              {entry.detail ? <small>{entry.detail}</small> : null}
            </li>
          ))}
        </ul>
      ) : <p>已明确封印为空；不把“未发布”解释为确认缺席。</p>}
    </article>
  );
}

function TeamResearchCard({ side, team }) {
  const schedule = team.schedule_context || {};
  const travel = objectValue(schedule.travel);
  const last7 = objectValue(schedule.fixtures_last_7d);
  const last14 = objectValue(schedule.fixtures_last_14d);
  const restHours = evidenceNumber(schedule.rest_hours_before_kickoff);
  const sequence = listValue(schedule.home_away_sequence);
  const tactics = listValue(team.tactical_context);
  const recent = team.recent_performance || {};
  const results = listValue(recent.results_sequence);
  const notes = listValue(recent.performance_notes);
  return (
    <details className="football-team-card" open>
      <summary>
        <span><strong>{evidenceText(team.team_name)}</strong><small>{side === "home" ? "主队" : "客队"} · {evidenceText(team.team_id)}</small></span>
        <ChevronDown size={16} />
      </summary>
      <div className="football-team-body">
        <section>
          <h4>赛程密度、旅行与主客场</h4>
          <div className="football-density-grid">
            <span><small>近 7 天</small><strong>{Number(last7.count) || 0} 场</strong></span>
            <span><small>近 14 天</small><strong>{Number(last14.count) || 0} 场</strong></span>
            <span><small>开球前休息</small><strong>{restHours === null ? "未提供" : `${restHours} 小时`}</strong></span>
          </div>
          <div className="football-travel-line">
            <MapPin size={14} />
            <span>
              <strong>{textValue(travel.origin?.venue_name)} → {textValue(travel.destination?.venue_name)}</strong>
              <small>{Number.isFinite(travel.distance_km) ? `${travel.distance_km} km` : "距离未提供"} · {textValue(travel.method, "方法未提供")}</small>
            </span>
            <EvidenceBadge field={schedule.travel} />
          </div>
          <p className="football-role-sequence">
            主客场序列：{sequence.length
              ? sequence.map((item) => `${item.match_id} ${item.role === "home" ? "主" : "客"}`).join(" · ")
              : "未提供"}
          </p>
          <FixtureHistory field={schedule.fixture_history} />
        </section>

        <section>
          <h4>阵容、伤停与停赛发布时间</h4>
          <AvailabilityRow label="阵容" field={team.availability?.lineup} />
          <AvailabilityRow label="伤停" field={team.availability?.injuries} />
          <AvailabilityRow label="停赛" field={team.availability?.suspensions} />
        </section>

        <section>
          <h4>战术与近期表现</h4>
          <div className="football-research-notes">
            <strong>战术上下文 <EvidenceBadge field={team.tactical_context} /></strong>
            {tactics.length ? <ul>{tactics.map((note, index) => <li key={`${index}:${note}`}>{note}</li>)}</ul> : <p>未提供</p>}
          </div>
          <div className="football-recent-results">
            {results.map((item) => <span key={item.match_id}><small>{item.match_id}</small><strong>{item.result}</strong></span>)}
          </div>
          {notes.length ? (
            <ul className="football-performance-notes">
              {notes.map((item) => <li key={item.match_id}><strong>{item.match_id}</strong><span>{item.note}</span></li>)}
            </ul>
          ) : <p className="football-empty">没有近期表现备注。</p>}
        </section>
      </div>
    </details>
  );
}

function EvidenceClassGrid({ claims }) {
  const grouped = useMemo(() => Object.fromEntries(
    FOOTBALL_EVIDENCE_CLASSES.map((evidenceClass) => [
      evidenceClass,
      claims.filter((claim) => claim.evidenceClass === evidenceClass),
    ]),
  ), [claims]);
  return (
    <section className="football-evidence-section" aria-label="证据分类">
      <header><strong>证据类别严格分离</strong><small>赔率只作为观测代理，不转换为未来胜率</small></header>
      <div className="football-evidence-grid">
        {FOOTBALL_EVIDENCE_CLASSES.map((evidenceClass) => {
          const rows = grouped[evidenceClass];
          return (
            <details key={evidenceClass} className={`football-evidence-class ${evidenceClass}`}>
              <summary><strong>{EVIDENCE_LABELS[evidenceClass]}</strong><span>{rows.length} 条</span></summary>
              {rows.length ? (
                <ul>
                  {rows.map((claim) => (
                    <li key={`${claim.claimId}:${claim.path}`}>
                      <strong>{claim.claimId}</strong>
                      <small>{claim.publicationState || "状态未提供"} · {claim.publishedAtUtc || claim.observedAtUtc || claim.asOfUtc}</small>
                      <span>{claim.publisher || "来源未提供"} · {claim.materialId || "材料未绑定"}{claim.materialVersion ? ` v${claim.materialVersion}` : ""}</span>
                    </li>
                  ))}
                </ul>
              ) : <p>该类别没有已封印声明。</p>}
            </details>
          );
        })}
      </div>
    </section>
  );
}

function FixedSafetyBoundary({ view }) {
  const raw = view.raw;
  return (
    <section className="football-safety-boundary" aria-label="足球只读安全边界">
      <header><ShieldCheck size={16} /><strong>固定只读边界</strong></header>
      <code>future_probability_available=false</code>
      <code>probability_metrics_visible=false</code>
      <code>odds_are_proxy_only=true</code>
      <code>betting_allowed=false</code>
      <code>automatic_betting_allowed=false</code>
      <code>wallet_connection_allowed=false</code>
      <code>order_placement_allowed=false</code>
      <p>
        执行能力：{raw.execution_capability}。不投注、不连接钱包、不自动下注，
        不生成未经真实校准的未来胜率，也不替代用户最终决定。
      </p>
    </section>
  );
}

export function FootballResearchPanel({
  room,
  activation,
  roundContextAuthorization = null,
  onRoundContextAuthorizationChange,
}) {
  const [expanded, setExpanded] = useState(false);
  const [source, setSource] = useState("");
  const [requestState, setRequestState] = useState(initialRequestState);
  const requestRef = useRef(null);
  const roomId = String(room?.id || "");

  useEffect(() => () => requestRef.current?.abort(), []);
  useEffect(() => {
    requestRef.current?.abort();
    setExpanded(false);
    setSource("");
    setRequestState(initialRequestState());
  }, [roomId]);

  if (!activation?.visible) return null;
  const active = activation.active === true;

  const updateSource = (value) => {
    if (roundContextAuthorization && typeof onRoundContextAuthorizationChange === "function") {
      onRoundContextAuthorizationChange(null);
    }
    setSource(value);
    setRequestState(initialRequestState());
  };

  const importJson = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (file.size > 1_000_000) {
      setRequestState({ status: "error", view: null, error: "JSON 文件超过 1 MB 只读检查上限。" });
      return;
    }
    try {
      updateSource(await file.text());
    } catch {
      setRequestState({ status: "error", view: null, error: "无法在本地读取这个 JSON 文件。" });
    }
  };

  const inspect = async () => {
    if (!active || !roomId || requestState.status === "loading") return;
    if (roundContextAuthorization && typeof onRoundContextAuthorizationChange === "function") {
      onRoundContextAuthorizationChange(null);
    }
    let payload;
    try {
      payload = parseFootballResearchJson(source);
    } catch (error) {
      setRequestState({ status: "error", view: null, error: error.message });
      return;
    }
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    setRequestState({ status: "loading", view: null, error: "" });
    try {
      const response = await api.inspectFootballResearch(roomId, payload, controller.signal);
      const view = normalizeFootballResearchResponse(response, roomId);
      setRequestState(view.valid
        ? { status: "ready", view, error: "" }
        : { status: "integrity_failed", view: null, error: view.reason });
    } catch (error) {
      if (error?.name === "AbortError") return;
      setRequestState({
        status: "error",
        view: null,
        error: error?.message || "足球材料只读检查失败。",
      });
    } finally {
      if (requestRef.current === controller) requestRef.current = null;
    }
  };

  const view = requestState.view;
  const claims = view ? footballEvidenceClaims(view.contract) : [];
  const authorizationState = footballRoundContextAuthorizationState(
    roundContextAuthorization,
    {
      roomId,
      contractSha256: view?.contract?.contract_sha256,
      pluginRegistrySnapshotSha256: activation.slot?.snapshotSha256,
    },
  );
  const authorizeForRound = () => {
    if (!view?.valid || typeof onRoundContextAuthorizationChange !== "function") return;
    try {
      onRoundContextAuthorizationChange(buildFootballRoundContextAuthorization(view, activation));
    } catch (error) {
      setRequestState({ status: "error", view: null, error: error.message });
    }
  };
  return (
    <section className={expanded ? "football-research-panel expanded" : "football-research-panel"} aria-label="足球只读研究检查器">
      <button
        type="button"
        className="football-research-toggle"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <span><ShieldCheck size={16} /><strong>足球只读材料检查</strong></span>
        <small>{active ? "精确 v2 贡献已激活" : "仅保留冻结说明"}</small>
        <ChevronDown size={16} />
      </button>

      {expanded ? (
        <div className="football-research-content">
          {!active ? (
            <p className="football-panel-state warning" role="note">
              <AlertTriangle size={16} />
              <span><strong>当前不可执行检查</strong><small>{activation.reason}</small></span>
            </p>
          ) : (
            <>
              <p className="football-material-seal-note">
                <Database size={15} />
                <span><strong>只读取房间内已存在的精确材料版本</strong><small>每条声明必须绑定 material ID、版本、内容哈希与快照哈希；不调用 Provider、不读取市场、不写业务数据。</small></span>
              </p>
              <label className="football-json-input">
                <span><FileJson2 size={15} /><strong>本地足球合同 JSON</strong></span>
                <textarea
                  value={source}
                  onChange={(event) => updateSource(event.target.value)}
                  spellCheck="false"
                  placeholder="粘贴 football_research_contract_v1 输入，或从本机导入 .json 文件"
                  aria-label="足球研究 JSON 输入"
                />
              </label>
              <div className="football-json-actions">
                <label className="secondary compact football-import-button">
                  <Upload size={14} />从本机导入 JSON
                  <input type="file" accept=".json,application/json" onChange={importJson} />
                </label>
                <button className="primary compact" type="button" onClick={inspect} disabled={requestState.status === "loading" || !source.trim()}>
                  {requestState.status === "loading" ? <LoaderCircle className="spin" size={14} /> : <Search size={14} />}
                  {requestState.status === "loading" ? "核验封印中…" : "执行只读检查"}
                </button>
              </div>
            </>
          )}

          {["error", "integrity_failed"].includes(requestState.status) ? (
            <p className="football-panel-state error" role="alert">
              <AlertTriangle size={16} />
              <span><strong>{requestState.status === "integrity_failed" ? "返回合同校验失败" : "检查未完成"}</strong><small>{requestState.error}</small></span>
            </p>
          ) : null}

          {view?.valid ? (
            <div className="football-research-result">
              <MatchFacts contract={view.contract} />
              <section className={authorizationState.valid ? "football-round-context authorized" : "football-round-context"}>
                <span>
                  <strong>{authorizationState.valid ? "已显式加入下一轮冻结上下文" : "下一轮上下文尚未授权"}</strong>
                  <small>比赛 {evidenceText(view.contract.match_identity?.match_id)} · 截止 {textValue(view.contract.data_cutoff_utc)}</small>
                </span>
                <button
                  type="button"
                  className="secondary compact"
                  onClick={authorizationState.valid
                    ? () => onRoundContextAuthorizationChange?.(null)
                    : authorizeForRound}
                >
                  {authorizationState.valid ? "撤销下一轮授权" : "显式用于下一轮"}
                </button>
                <p>只冻结这份已核验合同及其哈希；不会自动开始轮次，也不会授权投注或替代用户决定。</p>
              </section>
              <div className="football-team-grid">
                <TeamResearchCard side="home" team={view.contract.teams.home} />
                <TeamResearchCard side="away" team={view.contract.teams.away} />
              </div>
              <EvidenceClassGrid claims={claims} />
              <FixedSafetyBoundary view={view} />
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
