import {
  AlertTriangle,
  ClipboardList,
  ExternalLink,
  LoaderCircle,
  RefreshCw,
  Search,
  ShieldCheck,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ACTION_DESK_STATE_LABELS, ACTION_DESK_STATES } from "../actionDesk";
import {
  filterActionDeskOverviewItems,
  normalizeActionDeskOverviewResponse,
} from "../actionOverview";
import { api } from "../api";
import "../styles/action-overview.css";
import { useModalFocus } from "../useModalFocus";

const EMPTY_LOAD_STATE = Object.freeze({ status: "idle", overview: null, error: "" });
const EMPTY_NAVIGATION_STATE = Object.freeze({ status: "idle", key: "", error: "" });

function shortHash(value) {
  const hash = String(value || "").trim();
  return hash.length === 64 ? `${hash.slice(0, 8)}…${hash.slice(-6)}` : "未封印";
}

function formatTimestamp(value) {
  const timestamp = Number(value);
  if (!Number.isFinite(timestamp) || !Number.isFinite(new Date(timestamp).getTime())) return "时间未核验";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(timestamp));
}

function OverviewItem({ item, opening, navigationLocked, onOpen }) {
  return (
    <article className={`action-overview-item state-${item.state}`}>
      <header>
        <span className="action-overview-room-tag">{item.roomTitle}</span>
        <span className={`action-overview-state state-${item.state}`}>
          {ACTION_DESK_STATE_LABELS[item.state]}
        </span>
      </header>
      <p>{item.text}</p>
      <div className="action-overview-source">
        <strong>{item.artifactTitle} · v{item.artifactVersion}</strong>
        <small>精确来源 · {item.actionId}</small>
        <code title={item.actionSnapshotSha256}>{shortHash(item.actionSnapshotSha256)}</code>
      </div>
      <dl>
        <div><dt>负责人</dt><dd>{item.owner || "待分配"}</dd></div>
        <div><dt>期限</dt><dd>{item.due || "未设置"}</dd></div>
        <div><dt>进展</dt><dd>{item.note || "尚无进展说明"}</dd></div>
        <div><dt>更新</dt><dd>{formatTimestamp(item.updatedAt)}</dd></div>
      </dl>
      <footer>
        <span className={item.sourceCurrent ? "current" : "historical"}>
          {item.sourceCurrent
            ? "当前确认版本"
            : `历史确认版本 · 当前 v${item.currentArtifactVersion}`}
        </span>
        <button
          className="secondary compact"
          type="button"
          disabled={navigationLocked}
          onClick={onOpen}
        >
          {opening ? <LoaderCircle className="spin" size={14} /> : <ExternalLink size={14} />}
          {opening ? "正在打开…" : "打开对应行动台"}
        </button>
      </footer>
    </article>
  );
}

