import { useCallback, useEffect, useRef, useState } from "react";
import { Activity, AlertCircle, ArrowLeft, ArrowUpRight, Check, ChevronDown, Clock3, Inbox, KeyRound, LoaderCircle, MessageSquare, Pause, Play, RefreshCw, Search, SlidersHorizontal, Sparkles } from "lucide-react";
import { api } from "../api";
import "./NewsWorkspace.css";
import { MaterialQuality, AnalysisEvidence } from "./NewsContentQuality";

const PAGE_SIZE = 100;
const tabs = [{ id: "latest", label: "最新消息" }, { id: "attention", label: "值得关注" }, { id: "unread", label: "未读" }];
const sourceStates = { never: "尚未检查", ok: "最近一次检查成功", error: "抓取异常", fetching: "正在抓取", interrupted_unknown: "上次检查中断，结果未确认" };
const checkOutcomes = { started: "检查中", success: "检查成功", not_modified: "来源未更新", failed: "检查失败", interrupted_unknown: "中断，结果未确认" };
const gapLabels = { unclean_restart: "上次退出未确认", clean_restart: "正常停机后恢复", heartbeat_gap: "心跳记录存在缺口", monitor_paused: "曾暂停抓取", clock_changed: "系统时间发生变化" };
const timeFormatter = new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const deadlineFormatter = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
function timeLabel(value, empty = "暂无记录") {
  const parsed = value ? new Date(value) : null;
  return parsed && Number.isFinite(parsed.getTime()) ? timeFormatter.format(parsed) : empty;
}
function deadlineLabel(value) {
  const parsed = value ? new Date(value) : null;
  return parsed && Number.isFinite(parsed.getTime()) ? deadlineFormatter.format(parsed) : "时间待确认";
}
function safeLink(value) {
  try { const url = new URL(value); return url.protocol === "https:" && !url.username && !url.password ? url.href : ""; } catch { return ""; }
}
function message(error, fallback) { return error instanceof Error && error.message ? error.message.slice(0, 400) : fallback; }
function overviewFrom(payload) {
  if (!payload?.overview || typeof payload.overview.enabled !== "boolean" || !Array.isArray(payload.overview.sources)) throw new Error("运行状态暂时无法确认，请重新读取。");
  return payload.overview;
}
function Loading({ children }) { return <span className="news-loading"><LoaderCircle className="news-spin" size={16} aria-hidden="true" />{children}</span>; }
function countLabel(value) { return Number.isSafeInteger(value) && value >= 0 ? value : "—"; }
function checkStatusLabel(value) { return Number.isInteger(value) && value >= 100 && value <= 599 ? `HTTP ${value}` : "未取得 HTTP 状态"; }
function durationLabel(value) {
  if (!Number.isFinite(value) || value < 0) return "时长未知";
  if (value < 60) return "不足 1 分钟";
  if (value < 3600) return `约 ${Math.ceil(value / 60)} 分钟`;
  return `约 ${(value / 3600).toFixed(1)} 小时`;
}
function workerLabel(health) {
  if (health?.alive === false || health?.status === "stopped") return "抓取后台已停止";
  if (health?.status === "unresponsive" || health?.heartbeat_fresh === false) return "后台检查暂未响应";
  if (health?.status === "recovering") return "后台异常，正在恢复";
  return health?.alive === true ? "抓取后台正在运行" : "后台存活状态待确认";
}

function refreshPlan(overview) {
  const listedSources = overview?.sources || [];
  const sources = listedSources.filter((entry) => entry.enabled !== false);
  const held = sources.filter((entry) => entry.manual_hold === true);
  const cooling = sources.filter((entry) => entry.cooldown_active === true && !entry.manual_hold);
  const available = sources.filter((entry) => !entry.cooldown_active && !entry.manual_hold);
  const allHeld = sources.length > 0 && held.length === sources.length;
  const hasScheduleState = sources.some((entry) => typeof entry.refresh_queued === "boolean" || typeof entry.cooldown_active === "boolean");
  const queued = sources.filter((entry) => entry.refresh_queued === true);
  const disabledOnlyQueue = !queued.length && sources.every((entry) => entry.refresh_queued === false) && listedSources.some((entry) => entry.enabled === false && entry.refresh_queued === true);
  const batchPending = queued.length > 0 || (sources.length > 0 && hasScheduleState && overview?.refresh_pending === true && !disabledOnlyQueue);
  const waitingForCooldown = queued.length > 0 && queued.every((entry) => entry.cooldown_active === true && !entry.manual_hold);
  return { held, cooling, available, allHeld, hasScheduleState, batchPending,
    label: allHeld ? "需要检查来源设置" : batchPending ? waitingForCooldown ? "已排队，等待允许检查" : "检查已排队" : !available.length && cooling.length ? "安排允许后检查" : held.length || cooling.length ? "检查可用来源" : "立即检查",
  };
}

function refreshNotice(overview) {
  const plan = refreshPlan(overview);
  if (plan.allHeld) return "来源需要人工检查，暂不发送请求；已保存的消息仍可阅读。";
  if (plan.cooling.length || plan.held.length) {
    const queued = plan.cooling.some((entry) => entry.refresh_queued === true);
    return `${plan.available.length ? "已安排可用来源检查。" : "检查请求已收到。"}${queued ? "冷却来源已排队，到允许时间再检查。" : plan.cooling.length ? "冷却来源仍需等待允许时间。" : ""}${plan.held.length ? "需人工检查的来源仍保持等待。" : ""}重复点击不会提前请求。`;
  }
  return "已安排立即检查，抓取结果会自动更新。";
}