export function ActionOverviewDrawer({ open, onClose, onOpenRoom, restoreFocusRef }) {
  const openRef = useRef(Boolean(open));
  openRef.current = Boolean(open);
  const requestRef = useRef({ sequence: 0, controller: null });
  const [loadState, setLoadState] = useState(EMPTY_LOAD_STATE);
  const [navigation, setNavigation] = useState(EMPTY_NAVIGATION_STATE);
  const [stateFilter, setStateFilter] = useState("all");
  const [query, setQuery] = useState("");
  const drawerRef = useRef(null);
  const closeButtonRef = useRef(null);
  useModalFocus({
    open,
    containerRef: drawerRef,
    initialFocusRef: closeButtonRef,
    restoreFallbackRef: restoreFocusRef,
    onClose,
  });

  const loadOverview = useCallback(async () => {
    const previous = requestRef.current;
    previous.controller?.abort();
    const controller = new AbortController();
    const sequence = previous.sequence + 1;
    requestRef.current = { sequence, controller };
    setLoadState({ status: "loading", overview: null, error: "" });
    try {
      const payload = await api.actionDeskOverview(controller.signal);
      if (
        controller.signal.aborted
        || requestRef.current.sequence !== sequence
        || !openRef.current
      ) return false;
      const overview = normalizeActionDeskOverviewResponse(payload);
      setLoadState({
        status: overview.valid ? "ready" : "integrity_failed",
        overview,
        error: "",
      });
      return overview.valid;
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") return false;
      if (requestRef.current.sequence !== sequence || !openRef.current) return false;
      setLoadState({
        status: "error",
        overview: null,
        error: error?.message || "行动总览暂时无法读取。",
      });
      return false;
    }
  }, []);

  useEffect(() => {
    if (!open) {
      const previous = requestRef.current;
      previous.controller?.abort();
      requestRef.current = { sequence: previous.sequence + 1, controller: null };
      setNavigation(EMPTY_NAVIGATION_STATE);
      return undefined;
    }
    setNavigation(EMPTY_NAVIGATION_STATE);
    void loadOverview();
    return () => requestRef.current.controller?.abort();
  }, [loadOverview, open]);

  const overview = loadState.overview;
  const visibleItems = useMemo(
    () => filterActionDeskOverviewItems(overview?.items, { state: stateFilter, query }),
    [overview?.items, query, stateFilter],
  );

  const openRoom = async (roomId, key) => {
    if (navigation.status === "loading") return;
    setNavigation({ status: "loading", key, error: "" });
    try {
      const opened = await onOpenRoom?.(roomId);
      if (!openRef.current) return;
      setNavigation(opened
        ? EMPTY_NAVIGATION_STATE
        : { status: "error", key, error: "对应房间未能安全打开，请重试。" });
    } catch (error) {
      if (!openRef.current) return;
      setNavigation({
        status: "error",
        key,
        error: error?.message || "对应房间未能安全打开，请重试。",
      });
    }
  };

  const counts = overview?.countsVisible ? overview.counts : null;
  return (
    <>
      <button
        className={open ? "action-overview-scrim open" : "action-overview-scrim"}
        type="button"
        tabIndex={-1}
        aria-label="关闭行动总览"
        onClick={onClose}
      />
      <section
        ref={drawerRef}
        className={open ? "action-overview-drawer open" : "action-overview-drawer"}
        role="dialog"
        aria-modal="true"
        aria-hidden={!open}
        aria-label="跨房间行动总览"
      >
        <header className="action-overview-heading">
          <span><ClipboardList size={20} /><span><strong>行动总览</strong><small>查看所有房间已采纳的行动</small></span></span>
          <button ref={closeButtonRef} className="icon-button" type="button" aria-label="关闭行动总览" onClick={onClose}><X size={18} /></button>
        </header>

        <div className="action-overview-body">
          <p className="action-overview-intro">
            这里只做跨房间查看和定位。状态修改仍需进入对应房间的行动台，由你亲自完成。
          </p>

          {loadState.status === "loading" ? (
            <p className="action-overview-notice" aria-live="polite"><LoaderCircle className="spin" size={16} />正在汇总已采纳行动…</p>
          ) : null}
          {loadState.status === "error" ? (
            <p className="action-overview-notice error" role="alert">
              <AlertTriangle size={16} />
              <span><strong>行动总览读取失败</strong><small>{loadState.error}</small></span>
              <button className="secondary compact" type="button" onClick={() => void loadOverview()}><RefreshCw size={14} />重试</button>
            </p>
          ) : null}
          {loadState.status === "integrity_failed" ? (
            <p className="action-overview-notice error" role="alert">
              <AlertTriangle size={16} />
              <span><strong>行动总览校验失败</strong><small>结构、计数或安全边界不可信，全部行动与汇总数字已隐藏。</small></span>
            </p>
          ) : null}

          {loadState.status === "ready" && overview?.valid ? (
            <>
              {!overview.integrityOk ? (
                <p className="action-overview-notice warning" role="note">
                  <AlertTriangle size={16} />
                  <span><strong>部分跨房间数据需要复核</strong><small>已验证房间仍可查看；异常房间不会展示或推断任何行动内容。</small></span>
                </p>
              ) : null}

              {counts ? (
                <div className="action-overview-counts" aria-label="行动总览计数">
                  <span><small>已采纳</small><strong>{counts.itemCount}</strong></span>
                  <span><small>待处理</small><strong>{counts.openCount}</strong></span>
                  <span><small>进行中</small><strong>{counts.inProgressCount}</strong></span>
                  <span><small>受阻</small><strong>{counts.blockedCount}</strong></span>
                  <span><small>已完成</small><strong>{counts.doneCount}</strong></span>
                </div>
              ) : null}

              {overview.failedRooms.length ? (
                <div className="action-overview-room-warnings" aria-label="需要复核的房间">
                  {overview.failedRooms.map((failedRoom) => {
                    const warningKey = `warning:${failedRoom.roomId}`;
                    const opening = navigation.status === "loading" && navigation.key === warningKey;
                    return (
                      <article key={warningKey} role="alert">
                        <AlertTriangle size={16} />
                        <span><strong>{failedRoom.roomTitle}</strong><small>该房间行动数据完整性未确认，内容已隐藏，需进入房间复核。</small></span>
                        <button
                          className="secondary compact"
                          type="button"
                          disabled={navigation.status === "loading"}
                          onClick={() => void openRoom(failedRoom.roomId, warningKey)}
                        >
                          {opening ? <LoaderCircle className="spin" size={14} /> : <ExternalLink size={14} />}
                          {opening ? "正在打开…" : "进入房间复核"}
                        </button>
                      </article>
                    );
                  })}
                </div>
              ) : null}

              <div className="action-overview-filters">
                <label className="action-overview-search">
                  <Search size={16} />
                  <input
                    value={query}
                    maxLength={200}
                    placeholder="搜索行动、负责人或来源"
                    onChange={(event) => setQuery(event.target.value)}
                  />
                </label>
                <label>
                  <span>状态</span>
                  <select value={stateFilter} onChange={(event) => setStateFilter(event.target.value)}>
                    <option value="all">全部状态</option>
                    {ACTION_DESK_STATES.map((state) => (
                      <option value={state} key={state}>{ACTION_DESK_STATE_LABELS[state]}</option>
                    ))}
                  </select>
                </label>
              </div>

              <div className="action-overview-results-heading">
                <strong>已采纳行动</strong>
                <span>{visibleItems.length} / {overview.items.length} 项</span>
              </div>
              <div className="action-overview-list">
                {visibleItems.map((item) => (
                  <OverviewItem
                    key={item.overviewKey}
                    item={item}
                    navigationLocked={navigation.status === "loading"}
                    opening={navigation.status === "loading" && navigation.key === item.overviewKey}
                    onOpen={() => void openRoom(item.roomId, item.overviewKey)}
                  />
                ))}
                {!visibleItems.length ? (
                  <p className="action-overview-empty">
                    {overview.items.length ? "没有符合当前筛选条件的行动。" : "目前还没有已采纳的跨房间行动。"}
                  </p>
                ) : null}
              </div>
              {navigation.status === "error" ? (
                <p className="action-overview-notice error" role="alert"><AlertTriangle size={16} />{navigation.error}</p>
              ) : null}
            </>
          ) : null}
        </div>

        <footer className="action-overview-boundary">
          <ShieldCheck size={15} />只读查看，不自动排名、不生成赢家、不修改行动，也不会启动讨论。
        </footer>
      </section>
    </>
  );
}