function SourceStatus({ entry, workerStopped, workerUnresponsive }) {
  const held = entry.manual_hold === true;
  const cooling = entry.cooldown_active === true;
  const recovering = entry.recovery_pending === true;
  const restricted = held || cooling || recovering;
  const status = held ? "需要人工检查" : cooling ? entry.cooldown_reason === "rate_limited" ? "限流冷却中" : "等待下次允许检查" : entry.status === "fetching" && workerStopped ? "上次检查结果待确认" : entry.status === "fetching" && workerUnresponsive ? "检查推进待确认" : sourceStates[entry.status] || "状态未知";
  return <article>
    <strong>{entry.name}</strong>
    <span className={restricted || entry.status === "error" || entry.status === "interrupted_unknown" || entry.stale || entry.overdue ? "news-source-error" : ""}>{status}{entry.enabled === false ? " · 已停用" : entry.stale || entry.overdue ? " · 检查记录已过期" : ""}</span>
    {entry.cooldown_reason === "access_denied" ? <p>访问被拒绝，原因待确认；需要核对接入要求和请求路径。</p> : null}
    {held ? <p>自动检查已暂缓，需先人工核对；下次允许检查时间待确认。</p> : null}
    {recovering && !held ? <p>恢复观察中 · 连续成功 {countLabel(entry.recovery_successes)} 次。单次成功不代表持续恢复，将逐步恢复检查频率。</p> : null}
    {entry.refresh_queued === true ? <small className="news-source-queue">{held ? "已排队，需先完成人工检查" : cooling ? "已排队，到允许时间再检查" : "检查已排队"}；重复点击不会增加请求。</small> : null}
    <small>最近成功：{timeLabel(entry.last_success_at)}</small>
    {entry.enabled !== false && !held && (cooling || recovering) ? <small>下次允许检查：{deadlineLabel(entry.cooldown_until)}</small> : entry.enabled !== false && !held && entry.next_poll_at ? <small>下次常规检查：{timeLabel(entry.next_poll_at)}</small> : null}
    {entry.overdue ? <small>已超过检查时间 {durationLabel(entry.overdue_seconds)}</small> : null}
    {entry.error ? <p>{String(entry.error).slice(0, 300)}</p> : null}
  </article>;
}

function RuntimeHistory({ overview, overviewError, pending, startupError, onStartup }) {
  const continuity = overview?.continuity;
  const health = overview?.worker_health;
  const checks = overview?.checks_24h;
  const records = Array.isArray(overview?.recent_checks) ? overview.recent_checks : [];
  const startup = overview?.startup;
  const startupKnown = typeof startup?.enabled === "boolean" && typeof startup?.supported === "boolean";
  const gap = continuity?.last_gap;
  const hasGap = continuity?.previous_session_unclean || continuity?.unverified_gap_count_24h > 0;
  return <details className="news-run-history">
    <summary><Clock3 size={15} aria-hidden="true" /><span>运行记录</span><small>{hasGap ? "有未确认的运行时段" : continuity?.evidence_started_at ? "查看检查结果与恢复记录" : "连续运行证据待建立"}</small><ChevronDown size={14} aria-hidden="true" /></summary>
    <div className="news-run-history-body">
      <section className="news-startup-settings" aria-label="登录后恢复设置">
        <label className="news-ai-check"><input type="checkbox" checked={startup?.enabled === true} disabled={!startupKnown || !startup.supported || !!pending || !!overviewError} onChange={(event) => onStartup(event.target.checked)} aria-describedby="news-startup-help news-startup-error" /><span>登录 Windows 后恢复抓取</span></label>
        <p id="news-startup-help">{pending === "startup" ? "正在保存，请稍候…" : !startupKnown ? "本机设置待确认，不会自动更改。" : !startup.supported ? "当前运行方式不支持设置 Windows 登录后恢复。" : "登录当前 Windows 账户后启动本地服务，按上次抓取开关恢复；不会自动启用 AI 分析。"}</p>
        {startup?.enabled === true && startup?.version_current === false ? <div className="news-startup-update"><p className="news-history-warning">登录恢复入口仍指向旧版本。更新入口后，下次登录才会启动当前版本。</p><button className="news-button" type="button" disabled={!startupKnown || !startup.supported || !!pending || !!overviewError} onClick={() => onStartup(true)}>{pending === "startup" ? "正在更新…" : "更新登录恢复入口"}</button></div> : null}
        {startupError || startup?.error ? <p id="news-startup-error" className="news-source-error" role="alert">{startupError || String(startup.error).slice(0, 300)}</p> : null}
      </section>
      <section aria-label="后台与连续运行记录">
        <h2>{overviewError ? "后台状态待确认" : workerLabel(health)}</h2>
        <dl className="news-run-facts"><div><dt>最近心跳</dt><dd>{timeLabel(continuity?.last_heartbeat_at || health?.heartbeat_at)}</dd></div><div><dt>本次运行开始</dt><dd>{timeLabel(continuity?.session_started_at)}</dd></div><div><dt>记录开始</dt><dd>{timeLabel(continuity?.evidence_started_at)}</dd></div></dl>
        {health?.alive !== false && (health?.status === "unresponsive" || health?.heartbeat_fresh === false) ? <p className="news-history-warning">后台线程仍存在，但近期心跳未确认。可暂停抓取或点击“立即检查”；目前不能确认检查正在推进。</p> : null}
        {health?.status === "recovering" ? <p className="news-history-warning">后台发生异常，将于 {timeLabel(health.next_recovery_at, "稍后")} 再次恢复；也可暂停抓取。连续异常 {countLabel(health.consecutive_failures)} 次。</p> : null}
        {health?.alive === false || health?.status === "stopped" ? <p className="news-history-warning">后台目前未执行抓取。可点击“立即检查”或“恢复持续抓取”重新启动。</p> : null}
        {continuity?.previous_session_unclean ? <p className="news-history-warning">上次退出未确认，已保存的消息仍可阅读；停机期间可能有遗漏。</p> : null}
        {gap ? <p className="news-history-gap"><strong>{gapLabels[gap.kind] || "存在未验证的运行时段"}</strong> · {timeLabel(gap.started_at)} 至 {timeLabel(gap.ended_at, "结束待确认")}（{durationLabel(gap.duration_seconds)}）。这段记录不能证明抓取一直在运行。</p> : null}
        <p className="news-history-limitation">当前记录不能确认连续 24 小时稳定运行；消息条数和本次启动时间不代表完整覆盖。电脑需保持开机、联网且不休眠。</p>
      </section>
      <section aria-label="近24小时检查统计">
        <h2>近 24 小时来源检查</h2><p className="news-history-caption">按每个来源的一次检查计数，仅统计已记录的时段。</p>
        <dl className="news-run-counts"><div><dt>成功</dt><dd>{countLabel(checks?.successful)}</dd></div><div><dt>失败</dt><dd>{countLabel(checks?.failed)}</dd></div><div><dt>中断未确认</dt><dd>{countLabel(checks?.interrupted)}</dd></div><div><dt>新增消息</dt><dd>{countLabel(checks?.new_items)}</dd></div></dl>
        {records.length ? <ol className="news-check-records" aria-label="最近来源检查">{records.slice(0, 10).map((entry, index) => <li key={`${entry.source_id}-${entry.started_at}-${index}`}><div><strong>{entry.source_name || "来源未标注"}</strong><span className={entry.outcome === "failed" || entry.outcome === "interrupted_unknown" ? "news-source-error" : ""}>{entry.outcome === "started" && (health?.alive === false || health?.status === "unresponsive" || health?.heartbeat_fresh === false) ? "结果待确认" : checkOutcomes[entry.outcome] || "结果待确认"}{entry.outcome === "failed" ? ` · ${checkStatusLabel(entry.http_status)}` : ""}</span></div><div><time dateTime={entry.finished_at || entry.started_at || undefined}>{timeLabel(entry.finished_at || entry.started_at)}</time><span>新增 {countLabel(entry.new_items)} · 更新 {countLabel(entry.updated_items)}</span></div></li>)}</ol> : <p className="news-history-caption">尚无检查记录；点击“立即检查”后会在这里显示结果。</p>}
      </section>

    </div>
  </details>;
}

function AIReading({ analysis, materialQuality, configured, busy, onAnalyze }) {
  const status = analysis?.status;
  const insufficient = materialQuality?.can_analyze === false;
  return <section className="news-ai-reading">
    <h3><span className="news-ai-title"><Sparkles size={16} aria-hidden="true" />豆包分析</span></h3>
    {analysis ? <>
      <p className="news-ai-state">{({ completed: "分析已完成", success: "分析已完成", queued: "已加入分析队列", pending: "正在分析", running: "正在分析", failed: "分析未完成", material_insufficient: "材料不足，未发送模型请求", unknown: "结果未知，不会自动重试" })[String(status).toLowerCase()] || "分析状态待确认"}</p>
      {analysis.summary ? <p>{analysis.summary}</p> : null}
      {Array.isArray(analysis.key_points) && analysis.key_points.length ? <ul>{analysis.key_points.map((point, index) => <li key={index}>{point}</li>)}</ul> : null}
      {Array.isArray(analysis.uncertainties) && analysis.uncertainties.length ? <><h4>仍需核实</h4><ul>{analysis.uncertainties.map((point, index) => <li key={index}>{point}</li>)}</ul></> : null}
      <AnalysisEvidence analysis={analysis} />
      {analysis.error ? <p className="news-source-error">{analysis.error}</p> : null}
      <small>{analysis.analysis_performed === false ? "本机材料检查，未调用 AI；请打开原文核对。" : "AI 根据本条来源内容生成，结论仍需结合原文核对。"}</small>
    </> : <><p>{insufficient ? "当前订阅材料不足，请先阅读官方原文；不会发送模型请求。" : configured ? "需要进一步理解这条消息时，可使用本次已确认的额度分析。" : "AI 分析尚未启用；上方的本机整理不消耗模型额度。"}</p><button type="button" className="news-button" disabled={!configured || busy || insufficient} onClick={onAnalyze}>{busy ? <LoaderCircle className="news-spin" size={16} aria-hidden="true" /> : <Sparkles size={16} aria-hidden="true" />}用豆包分析这条</button></>}
  </section>;
}

export function NewsWorkspace({ onOpenRooms, onPrepareResearch, rooms = [], apiClient = api }) {
  const [overview, setOverview] = useState(null);
  const [overviewError, setOverviewError] = useState("");
  const [tab, setTab] = useState("latest");
  const [query, setQuery] = useState("");
  const [settledQuery, setSettledQuery] = useState("");
  const [source, setSource] = useState("");
  const [category, setCategory] = useState("");
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [detailRefresh, setDetailRefresh] = useState(0);
  const [pending, setPending] = useState("");
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState("");
  const [startupError, setStartupError] = useState("");
  const [researchOpen, setResearchOpen] = useState(false);
  const [researchRoomId, setResearchRoomId] = useState("");
  const [researchBusy, setResearchBusy] = useState(false);
  const [researchError, setResearchError] = useState("");
  const [ai, setAI] = useState(null);
  const [aiKey, setAIKey] = useState("");
  const [automaticAI, setAutomaticAI] = useState(false);
  const [budgetAcknowledged, setBudgetAcknowledged] = useState(false);
  const [aiError, setAIError] = useState("");
  const mounted = useRef(true);
  const itemRequest = useRef({ sequence: 0, controller: null });
  const overviewRequest = useRef({ sequence: 0, controller: null });
  const itemsRef = useRef(items);
  const detailHeading = useRef(null);
  const selectedTrigger = useRef(null);
  const selectionRef = useRef(selectedId);
  const pendingRef = useRef(false);
  const actionController = useRef(null);
  const researchController = useRef(null);
  const researchTrigger = useRef(null);
  const researchRoomSelect = useRef(null);
  const restoreResearchFocus = useRef(false);
  itemsRef.current = items;
  selectionRef.current = selectedId;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; itemRequest.current.controller?.abort(); overviewRequest.current.controller?.abort(); actionController.current?.abort(); researchController.current?.abort(); };
  }, []);
  useEffect(() => {
    const timeout = window.setTimeout(() => setSettledQuery(query.trim()), 300);
    return () => window.clearTimeout(timeout);
  }, [query]);

  const loadOverview = useCallback(async () => {
    overviewRequest.current.controller?.abort();
    const controller = new AbortController();
    const sequence = ++overviewRequest.current.sequence;
    overviewRequest.current.controller = controller;
    try {
      const data = overviewFrom(await apiClient.newsOverview(controller.signal));
      if (mounted.current && sequence === overviewRequest.current.sequence) { setOverview(data); setOverviewError(""); setStartupError(""); if (data.ai) setAI(data.ai); }
    } catch (error) {
      if (mounted.current && !controller.signal.aborted && sequence === overviewRequest.current.sequence) setOverviewError(message(error, "暂时无法读取运行状态。"));
    }
  }, [apiClient]);

  const loadItems = useCallback(async (append = false) => {
    itemRequest.current.controller?.abort();
    const controller = new AbortController();
    const sequence = ++itemRequest.current.sequence;
    itemRequest.current.controller = controller;
    setListLoading(true);
    setListError("");
    try {
      const data = await apiClient.newsItems({ query: settledQuery, source, category, unread: tab === "unread", importance: tab === "attention" ? "attention" : "", limit: PAGE_SIZE, offset: append ? itemsRef.current.length : 0, signal: controller.signal });
      if (!Array.isArray(data.items) || !Number.isFinite(data.total)) throw new Error("消息列表暂时无法读取，请重试。");
      if (!mounted.current || sequence !== itemRequest.current.sequence) return;
      setItems((previous) => append ? [...previous, ...data.items.filter((item) => !previous.some((entry) => entry.id === item.id))] : data.items);
      setTotal(data.total);
    } catch (error) {
      if (mounted.current && !controller.signal.aborted && sequence === itemRequest.current.sequence) setListError(message(error, "消息列表暂时无法读取。"));
    } finally {
      if (mounted.current && sequence === itemRequest.current.sequence) setListLoading(false);
    }
  }, [apiClient, settledQuery, source, category, tab]);

  useEffect(() => { void loadOverview(); }, [loadOverview]);
  useEffect(() => { setItems([]); setTotal(0); void loadItems(); }, [loadItems]);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "hidden" || pendingRef.current) return;
      void loadOverview();
      if (itemsRef.current.length <= PAGE_SIZE) void loadItems();
    }, 15000);
    return () => window.clearInterval(timer);
  }, [loadItems, loadOverview]);

  useEffect(() => {
    if (!selectedId) { setDetail(null); setDetailError(""); setDetailLoading(false); return undefined; }
    const controller = new AbortController();
    setDetail(null); setDetailError(""); setDetailLoading(true);
    apiClient.newsItem(selectedId, controller.signal).then((payload) => {
      if (controller.signal.aborted || !mounted.current) return;
      if (!payload.item || payload.item.id !== selectedId) throw new Error("未找到这条消息，请刷新列表。");
      setDetail(payload.item);
    }).catch((error) => {
      if (!controller.signal.aborted && mounted.current) setDetailError(message(error, "消息详情暂时无法读取。"));
    }).finally(() => { if (!controller.signal.aborted && mounted.current) setDetailLoading(false); });
    return () => controller.abort();
  }, [apiClient, selectedId, detailRefresh]);
  useEffect(() => { if (selectedId && !detailLoading) detailHeading.current?.focus(); }, [selectedId, detailLoading]);
  useEffect(() => {
    researchController.current?.abort();
    setResearchOpen(false); setResearchRoomId(""); setResearchBusy(false); setResearchError("");
  }, [selectedId]);
  useEffect(() => {
    if (researchOpen) researchRoomSelect.current?.focus();
    else if (restoreResearchFocus.current) { restoreResearchFocus.current = false; researchTrigger.current?.focus(); }
  }, [researchOpen]);
  useEffect(() => {
    if (!selectedId || !["queued", "pending", "running"].includes(detail?.ai_analysis?.status)) return undefined;
    const controller = new AbortController();
    let inFlight = false;
    const timer = window.setInterval(async () => {
      if (inFlight || document.visibilityState === "hidden") return;
      inFlight = true;
      try {
        const [itemPayload, statusPayload] = await Promise.all([apiClient.newsItem(selectedId, controller.signal), apiClient.newsAIStatus(controller.signal)]);
        if (controller.signal.aborted || !mounted.current || selectionRef.current !== selectedId) return;
        if (statusPayload.ai) setAI(statusPayload.ai);
        if (itemPayload.item?.id === selectedId && itemPayload.item.ai_analysis) setDetail(itemPayload.item);
        else if (statusPayload.ai?.error) { setAIError(statusPayload.ai.error); setDetail((current) => current?.id === selectedId ? { ...current, ai_analysis: null } : current); }
      } catch (error) { if (!controller.signal.aborted && mounted.current) setAIError(message(error, "暂时无法读取分析状态，不会重发模型请求。")); }
      finally { inFlight = false; }
    }, 2500);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [apiClient, selectedId, detail?.ai_analysis?.status]);

  const runAction = async (kind) => {
    if (pendingRef.current) return;
    pendingRef.current = true; setPending(kind); setActionError(""); setNotice("");
    overviewRequest.current.controller?.abort(); ++overviewRequest.current.sequence;
    const controller = new AbortController(); actionController.current = controller;
    try {
      const restoreStopped = overview?.enabled && (overview.worker_health?.alive === false || overview.worker_health?.status === "stopped");
      const payload = kind === "refresh" ? await apiClient.newsRefresh(controller.signal) : await apiClient.newsControl(restoreStopped || !overview?.enabled, controller.signal);
      const current = overviewFrom(payload);
      if (!mounted.current) return;
      setOverview(current); setOverviewError("");
      setNotice(kind === "refresh" ? refreshNotice(current) : current.enabled ? current.worker_health?.alive === true && current.worker_health?.status === "running" && current.worker_health?.heartbeat_fresh !== false ? "持续抓取已开启。电脑和本地服务保持运行时会定期检查。" : "已保存持续抓取设置，后台运行情况请查看上方状态。" : "已暂停后续抓取，已保存的消息仍可阅读。");
      void loadItems();
    } catch (error) { if (mounted.current) setActionError(message(error, "操作尚未确认，请读取状态后再试。")); }
    finally { pendingRef.current = false; if (mounted.current) setPending(""); }
  };
  const updateStartup = async (enabled) => {
    if (pendingRef.current) return;
    const updatingVersion = enabled && overview?.startup?.enabled === true && overview.startup.version_current === false;
    pendingRef.current = true; setPending("startup"); setStartupError(""); setNotice("");
    overviewRequest.current.controller?.abort(); ++overviewRequest.current.sequence;
    const controller = new AbortController(); actionController.current = controller;
    try {
      const payload = await apiClient.newsStartup({ enabled }, controller.signal);
      const current = payload.overview?.startup || payload.startup;
      if (!current || current.supported !== true || typeof current.enabled !== "boolean" || current.enabled !== enabled || (enabled && current.version_current === false) || current.error) throw new Error("登录后恢复设置尚未确认，请重新读取状态后再试。");
      if (!mounted.current) return;
      setOverview((previous) => ({ ...previous, startup: current }));
      setNotice(current.enabled ? updatingVersion ? "登录恢复入口已更新，下次登录将启动当前版本。" : "已开启登录 Windows 后恢复抓取。" : "已关闭登录 Windows 后恢复抓取。");
    } catch (error) { if (mounted.current && !controller.signal.aborted) setStartupError(message(error, "未能保存登录后恢复设置，请重新读取后再试。")); }
    finally { pendingRef.current = false; if (mounted.current) setPending(""); }
  };
  const markRead = async () => {
    if (!detail || pendingRef.current) return;
    const id = detail.id;
    pendingRef.current = true; setPending("read"); setActionError(""); setNotice("");
    const controller = new AbortController(); actionController.current = controller;
    try {
      const payload = await apiClient.newsMarkRead(id, controller.signal);
      if (!payload.item || payload.item.id !== id || payload.item.read !== true) throw new Error("已读状态尚未确认，请重新读取。");
      if (!mounted.current) return;
      if (selectionRef.current === id) setDetail(payload.item);
      setItems((previous) => previous.map((item) => item.id === id ? payload.item : item));
      setNotice("已标为已读。"); void loadOverview();
      if (tab === "unread") void loadItems();
    } catch (error) { if (mounted.current) setActionError(message(error, "未能保存已读状态。")); }
    finally { pendingRef.current = false; if (mounted.current) setPending(""); }
  };
  const aiAction = async (kind, event) => {
    event?.preventDefault();
    if (pendingRef.current || (kind === "configure" && (!aiKey.trim() || !budgetAcknowledged))) return;
    const keyForRequest = aiKey.trim();
    const itemId = detail?.id;
    setAIKey(""); setAIError(""); setNotice("");
    pendingRef.current = true; setPending(`ai-${kind}`);
    const controller = new AbortController(); actionController.current = controller;
    overviewRequest.current.controller?.abort(); ++overviewRequest.current.sequence;
    try {
      const payload = kind === "configure"
        ? await apiClient.newsAIConfigure({ api_key: keyForRequest, automatic: automaticAI, acknowledge_budget: true }, controller.signal)
        : kind === "clear" ? await apiClient.newsAIClear(controller.signal)
          : await apiClient.newsAIAnalyze(itemId, controller.signal);
      if (!mounted.current) return;
      if (kind === "analyze") {
        if (payload.item?.id === itemId && selectionRef.current === itemId) setDetail(payload.item);
        else if (payload.ai_analysis && selectionRef.current === itemId) setDetail((current) => current?.id === itemId ? { ...current, ai_analysis: payload.ai_analysis } : current);
        else if (selectionRef.current === itemId) setDetailRefresh((value) => value + 1);
        setNotice("分析请求已返回，请查看这条消息的 AI 分析状态。");
      } else {
        if (!payload.ai || typeof payload.ai.configured !== "boolean") throw new Error("AI 配置状态尚未确认，请重新读取。");
        setAI(payload.ai); setBudgetAcknowledged(false);
        setNotice(kind === "clear" ? "当前运行中的密钥已清除，后续 AI 分析已停止。" : "AI 分析已启用，密钥仅在本次服务运行期间有效。");
      }
      void loadOverview();
    } catch (error) { if (mounted.current && !controller.signal.aborted) setAIError(message(error, "AI 操作尚未确认，不会自动重试。")); }
    finally { pendingRef.current = false; if (mounted.current) setPending(""); }
  };
  const closeDetail = () => { setSelectedId(""); selectedTrigger.current?.focus(); };
  const cancelResearch = () => {
    researchController.current?.abort(); researchController.current = null;
    restoreResearchFocus.current = true;
    setResearchOpen(false); setResearchBusy(false); setResearchError("");
  };
  const prepareResearch = async () => {
    if (researchBusy || !detail || !researchRoomId || typeof onPrepareResearch !== "function") return;
    const controller = new AbortController(); researchController.current = controller;
    setResearchBusy(true); setResearchError("");
    try { await onPrepareResearch(detail, researchRoomId, { signal: controller.signal }); }
    catch (error) { if (mounted.current && !controller.signal.aborted) setResearchError(message(error, "无法打开目标房间，请重新选择后再试。")); }
    finally { if (mounted.current && !controller.signal.aborted) setResearchBusy(false); }
  };
  const workerStopped = overview?.worker_health?.alive === false || overview?.worker_health?.status === "stopped";
  const workerUnresponsive = !workerStopped && (overview?.worker_health?.status === "unresponsive" || overview?.worker_health?.heartbeat_fresh === false);
  const workerRecovering = overview?.worker_health?.status === "recovering";
  const sourceList = overview?.sources || [];
  const refresh = refreshPlan(overview);
  const fetching = !workerStopped && !workerUnresponsive && !workerRecovering && ((!refresh.hasScheduleState && overview?.refresh_pending) || sourceList.some((entry) => entry.status === "fetching"));
  const runningLabel = overviewError ? "状态待确认" : !overview ? "正在连接" : workerStopped ? "抓取后台已停止" : workerUnresponsive ? "后台检查暂未响应" : workerRecovering ? "后台异常，正在恢复" : !overview.enabled ? "已暂停" : overview.worker_health?.alive !== true || overview.worker_health?.status !== "running" ? "后台状态待确认" : fetching ? "正在抓取" : "持续抓取已开启";
  const anyErrors = overview?.sources?.filter((entry) => entry.status === "error") || [];
  const restrictedSources = sourceList.filter((entry) => entry.enabled !== false && (entry.cooldown_active || entry.manual_hold || entry.recovery_pending));
  const incompleteSources = sourceList.filter((entry) => entry.enabled !== false && (entry.status === "error" || entry.status === "interrupted_unknown" || entry.cooldown_active || entry.manual_hold || entry.recovery_pending));
  const staleSources = sourceList.filter((entry) => entry.enabled !== false && (entry.stale || entry.overdue));
  const originalUrl = safeLink(detail?.url);

  return <main className="news-workspace" aria-label="消息工作台">
    <header className="news-header">
      <div><span className="news-eyebrow">AI 共创室 · 消息与研究</span><h1>消息工作台</h1><p>集中看新消息，保留原文，找出值得继续研究的变化。</p></div>
      <button type="button" className="news-button" onClick={onOpenRooms}><MessageSquare size={17} aria-hidden="true" />讨论房间<ArrowUpRight size={15} aria-hidden="true" /></button>
    </header>

    <section className="news-runtime" aria-label="抓取运行状态">
      <div className="news-runtime-primary"><Activity size={20} aria-hidden="true" /><div><strong>{runningLabel}</strong><small>{overviewError ? "等待重新读取后台状态" : workerLabel(overview?.worker_health)} · {ai?.configured ? "AI 按已确认额度使用" : "本机整理 · AI 未启用"}</small></div></div>
      <div className="news-runtime-times"><span>最近成功 <b>{timeLabel(overview?.last_success_at)}</b></span><span>下次检查 <b>{workerStopped ? "等待恢复" : workerUnresponsive ? "等待后台响应" : refresh.allHeld ? "需人工检查" : overview?.enabled ? timeLabel(overview?.next_poll_at, "正在安排") : "暂停中"}</b></span></div>
      <div className="news-runtime-actions">
        <button type="button" className="news-button" disabled={!overview || !!pending || fetching || refresh.allHeld || refresh.batchPending} aria-describedby={restrictedSources.length || refresh.batchPending ? "news-refresh-help" : undefined} onClick={() => void runAction("refresh")}>{pending === "refresh" || fetching ? <LoaderCircle className="news-spin" size={16} aria-hidden="true" /> : <RefreshCw size={16} aria-hidden="true" />}{refresh.label}</button>
        <button type="button" className={overview?.enabled && !workerStopped ? "news-button" : "news-button news-button-primary"} disabled={!overview || !!pending || !!overviewError} onClick={() => void runAction("control")}>{pending === "control" ? <LoaderCircle className="news-spin" size={16} aria-hidden="true" /> : overview?.enabled && !workerStopped ? <Pause size={16} aria-hidden="true" /> : <Play size={16} aria-hidden="true" />}{overview?.enabled ? workerStopped ? "恢复持续抓取" : "暂停抓取" : "开始持续抓取"}</button>
      </div>
    </section>
    <p className="news-runtime-note">持续抓取开启且后台正常运行时，关闭网页仍会抓取。电脑休眠、关机或退出本地服务时暂停，停机期间可能有遗漏。</p>
    {staleSources.length && !overviewError ? <p className="news-runtime-warning"><AlertCircle size={14} aria-hidden="true" />{staleSources.length} 个来源的检查记录已过期，旧消息不代表仍在更新。{restrictedSources.length ? "请查看来源状态中的等待时间或人工核对提示。" : "可点击“立即检查”。"}</p> : null}
    {incompleteSources.length && !overviewError ? <p className="news-runtime-warning news-coverage-warning"><AlertCircle size={14} aria-hidden="true" />来源覆盖暂不完整：{incompleteSources.length} 个来源异常、等待或恢复观察中。后台运行不代表每个来源都已成功；已保存的消息仍可阅读。</p> : null}
    {restrictedSources.length || refresh.batchPending ? <p id="news-refresh-help" className="news-runtime-note">{refresh.allHeld ? "来源需要人工核对，当前不会发送检查请求。" : refresh.batchPending ? "已有检查批次在排队，暂不重复安排；冷却来源到允许时间再检查，其他来源仍按原计划检查。" : refresh.available.length ? "检查按钮会安排可用来源，并将冷却来源排队；需人工核对的来源继续等待。" : "冷却中的来源可合并排队，到允许时间再检查；不会因重复点击提前请求。"}具体时间与状态见“来源状态”。</p> : null}
    {overviewError ? <div className="news-alert" role="alert"><AlertCircle size={17} aria-hidden="true" /><span>{overviewError} 已保留上次读取的内容。</span><button type="button" className="news-inline-button" onClick={() => void loadOverview()}>重新读取</button></div> : null}
    {actionError ? <p className="news-alert" role="alert">{actionError}</p> : null}
    {aiError ? <p className="news-alert" role="alert">{aiError}</p> : null}
    <p className="news-notice" role="status" aria-live="polite">{notice}</p>

    <details className="news-ai-settings">
      <summary><KeyRound size={15} aria-hidden="true" /><span>{ai?.configured ? "AI 分析已配置" : "配置 AI 分析"}</span><small>{ai?.configured ? `${ai.calls_reserved ?? 0} / ${ai.max_calls ?? 36} 次` : "可选 · 免费抓取不需要密钥"}</small><ChevronDown size={14} aria-hidden="true" /></summary>
      <div className="news-ai-settings-body">
        <p>豆包模型：doubao-seed-2-1-pro-260915。密钥仅当前运行有效，最长 24 小时，不保存到本地文件。每次调用可能产生费用。</p>
        <p>近 24 小时累计最多 <strong>36 次</strong>，应用预约预算 <strong>3 元</strong>，失败不重试。预约金额用于限制发送，实际费用以供应商账单为准。</p>
        {ai?.configured ? <div className="news-ai-configured"><p>已预约 {ai.calls_reserved ?? 0} / {ai.max_calls ?? 36} 次 · 已预约费用 {Number(ai.reserved_cny || 0).toFixed(2)} / {Number(ai.budget_cny || 3).toFixed(2)} 元 · {ai.automatic ? "自动分析此后新增消息" : "仅手动选择消息分析"}</p><button className="news-button" type="button" disabled={!!pending} onClick={() => void aiAction("clear")}>{pending === "ai-clear" ? "正在清除…" : "清除密钥并停止 AI"}</button></div> : <form onSubmit={(event) => void aiAction("configure", event)}>
          <label className="news-ai-key"><span>豆包 API key</span><input type="password" autoComplete="off" spellCheck={false} value={aiKey} onChange={(event) => setAIKey(event.target.value)} placeholder="仅在本机输入" maxLength={1000} disabled={!!pending} /></label>
          <label className="news-ai-check"><input type="checkbox" checked={automaticAI} onChange={(event) => setAutomaticAI(event.target.checked)} disabled={!!pending} /><span>自动分析此后新增消息</span></label>
          <label className="news-ai-check"><input type="checkbox" checked={budgetAcknowledged} onChange={(event) => setBudgetAcknowledged(event.target.checked)} disabled={!!pending} /><span>我确认上述调用次数、预算和用途</span></label>
          <button type="submit" className="news-button news-button-primary" disabled={!aiKey.trim() || !budgetAcknowledged || !!pending}>{pending === "ai-configure" ? "正在启用…" : "确认额度并启用"}</button>
        </form>}
        {ai?.error ? <p role="status">{ai.error}</p> : null}
      </div>
    </details>

    <RuntimeHistory overview={overview} overviewError={overviewError} pending={pending} startupError={startupError} onStartup={(enabled) => void updateStartup(enabled)} />

    <div className="news-summary-row"><span>近 24 小时消息 <strong>{overview?.counts?.last_24h ?? "—"}</strong></span><span>未读 <strong>{overview?.counts?.unread ?? "—"}</strong></span><span>已保存 <strong>{overview?.counts?.total ?? "—"}</strong></span><details className="news-source-status"><summary><SlidersHorizontal size={15} aria-hidden="true" />{restrictedSources.length ? `${restrictedSources.length} 个来源受限或恢复中` : anyErrors.length ? `${anyErrors.length} 个来源异常` : staleSources.length ? `${staleSources.length} 个来源待检查` : `${sourceList.length} 个来源`}<ChevronDown size={14} aria-hidden="true" /></summary><div className="news-source-popover"><h2>来源状态</h2>{sourceList.length ? sourceList.map((entry) => <SourceStatus key={entry.id} entry={entry} workerStopped={workerStopped} workerUnresponsive={workerUnresponsive} />) : <p>正在读取来源目录。</p>}</div></details></div>

    <section className={`news-reader${selectedId ? " news-detail-open" : ""}`} aria-label="消息阅读区">
      <div className="news-list-panel">
        <div className="news-tabs" role="group" aria-label="消息范围">{tabs.map((entry) => <button key={entry.id} type="button" className={tab === entry.id ? "active" : ""} aria-pressed={tab === entry.id} onClick={() => setTab(entry.id)}>{entry.label}</button>)}<button type="button" className="news-list-reload" aria-label="重新读取消息列表" title="重新读取消息列表" disabled={listLoading} onClick={() => { void loadItems(); void loadOverview(); }}><RefreshCw size={16} aria-hidden="true" /></button></div>
        <div className="news-filters"><label className="news-search"><Search size={17} aria-hidden="true" /><span className="news-sr-only">搜索标题或内容</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索标题或内容" maxLength={200} /></label><div><label><span className="news-sr-only">按来源筛选</span><select value={source} onChange={(event) => setSource(event.target.value)}><option value="">全部来源</option>{sourceList.map((entry) => <option key={entry.id} value={entry.id}>{entry.name}</option>)}</select></label><label><span className="news-sr-only">按分类筛选</span><select value={category} onChange={(event) => setCategory(event.target.value)}><option value="">全部分类</option>{(overview?.categories || []).map((entry) => <option key={entry.id} value={entry.id}>{entry.label}</option>)}</select></label></div></div>
        <div className="news-list-count"><span>{total} 条消息{items.length < total ? ` · 已显示 ${items.length} 条` : ""}</span>{listLoading ? <Loading>正在读取</Loading> : <span>{items.length > PAGE_SIZE ? "已展开更多历史" : "每 15 秒更新"}</span>}</div>
        {listError ? <div className="news-alert" role="alert"><span>{listError}</span><button type="button" className="news-inline-button" onClick={() => void loadItems()}>重试</button></div> : null}
        <div className="news-item-scroll" aria-busy={listLoading}>
          {!items.length && !listLoading && !listError ? <div className="news-empty"><Inbox size={32} aria-hidden="true" /><h2>{settledQuery || source || category || tab !== "latest" ? "没有符合条件的消息" : "还没有抓取到消息"}</h2><p>{settledQuery || source || category || tab !== "latest" ? "换一个筛选条件，或查看最新消息。" : restrictedSources.length ? "请查看来源状态；冷却期间等待允许时间，需要人工核对的来源暂不检查。" : "点击“立即检查”获取官方来源的最新内容，也可以开启持续抓取。"}</p></div> : null}
          {items.map((item) => <button key={item.id} type="button" className={`news-item${selectedId === item.id ? " selected" : ""}${item.read ? " read" : ""}`} aria-current={selectedId === item.id ? "true" : undefined} onClick={(event) => { selectedTrigger.current = event.currentTarget; setSelectedId(item.id); }}><span className="news-item-meta"><span>{item.source_name}</span><time dateTime={item.published_at || undefined}>{timeLabel(item.published_at, "发布时间未知")}</time></span><strong>{!item.read ? <span className="news-unread-dot"><span className="news-sr-only">未读：</span></span> : null}{item.title}</strong><p>{item.summary || item.excerpt || "打开查看来源详情"}</p><span className="news-item-tags"><span>{item.category_label || "其他"}</span>{item.importance === "attention" ? <span className="news-attention">值得关注</span> : null}<span>本机整理</span></span></button>)}
          {items.length < total ? <button className="news-load-more news-button" type="button" disabled={listLoading} onClick={() => void loadItems(true)}>{listLoading ? "正在读取…" : "加载更多消息"}</button> : null}
        </div>
      </div>
      <aside className="news-detail" aria-label="消息详情">
        {selectedId ? <div className="news-detail-tools"><button type="button" className="news-inline-button" onClick={closeDetail}><ArrowLeft size={16} aria-hidden="true" />返回列表</button>{detail ? <button type="button" className="news-inline-button" disabled={detail.read || !!pending} onClick={() => void markRead()}><Check size={16} aria-hidden="true" />{pending === "read" ? "正在保存…" : detail.read ? "已读" : "标为已读"}</button> : null}</div> : null}
        {detailLoading ? <div className="news-detail-loading"><Loading>正在读取消息详情…</Loading></div> : detailError ? <div className="news-empty" role="alert"><h2 ref={detailHeading} tabIndex={-1}>详情暂时不可用</h2><p>{detailError}</p><button className="news-button" type="button" onClick={() => setDetailRefresh((value) => value + 1)}>重新读取</button></div> : detail ? <article className="news-detail-body"><div className="news-detail-source">{detail.source_name}<span>{detail.category_label || "其他"}</span></div><h2 tabIndex={-1} ref={detailHeading}>{detail.title}</h2><dl className="news-timestamps"><div><dt>{detail.material_quality?.freshness?.clock_anomaly ? "来源标注时间（未确认）" : detail.material_quality?.freshness?.revision_after_discovery ? "当前版本标注时间" : "原文发布"}</dt><dd>{timeLabel(detail.published_at, "未提供")}</dd></div><div><dt>本机发现</dt><dd>{timeLabel(detail.discovered_at)}</dd></div></dl><MaterialQuality quality={detail.material_quality} /><section className="news-analysis"><h3>本机整理要点 <span>未使用 AI</span></h3><p>{detail.summary || "来源没有提供可整理的摘要，请查看原文。"}</p>{detail.keywords?.length ? <div className="news-keywords">{detail.keywords.map((word) => <span key={word}>{word}</span>)}</div> : null}<small>按原文与关键词整理，关注标签不代表事实已核验。</small></section><section className="news-excerpt"><h3>来源摘录</h3><p>{detail.excerpt || "该来源未提供正文摘录。"}</p></section>{detail.limitations?.length ? <section className="news-limitations"><h3>阅读提示</h3><ul>{detail.limitations.map((entry, index) => <li key={`${index}-${entry}`}>{entry}</li>)}</ul></section> : null}{originalUrl ? <a className="news-button news-original" href={originalUrl} target="_blank" rel="noopener noreferrer">打开官方原文<ArrowUpRight size={17} aria-hidden="true" /></a> : <p>原文链接不可用。</p>}<AIReading analysis={detail.ai_analysis} materialQuality={detail.material_quality} configured={!!ai?.configured} busy={pending === "ai-analyze"} onAnalyze={() => void aiAction("analyze")} />{typeof onPrepareResearch === "function" ? <section className="news-research-handoff" aria-label="带到讨论房间"><h3>继续研究这条消息</h3><p>将已保存的摘录带入房间共享资料，核对后再保存；此操作不会调用 AI。</p>{!researchOpen ? <button ref={researchTrigger} className="news-button" type="button" onClick={() => { setResearchRoomId(""); setResearchError(""); setResearchOpen(true); }}><MessageSquare size={16} aria-hidden="true" />带到讨论房间</button> : <div className="news-research-picker"><label><span>选择接收资料的房间</span><select ref={researchRoomSelect} aria-label="选择接收资料的房间" value={researchRoomId} disabled={researchBusy} onChange={(event) => setResearchRoomId(event.target.value)}><option value="">请选择房间</option>{rooms.filter((entry) => entry?.id && entry?.title).map((entry) => <option key={entry.id} value={entry.id}>{entry.title}</option>)}</select></label>{!rooms.length ? <p>还没有讨论房间。可先通过页面上方“讨论房间”创建，再回来添加这条消息。</p> : null}<div className="news-research-actions"><button className="news-button news-button-primary" type="button" disabled={!researchRoomId || researchBusy} onClick={() => void prepareResearch()}>{researchBusy ? "正在打开房间…" : "打开资料草稿"}</button><button className="news-button" type="button" onClick={cancelResearch}>取消交接</button></div>{researchError ? <p className="news-source-error" role="alert">{researchError}</p> : null}</div>}</section> : <p className="news-ai-note">需要综合多条消息研判，可进入讨论房间继续研究。</p>}</article> : <div className="news-empty news-detail-empty"><Search size={32} aria-hidden="true" /><h2>选择一条消息开始阅读</h2><p>这里会展示来源摘录、整理要点和原文入口。</p><span><Clock3 size={15} aria-hidden="true" />发布时间与发现时间分别记录</span></div>}
      </aside>
    </section>
  </main>;
}
