import {
  Activity,
  AlertTriangle,
  Bell,
  BellOff,
  Check,
  ClipboardCopy,
  ExternalLink,
  FileJson2,
  FilePlus2,
  Inbox,
  Link2,
  LoaderCircle,
  Layers3,
  Paperclip,
  RefreshCw,
  Search,
  ShieldAlert,
  Upload,
  X,
} from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import dutyCatArt from "../assets/duty-cat.png";
import { api } from "../api";
import {
  EXTERNAL_UNVERIFIED,
  normalizeSourceInboxItem,
  normalizeSourceInboxResponse,
  normalizeSourceMonitoringHealth,
  replaceSourceInboxItem,
  SOURCE_INBOX_FILTERS,
  SOURCE_INBOX_STATE_LABELS,
  SOURCE_MONITORING_HEALTH_LABELS,
  sourceInboxItemPermissions,
  sourceMonitoringCheckLabel,
  sourceMonitoringNextStep,
  sourceMonitoringOperationState,
} from "../sourceInbox";
import {
  normalizeSourceImportPreview,
  normalizeSourceImportResult,
  normalizeSourceMonitoringPromptTemplate,
  SOURCE_IMPORT_MAX_BYTES,
  sourceImportUtf8Bytes,
} from "../sourceInboxImport";
import {
  normalizeSourceMonitoringEnablementResult,
  normalizeSourceMonitoringOperatorControl,
  normalizeSourceMonitoringOperatorPreview,
  sourceMonitoringConfirmationText,
} from "../sourceMonitoringControl";
import "../styles/source-inbox.css";
import { useModalFocus } from "../useModalFocus";

const EMPTY_LIST_STATE = Object.freeze({
  status: "idle",
  items: [],
  counts: {},
  totalCount: 0,
  unreadCount: 0,
  matchedCount: 0,
  sourceFacets: [],
  error: "",
});

const EMPTY_DETAIL_STATE = Object.freeze({
  status: "idle",
  item: null,
  error: "",
});

const EMPTY_ACTION_STATE = Object.freeze({
  status: "idle",
  type: "",
  error: "",
});

const EMPTY_HEALTH_STATE = Object.freeze({
  status: "idle",
  health: null,
  error: "",
});

const EMPTY_OPERATOR_CONTROL_STATE = Object.freeze({
  status: "idle",
  control: null,
  error: "",
});

const EMPTY_OPERATOR_ACTION_STATE = Object.freeze({
  status: "idle",
  adapterKey: "",
  intent: "",
  preview: null,
  confirmed: false,
  error: "",
  feedback: "",
});

const INITIALIZATION_STATUS_LABELS = Object.freeze({
  required: "首次初始化待预览",
  authorized: "首次初始化已授权、待 Runtime 消费",
  complete: "首次初始化收据已记录",
  legacy: "历史状态（模式未知）",
});

const EMPTY_IMPORT_PREVIEW_STATE = Object.freeze({
  status: "idle",
  preview: null,
  contentSnapshot: "",
  issues: [],
  error: "",
});

const EMPTY_IMPORT_ACTION_STATE = Object.freeze({
  status: "idle",
  issues: [],
  error: "",
});

const EMPTY_PROMPT_TEMPLATE_STATE = Object.freeze({
  status: "idle",
  template: null,
  error: "",
  feedback: "",
});

function errorMessage(error, fallback) {
  const message = error instanceof Error ? error.message.trim() : "";
  return (message || fallback).slice(0, 1000);
}

function formatServerTime(value) {
  const timestamp = Number(value);
  if (!Number.isFinite(timestamp) || timestamp <= 0) return "服务端接收时间不可用";
  const date = new Date(timestamp);
  if (!Number.isFinite(date.getTime())) return "服务端接收时间不可用";
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(date);
  } catch {
    return "服务端接收时间不可用";
  }
}

function shortHash(value) {
  const text = String(value || "");
  return text.length > 24 ? `${text.slice(0, 12)}…${text.slice(-10)}` : text || "未提供";
}

function DetailList({ empty, items, renderItem }) {
  if (!items.length) return <p className="source-inbox-muted">{empty}</p>;
  return <ul>{items.map(renderItem)}</ul>;
}

function SourceInboxListItem({ active, disabled, item, onSelect }) {
  return (
    <button
      className={`${active ? "source-inbox-row active" : "source-inbox-row"}${item.valid ? "" : " invalid"}`}
      type="button"
      aria-current={active ? "true" : undefined}
      disabled={disabled}
      onClick={onSelect}
    >
      <span className="source-inbox-row-signal" aria-hidden="true" />
      <span>
        <strong>{item.headline}</strong>
        <small>{item.sourceTierCode} · {item.sourceTierLabel} · {item.sourceKey || item.sourceChannel || "未声明来源"}</small>
        <code>{EXTERNAL_UNVERIFIED}</code>
      </span>
      <em>
        {item.acknowledged ? "已阅" : "未读"} · {item.valid
          ? (SOURCE_INBOX_STATE_LABELS[item.state] || item.state || "状态未知")
          : "记录完整性异常"}
      </em>
    </button>
  );
}

function normalizedImportIssues(error) {
  const rawIssues = Array.isArray(error?.issues)
    ? error.issues
    : Array.isArray(error?.details)
      ? error.details
      : [];
  return rawIssues.slice(0, 12).map((rawIssue, index) => {
    const issue = rawIssue && typeof rawIssue === "object" ? rawIssue : {};
    return {
      id: `${String(issue.path || "$")}:${String(issue.code || "invalid")}:${index}`,
      path: String(issue.path || "$ ").trim().slice(0, 240) || "$",
      code: String(issue.code || "SOURCE_IMPORT_INVALID").slice(0, 120),
      message: String(issue.message || "导入内容不符合固定合同。").slice(0, 500),
    };
  });
}

function SourceImportIssues({ issues }) {
  if (!issues.length) return null;
  return (
    <ul className="source-inbox-import-issues" aria-label="导入合同问题">
      {issues.map((issue) => (
        <li key={issue.id}>
          <code>{issue.path}</code>
          <strong>{issue.code}</strong>
          <span>{issue.message}</span>
        </li>
      ))}
    </ul>
  );
}

function SourceImportPreview({ headingRef, state }) {
  if (state.status === "idle") {
    return <p className="source-inbox-muted compact">输入 JSON 后先预览；正式导入仍会在事务内重新校验。</p>;
  }
  if (state.status === "loading") {
    return <p className="source-inbox-loading" role="status"><LoaderCircle aria-hidden="true" className="spin" size={15} />正在执行无写入预览…</p>;
  }
  if (state.status === "stale") {
    return <p className="source-inbox-notice error" role="alert"><AlertTriangle aria-hidden="true" size={15} />内容已更改，请重新预览。</p>;
  }
  if (state.status === "error") {
    return (
      <div className="source-inbox-import-errors" role="alert">
        <p className="source-inbox-notice error"><AlertTriangle aria-hidden="true" size={15} />{state.error}</p>
        <SourceImportIssues issues={state.issues} />
      </div>
    );
  }
  const preview = state.preview;
  if (!preview?.valid) {
    return <p className="source-inbox-notice error" role="alert">预览响应未满足固定安全合同，已停止展示。</p>;
  }
  return (
    <section className="source-inbox-import-preview" aria-label="导入预览">
      <header ref={headingRef} tabIndex={-1}>
        <span><Check aria-hidden="true" size={16} /><strong>严格合同预览通过</strong></span>
        <code>{EXTERNAL_UNVERIFIED}</code>
      </header>
      <p>
        预览未读写数据库，也未判定新增、重复或冲突；点击确认后仍会以原始文本重新校验。
      </p>
      <dl>
        <div><dt>来源</dt><dd>{preview.sourceChannel} / {preview.sourceKey}</dd></div>
        <div><dt>external run</dt><dd>{preview.externalRunId}</dd></div>
        <div><dt>时间窗口</dt><dd>{preview.cutoffAt} → {preview.checkedAt}</dd></div>
        <div><dt>计数</dt><dd>{preview.itemCount} 个 item · {preview.sourceCount} 个 source · {preview.payloadBytes} UTF-8 bytes</dd></div>
        <div><dt>语义变化</dt><dd>{preview.meaningfulChange ? "是，待人工复核" : "否，items 为空"}</dd></div>
        <div><dt>规范化哈希</dt><dd><code>{shortHash(preview.normalizedPacketSha256)}</code></dd></div>
      </dl>
      <div className="source-inbox-import-preview-items">
        {preview.items.map((item) => (
          <details key={item.fingerprint}>
            <summary>
              <span><strong>{item.index + 1}. {item.headline}</strong><small>{item.itemType} · {item.severity}</small></span>
              <em>{item.sourceCount} 个来源</em>
            </summary>
            <p>{item.summary}</p>
            <small>{item.occurredAt} · 路由建议 {item.recommendedRoute}</small>
            <small>{item.factCount} 个事实 · {item.hypothesisCount} 个假设 · {item.unknownCount} 个未知项</small>
            <ul>
              {item.sources.map((source, sourceIndex) => (
                <li key={`${item.fingerprint}:${sourceIndex}`}>
                  <strong>{source.publisher}</strong>
                  <code>{source.url}</code>
                </li>
              ))}
            </ul>
          </details>
        ))}
      </div>
    </section>
  );
}

function DeterministicImpactSection({ item }) {
  const validProjections = item.impactRuleProjections.filter((projection) => projection.valid === true);
  const invalidProjectionCount = item.impactRuleProjections.length - validProjections.length;
  const hypotheses = invalidProjectionCount > 0
    ? []
    : validProjections.flatMap((projection) => projection.hypotheses);
  return (
    <section className="source-inbox-section source-inbox-impact-rules">
      <h3><Layers3 aria-hidden="true" size={16} />确定性研究影响映射</h3>
      <p className="source-inbox-impact-boundary">
        这里只展示固定规则对研究范围的映射；不是方向预测、因果结论、盈利声明或执行授权。
      </p>
      {invalidProjectionCount > 0 ? (
        <p className="source-inbox-integrity-error compact" role="alert">
          影响映射完整性校验失败，已停止展示该投影内容。
        </p>
      ) : null}
      {item.impactEvaluationState === "not_evaluated" ? (
        <p className="source-inbox-muted compact">
          未评估：规则功能可能处于默认关闭状态，或该历史条目按不回填策略保留为空。
        </p>
      ) : null}
      {item.impactEvaluationState === "no_match" ? (
        <p className="source-inbox-muted compact">已执行固定规则，但没有匹配映射；这不等于“没有影响”。</p>
      ) : null}
      {hypotheses.length ? (
        <ul className="source-inbox-impact-list">
          {hypotheses.map((hypothesis) => (
            <li key={hypothesis.id}>
              <span className="source-inbox-impact-scope">
                <strong>{hypothesis.areaKind === "sector" ? "行业" : "标的"} · {hypothesis.areaId.toUpperCase()}</strong>
                {hypothesis.securityIds.length ? <small>{hypothesis.securityIds.join(" · ")}</small> : null}
              </span>
              <span>{hypothesis.statement}</span>
              <small>
                {hypothesis.timeHorizon || "时间范围未提供"} · 固定规则覆盖度 {Math.round(hypothesis.confidence * 100)}% · 反证未知
              </small>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function SourceInboxDetail({
  actionState,
  acknowledgementChecked,
  item,
  objective,
  onAcknowledge,
  onAcknowledgementChange,
  onAttach,
  onCreateDraft,
  onCopyDeepLink,
  onObjectiveChange,
  onRoomChange,
  roomId,
  rooms,
}) {
  const permissions = sourceInboxItemPermissions(item, roomId);
  const busy = actionState.status === "loading";
  const selectedRoom = rooms.find((room) => room.id === roomId) || null;

  return (
    <article className="source-inbox-detail">
      <header className="source-inbox-detail-heading">
        <span className="source-inbox-source-icon"><Inbox aria-hidden="true" size={20} /></span>
        <span>
          <strong>{item.headline}</strong>
          <small>{item.sourceChannel || "未声明来源通道"} · 服务端已接收</small>
        </span>
        <em>{SOURCE_INBOX_STATE_LABELS[item.state] || item.state || "状态未知"}</em>
      </header>

      <div className="source-inbox-unverified" role="note">
        <ShieldAlert aria-hidden="true" size={17} />
        <span>
          <strong>{EXTERNAL_UNVERIFIED}</strong>
          <small>来源内容、时间、事实声明与影响假设均未被本地系统核验。</small>
        </span>
      </div>

      {!item.valid ? (
        <div className="source-inbox-integrity-error" role="alert">
          <AlertTriangle aria-hidden="true" size={17} />
          <span>
            <strong>记录完整性校验失败，所有动作已锁定</strong>
            <small>响应未满足固定的 external_unverified / 零执行安全契约。请刷新或检查服务端记录。</small>
          </span>
        </div>
      ) : null}

      <section className="source-inbox-section">
        <h3><Link2 aria-hidden="true" size={16} />来源与谱系</h3>
        <dl className="source-inbox-provenance">
          <div><dt>服务端接收</dt><dd>{formatServerTime(item.receivedAt)}</dd></div>
          <div><dt>来源指纹</dt><dd><code title={item.serverFingerprint}>{shortHash(item.serverFingerprint)}</code></dd></div>
          <div><dt>来源键</dt><dd>{item.sourceKey || "未提供"}</dd></div>
          <div><dt>接入层级</dt><dd>{item.sourceTierCode} · {item.sourceTierLabel}（不是事实认证）</dd></div>
          <div><dt>外部运行 ID</dt><dd>{item.externalRunId || "未提供"}</dd></div>
        </dl>
        <button className="secondary compact source-inbox-copy-link" type="button" onClick={onCopyDeepLink}>
          <ClipboardCopy aria-hidden="true" size={14} />复制此事件链接
        </button>
        <DetailList
          empty="未提供可展示的来源链接。"
          items={item.sources}
          renderItem={(source, index) => (
            <li key={`${source.url}-${index}`} className="source-inbox-source-link">
              <span>
                <strong>{source.publisher || "未声明发布者"}</strong>
                <small>{source.sourceType || "未声明来源类型"} · {EXTERNAL_UNVERIFIED}</small>
              </span>
              {source.url ? (
                <a href={source.url} target="_blank" rel="noreferrer noopener">
                  打开外部链接<ExternalLink aria-hidden="true" size={14} />
                </a>
              ) : null}
            </li>
          )}
        />
      </section>

      <DeterministicImpactSection item={item} />

      <section className="source-inbox-section">
        <h3><Search aria-hidden="true" size={16} />外部声明与影响假设</h3>
        {item.summary ? <p className="source-inbox-summary">{item.summary}</p> : null}
        <h4>事实声明（尚未核验）</h4>
        <DetailList
          empty="未提供事实声明。"
          items={item.facts}
          renderItem={(fact, index) => <li key={`${fact.claim}-${index}`}>{fact.claim}</li>}
        />
        <h4>影响假设（待验证）</h4>
        <DetailList
          empty="未提供影响假设。"
          items={item.impactHypotheses}
          renderItem={(hypothesis, index) => (
            <li key={`${hypothesis.statement}-${index}`}>
              <span>{hypothesis.statement}</span>
              {(hypothesis.affectedArea || hypothesis.timeHorizon) ? (
                <small>{[hypothesis.affectedArea, hypothesis.timeHorizon].filter(Boolean).join(" · ")}</small>
              ) : null}
            </li>
          )}
        />
        <h4>未知项</h4>
        <DetailList
          empty="未列出未知项；这不表示内容已被核验。"
          items={item.unknowns}
          renderItem={(unknown, index) => <li key={`${unknown}-${index}`}>{unknown}</li>}
        />
      </section>

      <section className="source-inbox-section source-inbox-acknowledgement">
        <label>
          <input
            type="checkbox"
            checked={item.acknowledged || acknowledgementChecked}
            disabled={item.acknowledged || busy || !permissions.actionable}
            onChange={(event) => onAcknowledgementChange(event.target.checked)}
          />
          <span>
            <strong>已阅，不代表事实确认</strong>
            <small>确认只表示你读过该来源，不代表内容为事实、已验证、已批准或可执行。</small>
          </span>
        </label>
        <button
          className="secondary"
          type="button"
          disabled={!permissions.canAcknowledge || !acknowledgementChecked || busy}
          onClick={onAcknowledge}
        >
          {actionState.type === "acknowledge" && busy
            ? <LoaderCircle aria-hidden="true" className="spin" size={15} />
            : <Check aria-hidden="true" size={15} />}
          {item.acknowledged ? "已记录为已阅" : "记录已阅"}
        </button>
      </section>

      <section className="source-inbox-section source-inbox-room-actions">
        <h3><Paperclip aria-hidden="true" size={16} />附加到房间</h3>
        <label>
          <span>目标房间（手动选择）</span>
          <select value={roomId} onChange={(event) => onRoomChange(event.target.value)}>
            <option value="">选择目标房间</option>
            {rooms.map((room) => (
              <option key={room.id} value={room.id}>
                {room.title}{room.current ? "（当前房间）" : ""}
              </option>
            ))}
          </select>
        </label>
        <button
          className="secondary"
          type="button"
          disabled={!permissions.canAttach || busy}
          onClick={onAttach}
        >
          {actionState.type === "attach" && busy
            ? <LoaderCircle aria-hidden="true" className="spin" size={15} />
            : <Paperclip aria-hidden="true" size={15} />}
          {permissions.attachment ? "已附加到该房间" : "附加到房间"}
        </button>
        {!item.acknowledged ? <small>先完成“已阅，不代表事实确认”。</small> : null}
      </section>

      <section className="source-inbox-section source-inbox-draft-actions">
        <h3><FilePlus2 aria-hidden="true" size={16} />轮次草稿</h3>
        <label>
          <span>草稿目标（可选）</span>
          <textarea
            value={objective}
            maxLength={2000}
            placeholder="补充待讨论问题；内容仍是草稿。"
            onChange={(event) => onObjectiveChange(event.target.value)}
          />
        </label>
        <button
          className="primary"
          type="button"
          disabled={!permissions.canDraft || busy}
          onClick={onCreateDraft}
        >
          {actionState.type === "draft" && busy
            ? <LoaderCircle aria-hidden="true" className="spin" size={15} />
            : <FilePlus2 aria-hidden="true" size={15} />}
          {permissions.roundDraft ? "该房间已有 round draft" : "仅生成 round draft"}
        </button>
        <p className="source-inbox-draft-boundary">
          草稿不启动 Provider，不创建正式 round，不读取市场，也不授予执行权限。
          {selectedRoom ? ` 当前目标：${selectedRoom.title}。` : ""}
        </p>
      </section>
    </article>
  );
}

function sourceMonitoringInitializationPolicy(settings, adapter) {
  const initialMode = adapter?.initializationMode || settings?.initialMode || "";
  return {
    initialMode,
    catchUpMaxItems: initialMode === "catch_up" ? settings?.catchUpMaxItems || 0 : 0,
    fromTime: initialMode === "from_time" ? settings?.fromTime || "" : "",
  };
}

function SourceMonitoringOperatorControls({
  actionState,
  controlState,
  onCancel,
  onConfirmationChange,
  onLoad,
  onPrepareEnable,
  onPrepareDisable,
  onPreview,
  onSubmit,
  previewHeadingRef,
}) {
  if (controlState.status === "idle") {
    return (
      <section className="source-monitoring-controls" aria-label="Adapter 接入设置">
        <p>接入设置仅在你明确读取后显示；打开来源收件箱不会轮询任何外部来源。</p>
        <button className="secondary compact" type="button" onClick={onLoad}>查看 Adapter 接入设置</button>
      </section>
    );
  }
  if (controlState.status === "loading") {
    return <p className="source-inbox-loading" role="status"><LoaderCircle aria-hidden="true" className="spin" size={14} />正在读取 Adapter 接入设置…</p>;
  }
  if (controlState.status === "error" || !controlState.control?.valid) {
    return (
      <section className="source-monitoring-controls" aria-label="Adapter 接入设置">
        <p className="source-inbox-notice error" role="alert">
          {controlState.error || "Adapter 接入设置未满足固定安全合同。"}
        </p>
        <button className="secondary compact" type="button" onClick={onLoad}>重新读取接入设置</button>
      </section>
    );
  }

  const { control } = controlState;
  const selected = control.adapters.find(
    (adapter) => adapter.adapterKey === actionState.adapterKey,
  ) || null;
  const selectedPolicy = sourceMonitoringInitializationPolicy(
    control.settings,
    selected,
  );
  const preview = actionState.preview;
  const previewUsable = preview?.valid === true
    && !preview.initializationBlocked
    && preview.adapterKey === selected?.adapterKey
    && preview.configVersion === selected?.configVersion
    && preview.stateVersion === selected?.stateVersion
    && preview.initialRequired === (selected?.initializationStatus === "required")
    && preview.mode === selectedPolicy.initialMode
    && preview.catchUpMaxItems === selectedPolicy.catchUpMaxItems
    && preview.fromTime === selectedPolicy.fromTime;
  const enableWithoutPreview = actionState.intent === "enable"
    && selected
    && selected.initializationStatus !== "required";
  const canSubmit = actionState.confirmed
    && actionState.status !== "loading"
    && selected
    && (actionState.intent === "disable" || previewUsable || enableWithoutPreview);
  const confirmationText = actionState.intent === "disable"
    ? "我确认只停止该 Adapter 的后续轮询；不会删除 checkpoint、初始化收据或 Source Inbox 记录。"
    : previewUsable
      ? sourceMonitoringConfirmationText(preview)
      : enableWithoutPreview
        ? "我确认保存该 Adapter 的启用状态；这不会证明 Runtime 已启动或来源可用。"
        : "";

  return (
    <section
      className="source-monitoring-controls"
      aria-label="Adapter 接入设置"
      aria-busy={actionState.status === "loading"}
    >
      <header>
        <span>
          <strong>Adapter 接入设置</strong>
          <small>
            全局{control.settings.globalEnabled ? "已启用" : "关闭"}
            {control.settings.autoStart ? " · auto-start 已启用" : " · auto-start 关闭"}
            {control.settings.dryRun ? " · dry-run" : ""}
          </small>
        </span>
        <button className="secondary compact" type="button" onClick={onLoad}>重读设置</button>
      </header>
      {control.profile ? (
        <div role="note" aria-label="官方来源试用范围">
          <strong>{control.profile.label}</strong>
          {control.profile.sources.map((source) => (
            <p key={source.adapterKey}>
              {source.adapterKey === "sec_filings" ? "SEC" : "Micron 官方 IR"}
              {` · ${source.symbols.join("、")}`}
              {source.forms.length ? ` · ${source.forms.join("、")}` : null}
              {source.format === "micron_q4_public_json_v1" ? ` · Q4 公开 JSON 最近 ${source.historyLimit} 条` : null}
              {` · 每次最多 ${source.perSymbolLimit} 条 · 每 ${source.pollIntervalMs / 60_000} 分钟轮询`}
            </p>
          ))}
          <p>仅建基线：首次运行不补录历史。采集/草稿不调用模型；不授予交易权限。</p>
        </div>
      ) : null}
      <p>
        保存 Adapter 启用状态不证明 Runtime 在线；预览只读取固定来源，不写 checkpoint 或 Source Inbox，
        不调用 Provider，也不授予交易权限。
      </p>
      <p role="note">确认首次启用时，服务端会重读同一固定来源并核对预览哈希；来源证据漂移时不会保存启用状态。</p>
      {control.adapters.some((adapter) => adapter.sourceClass === "readonly_market") ? (
        <p role="note">readonly_market 首次只预览静态政策，不读取行情；首次 Runtime 运行时才执行受控只读轮询。</p>
      ) : null}
      {!control.settings.autoStart ? (
        <p role="note">auto-start 当前关闭；保存启用状态后不会自动轮询。</p>
      ) : null}
      {control.settings.dryRun ? (
        <p role="note">dry-run 当前启用；轮询只生成预览证据，不提交首次 checkpoint，也不写 Source Inbox。</p>
      ) : null}
      <div className="source-monitoring-control-list" role="list">
        {control.adapters.map((adapter) => (
          <article key={adapter.adapterKey} role="listitem">
            <header>
              <span><strong>{adapter.adapterKey}</strong><small>{adapter.sourceClass}</small></span>
              <em>{adapter.persistedEnabled ? "持久化启用" : "持久化关闭"}</em>
            </header>
            <small>
              {INITIALIZATION_STATUS_LABELS[adapter.initializationStatus] || "初始化状态异常"}
              {adapter.initializationMode ? ` · ${adapter.initializationMode}` : ""}
              {adapter.activeRun ? " · 当前有运行记录" : ""}
            </small>
            {adapter.initializationStatus === "legacy" ? (
              <small role="note">
                {adapter.adapterKey === "sec_filings" ? "旧 SEC 状态" : "历史状态"}无法证明完整首次基线；先核对升级方案，保留现有 checkpoint 与收件箱，不自动重置或迁移。
              </small>
            ) : null}
            {adapter.blockedReasonCodes.length ? (
              <small className="source-monitoring-blockers" role="note">
                阻断：{adapter.blockedReasonCodes.join(" · ")}
              </small>
            ) : null}
            <footer>
              {adapter.canPreview ? (
                <button
                  className="secondary compact"
                  type="button"
                  disabled={actionState.status === "loading"}
                  onClick={() => onPreview(adapter)}
                >预览首次读取范围</button>
              ) : null}
              {adapter.canEnable && !adapter.canPreview ? (
                <button
                  className="secondary compact"
                  type="button"
                  disabled={actionState.status === "loading"}
                  onClick={() => onPrepareEnable(adapter)}
                >准备启用</button>
              ) : null}
              {adapter.canDisable ? (
                <button
                  className="secondary compact"
                  type="button"
                  disabled={actionState.status === "loading"}
                  onClick={() => onPrepareDisable(adapter)}
                >准备停用</button>
              ) : null}
            </footer>
          </article>
        ))}
      </div>
      {actionState.status === "loading" ? (
        <p className="source-inbox-loading" role="status"><LoaderCircle aria-hidden="true" className="spin" size={14} />正在处理显式请求…</p>
      ) : null}
      {actionState.error ? <p className="source-inbox-notice error" role="alert">{actionState.error}</p> : null}
      {actionState.feedback ? <p className="source-inbox-notice" role="status" aria-live="polite">{actionState.feedback}</p> : null}
      {preview?.valid ? (
        <section className="source-monitoring-preview" aria-label="首次初始化预览">
          <header ref={previewHeadingRef} tabIndex={-1}>
            <strong>首次读取范围预览</strong>
            <code>{shortHash(preview.previewSha256)}</code>
          </header>
          {["sec_filings", "company_ir"].includes(preview.adapterKey) && preview.mode === "seed_only" ? (
            <p role="note">下方数量是本批内容预览，完整基线的公告 ID 数可能更多；基线仅覆盖所选来源本次取得的完整 recent 列表或 RSS，不代表全部历史公告。</p>
          ) : null}
          <dl>
            <div><dt>模式</dt><dd>{preview.mode}</dd></div>
            <div><dt>候选</dt><dd>{preview.candidateCount}</dd></div>
            <div><dt>将处理</dt><dd>{preview.selectedCount}</dd></div>
            <div><dt>将跳过</dt><dd>{preview.skippedCount}</dd></div>
            <div><dt>重复候选</dt><dd>{preview.adapterDuplicateCount}</dd></div>
            <div><dt>来源异常 / 拒绝</dt><dd>{preview.sourceErrorCount} / {preview.rejectedCount}</dd></div>
            <div><dt>只读市场调用</dt><dd>{preview.marketCallsPerformed}</dd></div>
            <div><dt>模式参数</dt><dd>{preview.mode === "catch_up"
              ? `最多 ${preview.catchUpMaxItems} 条`
              : preview.mode === "from_time"
                ? preview.fromTime
                : "只建立基线"}</dd></div>
            <div><dt>捕获时间</dt><dd>{formatServerTime(preview.capturedAt)}</dd></div>
            <div><dt>最早时间</dt><dd>{preview.earliestOccurredAt || "无候选"}</dd></div>
            <div><dt>最晚时间</dt><dd>{preview.latestOccurredAt || "无候选"}</dd></div>
            <div><dt>起始 checkpoint</dt><dd><code>{shortHash(preview.startingCheckpointSha256)}</code></dd></div>
            <div><dt>下一 checkpoint</dt><dd><code>{shortHash(preview.nextCheckpointSha256)}</code></dd></div>
          </dl>
          {preview.initializationBlocked ? (
            <p className="source-inbox-notice error" role="alert">
              预览包含 {preview.sourceErrorCount} 个来源错误和 {preview.rejectedCount} 个拒绝项，不能确认。
            </p>
          ) : null}
        </section>
      ) : null}
      {confirmationText ? (
        <fieldset className="source-monitoring-confirmation" disabled={actionState.status === "loading"}>
          <label>
            <input
              type="checkbox"
              checked={actionState.confirmed}
              onChange={(event) => onConfirmationChange(event.target.checked)}
            />
            <span>{confirmationText}</span>
          </label>
          <footer>
            <button className="secondary compact" type="button" onClick={onCancel}>取消</button>
            <button className="primary compact" type="button" disabled={!canSubmit} onClick={onSubmit}>
              {actionState.intent === "disable" ? "确认停止后续轮询" : "确认保存 Adapter 启用状态"}
            </button>
          </footer>
        </fieldset>
      ) : null}
    </section>
  );
}

function SourceMonitoringHealth({
  healthState,
  operatorActionState,
  operatorControlState,
  operatorPreviewHeadingRef,
  notificationState,
  onCancelOperatorAction,
  onLoadOperatorControl,
  onNotificationPreferenceChange,
  onOperatorConfirmationChange,
  onPrepareAdapterDisable,
  onPrepareAdapterEnable,
  onPreviewAdapterInitialization,
  onSubmitAdapterEnablement,
}) {
  const health = healthState.health;
  const control = operatorControlState.status === "ready" ? operatorControlState.control : null;
  const operation = sourceMonitoringOperationState(health, control);
  const notificationSupported = notificationState?.supported === true;
  const notificationEnabled = notificationState?.enabled === true;
  const notificationPermission = String(notificationState?.permission || "unsupported");
  const runtimeNeedsAttention = health?.valid === true && operation.state === "degraded";
  return (
    <details className="source-inbox-health">
      <summary>
        <span><Activity aria-hidden="true" size={15} /><strong>Adapter 健康</strong></span>
        <small role={runtimeNeedsAttention ? "alert" : undefined}>
          {healthState.status === "loading"
            ? "读取中"
            : health?.valid
              ? `${operation.label} · ${health.adapters.length} 个 Adapter`
              : healthState.status === "error"
                ? "读取失败"
                : "尚未读取"}
        </small>
      </summary>
      <div className="source-inbox-health-body">
        {healthState.status === "error" ? (
          <p className="source-inbox-notice error" role="alert">{healthState.error}</p>
        ) : null}
        {health && !health.valid ? (
          <p className="source-inbox-notice error" role="alert">
            Adapter 健康响应未满足零执行与 Runtime 在线性边界，未将其解释为健康状态。
          </p>
        ) : null}
        {health?.valid ? (
          <>
            <p role="note">{operation.detail}</p>
            <div className="source-inbox-health-adapters" role="group" aria-label="当前进程生效配置">
              <article>
                <header><strong>生效配置</strong><em>来源：当前启动进程</em></header>
                <small>
                  全局{health.globalEnabled ? "启用" : "关闭"} · 自动启动{health.autoStart ? "启用" : "关闭"}
                  {health.dryRun ? " · dry-run（仅预览）" : " · 导入收件箱模式"}
                </small>
                <small>
                  来源范围：{health.officialOnly ? "官方来源" : "只读行情"}
                  {health.officialOnly && health.allowReadonlyMarket ? " + 已允许的只读行情" : ""}
                  {` · 首次策略 ${health.initialMode}`}
                </small>
                <small>监控变量取自启动进程环境；写入 .env.local 不会自动生效。按启动说明设置后重启，再重读健康记录。</small>
              </article>
            </div>
            <div className="source-inbox-health-adapters" role="group" aria-label="Monitoring Runtime 状态">
              <article>
                <header>
                  <strong>Runtime</strong>
                  <em>{health.runtime.statusLabel}</em>
                </header>
                <small>
                  心跳 {formatServerTime(health.runtime.heartbeatAt)}
                  {health.runtime.activeAdapter ? ` · 活动 Adapter ${health.runtime.activeAdapter}` : " · 当前无活动 Adapter"}
                </small>
                <small>
                  下次检查 {formatServerTime(health.runtime.nextDueAt)}
                  {health.runtime.dryRun ? " · dry-run 已启用" : " · dry-run 已关闭"}
                </small>
                {["stalled", "failed"].includes(health.runtime.status) ? (
                  <small>
                    Runtime {health.runtime.status === "stalled" ? "心跳已停滞" : "运行失败"}
                    {health.runtime.lastFatalErrorCode ? ` · 错误码 ${health.runtime.lastFatalErrorCode}` : ""}。
                  </small>
                ) : null}
              </article>
            </div>
            <p className="source-inbox-health-boundary">
              捕获于 {formatServerTime(health.capturedAt)}。{health.globalEnabled ? "全局监控已启用" : "全局监控默认关闭"}
              {health.dryRun ? " · dry-run" : ""}。新鲜本机心跳只证明 worker 有进展，不证明来源可用、内容为事实或具备交易权限。
            </p>
            <div className="source-inbox-health-adapters" role="list" aria-label="Adapter 健康记录">
              {health.adapters.map((adapter) => {
                const sourceControl = control?.valid === true
                  ? control.adapters.find((entry) => entry.adapterKey === adapter.adapterKey
                    && entry.configVersion === adapter.configVersion)
                  : null;
                const nextStep = sourceMonitoringNextStep(adapter);
                return (
                <article key={adapter.adapterKey} role="listitem">
                  <header><strong>{adapter.adapterKey}</strong><em>{SOURCE_MONITORING_HEALTH_LABELS[adapter.state] || adapter.state}</em></header>
                  <small>
                    {adapter.officialSource ? "官方来源" : adapter.sourceClass === "readonly_market" ? "只读行情" : "来源未登记"}
                    {` · 生效开关${adapter.enabled ? "启用" : "关闭"}`}
                    {adapter.pollIntervalMs > 0 ? ` · 轮询周期 ${adapter.pollIntervalMs / 60_000} 分钟` : " · 轮询周期未提供"}
                  </small>
                  <small>
                    基线：{sourceControl
                      ? INITIALIZATION_STATUS_LABELS[sourceControl.initializationStatus] || "状态待核实"
                      : "状态未读取，请查看接入设置"}
                  </small>
                  <small>
                    {adapter.persistedStatePresent ? "有持久化状态" : "尚无持久化状态"}
                    {adapter.persistedStatePresent
                      ? ` · 持久化开关${adapter.persistedEnabled ? "启用" : "关闭"}`
                      : ""}
                    {adapter.configStatus ? ` · 配置 ${adapter.configStatus}` : ""}
                    {adapter.latestRunStatus ? ` · 最近运行 ${adapter.latestRunStatus}` : ""}
                  </small>
                  <small>
                    上次检查 {formatServerTime(adapter.lastCheckedAt)} · 下次检查 {formatServerTime(adapter.nextDueAt)}
                  </small>
                  <small>{sourceMonitoringCheckLabel(adapter)}</small>
                  <small>
                    最近成功 {formatServerTime(adapter.lastSuccessAt)}
                    {adapter.lastErrorCode ? ` · 错误码 ${adapter.lastErrorCode}` : ""}
                  </small>
                  {nextStep ? <small role="note">{nextStep}</small> : null}
                </article>
                );
              })}
            </div>
            <SourceMonitoringOperatorControls
              actionState={operatorActionState}
              controlState={operatorControlState}
              onCancel={onCancelOperatorAction}
              onConfirmationChange={onOperatorConfirmationChange}
              onLoad={onLoadOperatorControl}
              onPrepareDisable={onPrepareAdapterDisable}
              onPrepareEnable={onPrepareAdapterEnable}
              onPreview={onPreviewAdapterInitialization}
              onSubmit={onSubmitAdapterEnablement}
              previewHeadingRef={operatorPreviewHeadingRef}
            />
          </>
        ) : null}
        <div className="source-inbox-notifications">
          <span>
            {notificationEnabled ? <Bell aria-hidden="true" size={15} /> : <BellOff aria-hidden="true" size={15} />}
            <span>
              <strong>浏览器通知</strong>
              <small>
                {!notificationSupported
                  ? "当前浏览器不支持。"
                  : notificationPermission === "denied"
                    ? "权限已被浏览器拒绝；请在浏览器设置中调整。"
                    : notificationEnabled
                      ? "仅页面打开时提示新未读事件；通知不含外部正文。"
                      : "只在你明确启用后申请权限；历史事件不会补发。"}
              </small>
            </span>
          </span>
          <button
            className="secondary compact"
            type="button"
            disabled={!notificationSupported || notificationPermission === "denied"}
            onClick={() => onNotificationPreferenceChange?.(!notificationEnabled)}
          >
            {notificationEnabled ? "停用通知" : "启用通知"}
          </button>
        </div>
      </div>
    </details>
  );
}

export function SourceInboxPanel({
  activeRoomId = "",
  notificationState = { supported: false, permission: "unsupported", enabled: false },
  onClose,
  onCopyEventLink,
  onEventTargetChange,
  onNotificationPreferenceChange,
  onRoomAttached,
  onUnreadCountChange,
  open,
  refreshToken = 0,
  requestedItemId = "",
  restoreFocusRef,
  rooms = [],
}) {
  const panelRef = useRef(null);
  const closeButtonRef = useRef(null);
  const openRef = useRef(Boolean(open));
  const listRequestRef = useRef({ sequence: 0, controller: null });
  const healthRequestRef = useRef({ sequence: 0, controller: null });
  const operatorRequestRef = useRef({ sequence: 0, controller: null });
  const detailRequestRef = useRef({ sequence: 0, controller: null });
  const actionRequestRef = useRef({ sequence: 0, controller: null });
  const importRequestRef = useRef({ sequence: 0, controller: null });
  const promptTemplateRequestRef = useRef({ sequence: 0, controller: null });
  const previewHeadingRef = useRef(null);
  const operatorPreviewHeadingRef = useRef(null);
  const promptTextareaRef = useRef(null);
  const consumedRefreshTokenRef = useRef(0);
  const [queryInput, setQueryInput] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [stateFilter, setStateFilter] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [listState, setListState] = useState(EMPTY_LIST_STATE);
  const [healthState, setHealthState] = useState(EMPTY_HEALTH_STATE);
  const [operatorControlState, setOperatorControlState] = useState(EMPTY_OPERATOR_CONTROL_STATE);
  const [operatorActionState, setOperatorActionState] = useState(EMPTY_OPERATOR_ACTION_STATE);
  const [selectedItemId, setSelectedItemId] = useState("");
  const [detailState, setDetailState] = useState(EMPTY_DETAIL_STATE);
  const [selectedRoomId, setSelectedRoomId] = useState("");
  const [acknowledgementChecked, setAcknowledgementChecked] = useState(false);
  const [objective, setObjective] = useState("");
  const [actionState, setActionState] = useState(EMPTY_ACTION_STATE);
  const [feedback, setFeedback] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [importContent, setImportContent] = useState("");
  const [importError, setImportError] = useState("");
  const [importPreviewState, setImportPreviewState] = useState(EMPTY_IMPORT_PREVIEW_STATE);
  const [importActionState, setImportActionState] = useState(EMPTY_IMPORT_ACTION_STATE);
  const [promptTemplateOpen, setPromptTemplateOpen] = useState(false);
  const [promptTemplateState, setPromptTemplateState] = useState(EMPTY_PROMPT_TEMPLATE_STATE);
  const titleId = useId();
  const descriptionId = useId();
  const promptTemplateId = useId();
  const importTextareaId = useId();
  const importBoundaryId = useId();
  openRef.current = Boolean(open);

  const importContentBytes = useMemo(
    () => sourceImportUtf8Bytes(importContent),
    [importContent],
  );

  const roomOptions = useMemo(() => rooms
    .filter((room) => room?.id)
    .map((room) => ({
      id: String(room.id),
      title: String(room.title || room.id),
      current: String(room.id) === String(activeRoomId || ""),
    })), [activeRoomId, rooms]);

  const requestClose = useCallback(() => {
    if (typeof onClose === "function") onClose();
  }, [onClose]);

  useModalFocus({
    open,
    containerRef: panelRef,
    initialFocusRef: closeButtonRef,
    restoreFallbackRef: restoreFocusRef,
    onClose: requestClose,
  });

  useEffect(() => {
    setSelectedRoomId((current) => {
      if (roomOptions.some((room) => room.id === current)) return current;
      return "";
    });
  }, [roomOptions]);

  const loadList = useCallback(async ({ preferredItemId = "" } = {}) => {
    const previous = listRequestRef.current;
    previous.controller?.abort();
    const controller = new AbortController();
    const sequence = previous.sequence + 1;
    listRequestRef.current = { sequence, controller };
    setListState((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const payload = await api.listSourceInbox({
        state: stateFilter,
        query: submittedQuery,
        source: sourceFilter,
        unread: unreadOnly,
        limit: 100,
        signal: controller.signal,
      });
      if (
        controller.signal.aborted
        || listRequestRef.current.sequence !== sequence
        || !openRef.current
      ) return false;
      const normalized = normalizeSourceInboxResponse(payload);
      if (!normalized.valid) {
        throw new Error("来源收件箱列表响应未满足固定契约，已拒绝更新。");
      }
      setListState({ status: "ready", ...normalized, error: "" });
      if (typeof onUnreadCountChange === "function") {
        onUnreadCountChange(normalized.unreadCount);
      }
      setSelectedItemId((current) => {
        if (preferredItemId) return preferredItemId;
        if (requestedItemId) return requestedItemId;
        if (current && normalized.items.some((item) => item.id === current)) return current;
        return normalized.items[0]?.id || "";
      });
      return true;
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") return false;
      if (listRequestRef.current.sequence !== sequence || !openRef.current) return false;
      setListState((current) => ({
        ...current,
        status: "error",
        error: errorMessage(error, "来源收件箱暂时无法读取。"),
      }));
      return false;
    }
  }, [onUnreadCountChange, requestedItemId, sourceFilter, stateFilter, submittedQuery, unreadOnly]);

  const loadHealth = useCallback(async () => {
    const previous = healthRequestRef.current;
    previous.controller?.abort();
    const controller = new AbortController();
    const sequence = previous.sequence + 1;
    healthRequestRef.current = { sequence, controller };
    setHealthState((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const payload = await api.sourceMonitoringHealth(controller.signal);
      if (
        controller.signal.aborted
        || healthRequestRef.current.sequence !== sequence
        || !openRef.current
      ) return false;
      const health = normalizeSourceMonitoringHealth(payload);
      setHealthState({ status: "ready", health, error: "" });
      return true;
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") return false;
      if (healthRequestRef.current.sequence !== sequence || !openRef.current) return false;
      setHealthState({
        status: "error",
        health: null,
        error: errorMessage(error, "Adapter 健康记录暂时无法读取。"),
      });
      return false;
    }
  }, []);

  const loadOperatorControl = useCallback(async () => {
    const previous = operatorRequestRef.current;
    previous.controller?.abort();
    const controller = new AbortController();
    const sequence = previous.sequence + 1;
    operatorRequestRef.current = { sequence, controller };
    setOperatorActionState(EMPTY_OPERATOR_ACTION_STATE);
    setOperatorControlState((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const payload = await api.sourceMonitoringOperatorControl(controller.signal);
      if (
        controller.signal.aborted
        || operatorRequestRef.current.sequence !== sequence
        || !openRef.current
      ) return false;
      const control = normalizeSourceMonitoringOperatorControl(payload);
      if (!control.valid) {
        throw new Error("Adapter 接入设置未满足固定零执行合同。");
      }
      setOperatorControlState({ status: "ready", control, error: "" });
      return control;
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") return false;
      if (operatorRequestRef.current.sequence !== sequence || !openRef.current) return false;
      setOperatorControlState({
        status: "error",
        control: null,
        error: errorMessage(error, "Adapter 接入设置暂时无法读取。"),
      });
      return null;
    }
  }, []);

  const previewAdapterInitialization = useCallback(async (adapter) => {
    if (!adapter?.canPreview || operatorActionState.status === "loading") return;
    const previous = operatorRequestRef.current;
    previous.controller?.abort();
    const controller = new AbortController();
    const sequence = previous.sequence + 1;
    operatorRequestRef.current = { sequence, controller };
    const initializationPolicy = sourceMonitoringInitializationPolicy(
      operatorControlState.control?.settings,
      adapter,
    );
    const binding = {
      adapterKey: adapter.adapterKey,
      configVersion: adapter.configVersion,
      stateVersion: adapter.stateVersion,
      ...initializationPolicy,
    };
    setOperatorActionState({
      ...EMPTY_OPERATOR_ACTION_STATE,
      status: "loading",
      intent: "enable",
      adapterKey: adapter.adapterKey,
    });
    try {
      const payload = await api.previewSourceMonitoringAdapterInitialization(
        adapter.adapterKey,
        {
          expected_state_version: adapter.stateVersion,
          expected_config_version: adapter.configVersion,
        },
        controller.signal,
      );
      if (
        controller.signal.aborted
        || operatorRequestRef.current.sequence !== sequence
        || !openRef.current
      ) return;
      const preview = normalizeSourceMonitoringOperatorPreview(payload);
      if (
        !preview.valid
        || preview.adapterKey !== binding.adapterKey
        || preview.configVersion !== binding.configVersion
        || preview.stateVersion !== binding.stateVersion
        || preview.mode !== binding.initialMode
        || preview.catchUpMaxItems !== binding.catchUpMaxItems
        || preview.fromTime !== binding.fromTime
      ) {
        throw new Error("首次初始化预览未绑定当前 Adapter 精确状态。");
      }
      setOperatorActionState({
        ...EMPTY_OPERATOR_ACTION_STATE,
        status: "ready",
        intent: "enable",
        adapterKey: adapter.adapterKey,
        preview,
      });
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") return;
      if (operatorRequestRef.current.sequence !== sequence || !openRef.current) return;
      setOperatorActionState({
        ...EMPTY_OPERATOR_ACTION_STATE,
        status: "error",
        intent: "enable",
        adapterKey: adapter.adapterKey,
        error: errorMessage(error, "首次初始化预览失败。"),
      });
    }
  }, [operatorActionState.status, operatorControlState.control]);

  const prepareAdapterEnable = useCallback((adapter) => {
    operatorRequestRef.current.controller?.abort();
    setOperatorActionState({
      ...EMPTY_OPERATOR_ACTION_STATE,
      status: "ready",
      intent: "enable",
      adapterKey: adapter.adapterKey,
    });
  }, []);

  const prepareAdapterDisable = useCallback((adapter) => {
    operatorRequestRef.current.controller?.abort();
    setOperatorActionState({
      ...EMPTY_OPERATOR_ACTION_STATE,
      status: "ready",
      intent: "disable",
      adapterKey: adapter.adapterKey,
    });
  }, []);

  const submitAdapterEnablement = useCallback(async () => {
    const control = operatorControlState.control;
    const adapter = control?.adapters.find(
      (candidate) => candidate.adapterKey === operatorActionState.adapterKey,
    );
    if (!adapter || !operatorActionState.confirmed || operatorActionState.status === "loading") return;
    const enabled = operatorActionState.intent === "enable";
    const preview = operatorActionState.preview;
    const previewSha256 = (
      enabled && adapter.initializationStatus === "required"
        ? preview?.previewSha256 || ""
        : ""
    );
    if (enabled && adapter.initializationStatus === "required" && !preview?.valid) return;

    const previous = operatorRequestRef.current;
    previous.controller?.abort();
    const controller = new AbortController();
    const sequence = previous.sequence + 1;
    operatorRequestRef.current = { sequence, controller };
    setOperatorActionState((current) => ({ ...current, status: "loading", error: "", feedback: "" }));
    try {
      const payload = await api.setSourceMonitoringAdapterEnablement(
        adapter.adapterKey,
        {
          expected_state_version: adapter.stateVersion,
          expected_config_version: adapter.configVersion,
          enabled,
          preview_sha256: previewSha256,
          confirmation: enabled
            ? "ENABLE_SOURCE_MONITORING_ADAPTER"
            : "DISABLE_SOURCE_MONITORING_ADAPTER",
        },
        controller.signal,
      );
      if (
        controller.signal.aborted
        || operatorRequestRef.current.sequence !== sequence
        || !openRef.current
      ) return;
      const result = normalizeSourceMonitoringEnablementResult(payload);
      const initializationAuthorized = enabled
        && adapter.initializationStatus === "required";
      if (
        !result.valid
        || result.adapterKey !== adapter.adapterKey
        || result.configVersion !== adapter.configVersion
        || result.persistedEnabled !== enabled
        || result.initializationAuthorized !== initializationAuthorized
        || result.previewSha256 !== (
          initializationAuthorized ? previewSha256 : ""
        )
      ) {
        throw new Error("Adapter 启停回执未绑定本次精确请求。");
      }
      const [authoritativeControl, healthLoaded] = await Promise.all([
        loadOperatorControl(),
        loadHealth(),
      ]);
      if (!openRef.current) return;
      const authoritativeAdapter = authoritativeControl?.adapters.find(
        (candidate) => candidate.adapterKey === adapter.adapterKey,
      );
      const controlConfirmed = authoritativeAdapter?.configVersion
        === adapter.configVersion
        && authoritativeAdapter?.persistedEnabled === enabled
        && authoritativeAdapter?.stateVersion >= result.stateVersion;
      setOperatorActionState({
        ...EMPTY_OPERATOR_ACTION_STATE,
        feedback: controlConfirmed && healthLoaded
          ? enabled
            ? "启用状态变更已提交并完成权威重读；Runtime 在线状态请以上方健康记录为准。"
            : "后续轮询停用请求已提交并完成权威重读；历史 checkpoint 与收件箱记录未被删除。"
          : "变更请求已返回有效回执，但权威重读未确认目标状态；请重新读取接入设置与健康记录。",
      });
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") return;
      if (operatorRequestRef.current.sequence !== sequence || !openRef.current) return;
      if (error?.status === 409) {
        await Promise.all([loadOperatorControl(), loadHealth()]);
      }
      if (!openRef.current) return;
      setOperatorActionState({
        ...EMPTY_OPERATOR_ACTION_STATE,
        status: "error",
        adapterKey: adapter.adapterKey,
        intent: enabled ? "enable" : "disable",
        error: error?.status === 409
          ? `${errorMessage(error, "Adapter 状态已变化。")} 已权威重读，请重新预览和确认。`
          : errorMessage(error, "Adapter 启停请求未完成。"),
      });
    }
  }, [loadHealth, loadOperatorControl, operatorActionState, operatorControlState.control]);

  useEffect(() => {
    if (open && requestedItemId) setSelectedItemId(requestedItemId);
  }, [open, requestedItemId]);

  useEffect(() => {
    if (!open) {
      listRequestRef.current.controller?.abort();
      healthRequestRef.current.controller?.abort();
      return undefined;
    }
    let cancelled = false;
    void (async () => {
      await loadList();
      if (!cancelled && openRef.current) await loadHealth();
    })();
    return () => {
      cancelled = true;
      listRequestRef.current.controller?.abort();
      healthRequestRef.current.controller?.abort();
    };
  }, [loadHealth, loadList, open]);

  useEffect(() => {
    if (open) return undefined;
    detailRequestRef.current.controller?.abort();
    actionRequestRef.current.controller?.abort();
    operatorRequestRef.current.controller?.abort();
    importRequestRef.current.controller?.abort();
    promptTemplateRequestRef.current.controller?.abort();
    setActionState(EMPTY_ACTION_STATE);
    setImportActionState(EMPTY_IMPORT_ACTION_STATE);
    setImportPreviewState(EMPTY_IMPORT_PREVIEW_STATE);
    setPromptTemplateState(EMPTY_PROMPT_TEMPLATE_STATE);
    setOperatorControlState(EMPTY_OPERATOR_CONTROL_STATE);
    setOperatorActionState(EMPTY_OPERATOR_ACTION_STATE);
    setImportError("");
    setImportContent("");
    setImportOpen(false);
    setPromptTemplateOpen(false);
    return undefined;
  }, [open]);

  useEffect(() => () => {
    detailRequestRef.current.controller?.abort();
    actionRequestRef.current.controller?.abort();
    operatorRequestRef.current.controller?.abort();
    importRequestRef.current.controller?.abort();
    promptTemplateRequestRef.current.controller?.abort();
  }, []);

  useEffect(() => {
    if (importPreviewState.status === "ready") {
      previewHeadingRef.current?.focus();
    }
  }, [importPreviewState.status]);

  useEffect(() => {
    if (operatorActionState.status === "ready" && operatorActionState.preview?.valid) {
      operatorPreviewHeadingRef.current?.focus();
    }
  }, [operatorActionState.preview, operatorActionState.status]);

  const loadDetail = useCallback(async (itemId) => {
    if (!itemId) {
      setDetailState(EMPTY_DETAIL_STATE);
      return false;
    }
    const previous = detailRequestRef.current;
    previous.controller?.abort();
    const controller = new AbortController();
    const sequence = previous.sequence + 1;
    detailRequestRef.current = { sequence, controller };
    setDetailState((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const payload = await api.sourceInboxItem(itemId, controller.signal);
      if (
        controller.signal.aborted
        || detailRequestRef.current.sequence !== sequence
        || !openRef.current
      ) return false;
      const item = normalizeSourceInboxItem(payload.source_item);
      if (item.id !== itemId) {
        throw new Error("来源详情与请求事件 ID 不一致，已拒绝展示。");
      }
      setAcknowledgementChecked(false);
      setDetailState({ status: "ready", item, error: "" });
      setListState((current) => ({
        ...current,
        items: replaceSourceInboxItem(current.items, item),
      }));
      return true;
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") return false;
      if (detailRequestRef.current.sequence !== sequence || !openRef.current) return false;
      setDetailState({
        status: "error",
        item: null,
        error: errorMessage(error, "来源详情暂时无法读取。"),
      });
      return false;
    }
  }, []);

  useEffect(() => {
    setAcknowledgementChecked(false);
    setObjective("");
    setActionState(EMPTY_ACTION_STATE);
    setFeedback("");
    if (open) void loadDetail(selectedItemId);
  }, [loadDetail, open, selectedItemId]);

  const refreshInbox = useCallback(async () => {
    const preferredItemId = selectedItemId;
    await loadList({ preferredItemId });
    await loadHealth();
    if (preferredItemId) await loadDetail(preferredItemId);
  }, [loadDetail, loadHealth, loadList, selectedItemId]);

  useEffect(() => {
    if (
      open
      && refreshToken > consumedRefreshTokenRef.current
    ) {
      consumedRefreshTokenRef.current = refreshToken;
      void refreshInbox();
    }
  }, [open, refreshInbox, refreshToken]);

  const selectItem = useCallback((itemId) => {
    const cleanItemId = String(itemId || "");
    setSelectedItemId(cleanItemId);
    if (cleanItemId && typeof onEventTargetChange === "function") {
      onEventTargetChange(cleanItemId);
    }
  }, [onEventTargetChange]);

  const copyDeepLink = useCallback(async () => {
    if (!detailState.item?.id || typeof onCopyEventLink !== "function") return;
    setFeedback("");
    try {
      await onCopyEventLink(detailState.item.id);
      setFeedback("已复制该事件的本地深链接；链接只包含服务端事件 ID。");
    } catch (error) {
      setFeedback(errorMessage(error, "无法复制事件链接。"));
    }
  }, [detailState.item, onCopyEventLink]);

  const loadPromptTemplate = useCallback(async () => {
    const previous = promptTemplateRequestRef.current;
    previous.controller?.abort();
    const controller = new AbortController();
    const sequence = previous.sequence + 1;
    promptTemplateRequestRef.current = { sequence, controller };
    setPromptTemplateState({ ...EMPTY_PROMPT_TEMPLATE_STATE, status: "loading" });
    try {
      const payload = await api.sourceMonitoringPromptTemplate(controller.signal);
      if (
        controller.signal.aborted
        || promptTemplateRequestRef.current.sequence !== sequence
        || !openRef.current
      ) return false;
      const template = normalizeSourceMonitoringPromptTemplate(payload);
      if (!template.valid) {
        throw new Error("GPT 监控提示词模板未满足固定安全合同。");
      }
      setPromptTemplateState({ status: "ready", template, error: "", feedback: "" });
      return true;
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") return false;
      if (promptTemplateRequestRef.current.sequence !== sequence || !openRef.current) return false;
      setPromptTemplateState({
        status: "error",
        template: null,
        error: errorMessage(error, "GPT 监控提示词模板暂时无法读取。"),
        feedback: "",
      });
      return false;
    }
  }, []);

  const togglePromptTemplate = useCallback(() => {
    const nextOpen = !promptTemplateOpen;
    setPromptTemplateOpen(nextOpen);
    if (nextOpen && promptTemplateState.status === "idle") {
      void loadPromptTemplate();
    }
  }, [loadPromptTemplate, promptTemplateOpen, promptTemplateState.status]);

  const copyPromptTemplate = useCallback(async () => {
    const prompt = promptTemplateState.template?.prompt || "";
    if (!prompt) return;
    try {
      if (typeof globalThis.navigator?.clipboard?.writeText !== "function") {
        throw new Error("当前浏览器无法写入剪贴板。");
      }
      await globalThis.navigator.clipboard.writeText(prompt);
      setPromptTemplateState((current) => ({
        ...current,
        error: "",
        feedback: "已复制 GPT 监控提示词；本页没有打开、登录或控制 ChatGPT。",
      }));
    } catch (error) {
      promptTextareaRef.current?.focus();
      promptTextareaRef.current?.select();
      setPromptTemplateState((current) => ({
        ...current,
        error: errorMessage(error, "复制失败，已选中模板供手动复制。"),
        feedback: "",
      }));
    }
  }, [promptTemplateState.template]);

  const clearImportDraft = useCallback(() => {
    importRequestRef.current.controller?.abort();
    setImportContent("");
    setImportError("");
    setImportPreviewState(EMPTY_IMPORT_PREVIEW_STATE);
    setImportActionState(EMPTY_IMPORT_ACTION_STATE);
  }, []);

  const changeImportContent = useCallback((value) => {
    const nextValue = String(value || "");
    importRequestRef.current.controller?.abort();
    setImportContent(nextValue);
    setImportError("");
    setImportActionState(EMPTY_IMPORT_ACTION_STATE);
    setImportPreviewState((current) => (
      current.status === "idle" && !current.contentSnapshot
        ? current
        : {
          ...EMPTY_IMPORT_PREVIEW_STATE,
          status: "stale",
          error: "内容已更改，请重新预览。",
        }
    ));
  }, []);

  const previewImportPacket = useCallback(async () => {
    const rawSnapshot = importContent;
    if (!rawSnapshot.trim() || importActionState.status === "loading") return;
    const payloadBytes = sourceImportUtf8Bytes(rawSnapshot);
    if (payloadBytes > SOURCE_IMPORT_MAX_BYTES) {
      setImportPreviewState({
        ...EMPTY_IMPORT_PREVIEW_STATE,
        status: "error",
        error: `导入内容为 ${payloadBytes} UTF-8 bytes，超过 ${SOURCE_IMPORT_MAX_BYTES} 上限。`,
      });
      return;
    }
    const previous = importRequestRef.current;
    previous.controller?.abort();
    const controller = new AbortController();
    const sequence = previous.sequence + 1;
    importRequestRef.current = { sequence, controller };
    setImportError("");
    setImportActionState(EMPTY_IMPORT_ACTION_STATE);
    setImportPreviewState({
      ...EMPTY_IMPORT_PREVIEW_STATE,
      status: "loading",
      contentSnapshot: rawSnapshot,
    });
    try {
      const payload = await api.previewSourceInboxImport(rawSnapshot, controller.signal);
      if (
        controller.signal.aborted
        || importRequestRef.current.sequence !== sequence
        || !openRef.current
      ) return;
      const preview = normalizeSourceImportPreview(payload);
      if (!preview.valid) {
        throw new Error("预览响应未满足固定安全合同。");
      }
      setImportPreviewState({
        status: "ready",
        preview,
        contentSnapshot: rawSnapshot,
        issues: [],
        error: "",
      });
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") return;
      if (importRequestRef.current.sequence !== sequence || !openRef.current) return;
      setImportPreviewState({
        ...EMPTY_IMPORT_PREVIEW_STATE,
        status: "error",
        contentSnapshot: rawSnapshot,
        issues: normalizedImportIssues(error),
        error: errorMessage(error, "ChatGPT 来源包 JSON 预览失败。"),
      });
    }
  }, [importActionState.status, importContent]);

  const adoptItem = useCallback((rawItem, expectedItemId = "") => {
    const item = normalizeSourceInboxItem(rawItem);
    if (!item.valid || !item.id || (expectedItemId && item.id !== expectedItemId)) return null;
    setDetailState({ status: "ready", item, error: "" });
    setListState((current) => ({
      ...current,
      items: replaceSourceInboxItem(current.items, item),
    }));
    setSelectedItemId(item.id);
    return item;
  }, []);

  const runItemAction = useCallback(async (type, request, successMessage) => {
    if (!detailState.item || actionState.status === "loading") return false;
    const targetItemId = detailState.item.id;
    const previous = actionRequestRef.current;
    previous.controller?.abort();
    const controller = new AbortController();
    const sequence = previous.sequence + 1;
    actionRequestRef.current = { sequence, controller };
    setActionState({ status: "loading", type, error: "" });
    setFeedback("");
    try {
      const payload = await request(controller.signal);
      if (
        controller.signal.aborted
        || actionRequestRef.current.sequence !== sequence
        || !openRef.current
      ) return false;
      const item = adoptItem(payload.source_item || payload.item, targetItemId);
      if (!item) throw new Error("服务端未返回更新后的来源条目。");
      setAcknowledgementChecked(false);
      setActionState(EMPTY_ACTION_STATE);
      setFeedback(successMessage);
      await loadList({ preferredItemId: item.id });
      return true;
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") return false;
      if (actionRequestRef.current.sequence !== sequence || !openRef.current) return false;
      if (error?.status === 409) await loadDetail(targetItemId);
      setActionState({
        status: "error",
        type,
        error: error?.status === 409
          ? `${errorMessage(error, "来源状态已变化。")} 已重新读取该条目，请核对后重试。`
          : errorMessage(error, "来源操作未完成，请刷新后重试。"),
      });
      return false;
    }
  }, [actionState.status, adoptItem, detailState.item, loadDetail, loadList]);

  const acknowledgeItem = () => runItemAction(
    "acknowledge",
    (signal) => api.acknowledgeSourceInboxItem(
      detailState.item.id,
      detailState.item.stateVersion,
      signal,
    ),
    "已记录为已阅；这不代表事实确认。",
  );

  const attachItem = async () => {
    const attached = await runItemAction(
      "attach",
      (signal) => api.attachSourceInboxItem(
        detailState.item.id,
        selectedRoomId,
        detailState.item.stateVersion,
        signal,
      ),
      "来源已作为未核验材料附加到所选房间；未启动任何 Provider。",
    );
    if (attached && typeof onRoomAttached === "function") {
      try {
        await onRoomAttached(selectedRoomId);
      } catch {
        setFeedback("来源已附加成功；房间画布未能自动刷新，可稍后手动刷新房间。");
      }
    }
  };

  const createDraft = () => runItemAction(
    "draft",
    (signal) => api.createSourceInboxRoundDraft(
      detailState.item.id,
      selectedRoomId,
      detailState.item.stateVersion,
      objective.trim(),
      signal,
    ),
    "仅生成了 round draft；Provider、正式 round 与市场调用均未启动。",
  );

  const importPacket = async () => {
    if (
      importActionState.status === "loading"
      || importPreviewState.status !== "ready"
      || !importPreviewState.preview?.valid
      || !importPreviewState.contentSnapshot
      || importContent !== importPreviewState.contentSnapshot
    ) return;
    const rawSnapshot = importPreviewState.contentSnapshot;
    setImportError("");
    setFeedback("");
    const previous = importRequestRef.current;
    previous.controller?.abort();
    const controller = new AbortController();
    const sequence = previous.sequence + 1;
    importRequestRef.current = { sequence, controller };
    setImportActionState({ status: "loading", error: "" });
    try {
      const payload = await api.importSourceInbox(rawSnapshot, controller.signal);
      if (
        controller.signal.aborted
        || importRequestRef.current.sequence !== sequence
        || !openRef.current
      ) return;
      const importResult = normalizeSourceImportResult(payload);
      if (!importResult.valid) {
        throw new Error("导入响应未满足固定回执、身份和零执行合同。");
      }
      const firstItem = importResult.items[0] || null;
      if (firstItem) {
        setListState((current) => ({
          ...current,
          items: importResult.items.reduce(
            (items, item) => replaceSourceInboxItem(items, item),
            current.items,
          ),
        }));
        setDetailState({ status: "ready", item: firstItem, error: "" });
        setSelectedItemId(firstItem.id);
      }
      setImportActionState(EMPTY_IMPORT_ACTION_STATE);
      setImportPreviewState(EMPTY_IMPORT_PREVIEW_STATE);
      setImportContent("");
      setImportOpen(false);
      setStateFilter("");
      setSourceFilter("");
      setUnreadOnly(false);
      setQueryInput("");
      setSubmittedQuery("");
      setFeedback(importResult.idempotentReplay
        ? "该 external run 已导入过；已返回同一份幂等结果，未重复创建条目。"
        : importResult.items.length
          ? `已导入 ${importResult.items.length} 条 external_unverified 来源；仍需逐条人工审阅。`
          : "导入已接收，但没有新增来源条目。");
      void loadList({ preferredItemId: firstItem?.id || "" });
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") return;
      if (importRequestRef.current.sequence !== sequence || !openRef.current) return;
      const issues = normalizedImportIssues(error);
      setImportActionState({
        status: "error",
        issues,
        error: errorMessage(error, "ChatGPT 来源包 JSON 导入失败。"),
      });
      setImportError(issues.length
        ? `${errorMessage(error, "导入失败。")}（${issues.length} 个问题）`
        : errorMessage(error, "导入失败。"));
    }
  };

  const visibleCount = listState.items.length;
  const selectedItem = detailState.item;
  const importBusy = importPreviewState.status === "loading" || importActionState.status === "loading";
  const busy = actionState.status === "loading" || importBusy;
  const canConfirmImport = (
    importPreviewState.status === "ready"
    && importPreviewState.preview?.valid === true
    && importPreviewState.contentSnapshot.length > 0
    && importContent === importPreviewState.contentSnapshot
    && !importBusy
  );

  return (
    <>
      <button
        className={open ? "source-inbox-scrim open" : "source-inbox-scrim"}
        type="button"
        tabIndex={-1}
        aria-label="关闭来源收件箱"
        onClick={requestClose}
      />
      <section
        ref={panelRef}
        className={open ? "source-inbox-panel open" : "source-inbox-panel"}
        role="dialog"
        aria-modal="true"
        aria-hidden={!open}
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        aria-busy={listState.status === "loading" || detailState.status === "loading" || busy}
        tabIndex={-1}
      >
        <header className="source-inbox-heading">
          <span>
            <Inbox aria-hidden="true" size={20} />
            <span>
              <strong id={titleId}>来源收件箱</strong>
              <small>人工审阅外部来源 · {Number(listState.unreadCount || 0)} 条未读</small>
            </span>
          </span>
          <button
            ref={closeButtonRef}
            className="icon-button"
            type="button"
            aria-label="关闭来源收件箱"
            onClick={requestClose}
          ><X aria-hidden="true" size={18} /></button>
        </header>

        <div className="source-inbox-boundary" id={descriptionId}>
          <ShieldAlert aria-hidden="true" size={17} />
          <span>
            <strong>外部信息默认不可信</strong>
            <small>打开本页及收件箱审阅、附加、草稿操作不触发 Provider、正式 round、市场读取或执行；readonly_market 首次预览仅展示静态政策，不读取行情，首次 Runtime 运行时才执行受控只读轮询。</small>
          </span>
        </div>

        <div className="source-inbox-toolbar">
          <form onSubmit={(event) => {
            event.preventDefault();
            setSubmittedQuery(queryInput.trim());
          }}>
            <Search aria-hidden="true" size={16} />
            <input
              aria-label="搜索来源"
              value={queryInput}
              maxLength={200}
              placeholder="搜索标题或摘要"
              onChange={(event) => setQueryInput(event.target.value)}
            />
            <button className="secondary compact" type="submit">搜索</button>
          </form>
          <button
            className="secondary compact"
            type="button"
            onClick={() => setImportOpen((value) => !value)}
            aria-expanded={importOpen}
          ><Upload aria-hidden="true" size={14} />导入 JSON</button>
          <button
            className="icon-button"
            type="button"
            aria-label="刷新来源收件箱"
            title="刷新来源收件箱"
            disabled={listState.status === "loading"}
            onClick={() => void refreshInbox()}
          ><RefreshCw aria-hidden="true" className={listState.status === "loading" ? "spin" : undefined} size={16} /></button>
        </div>

        {importOpen ? (
          <section
            className="source-inbox-import"
            aria-label="manual_chatgpt JSON 导入"
            aria-busy={importBusy}
          >
            <header><FileJson2 aria-hidden="true" size={17} /><span><strong>ChatGPT 来源包 JSON（manual import）</strong><small>与 Manual ChatGPT 协作结论流分离；仅预览和导入 source_import_packet_v1，也支持单个 fenced JSON。</small></span></header>
            <div className="source-inbox-prompt-template">
              <button
                className="secondary compact"
                type="button"
                aria-expanded={promptTemplateOpen}
                aria-controls={promptTemplateId}
                onClick={togglePromptTemplate}
              >
                <ClipboardCopy aria-hidden="true" size={14} />
                GPT 监控提示词模板
              </button>
              {promptTemplateOpen ? (
                <section id={promptTemplateId} aria-label="GPT 监控提示词模板">
                  <p>
                    请先在普通 ChatGPT 对话中测试并手动替换占位符。本页不打开、登录或控制 ChatGPT，也不创建 Scheduled Task。
                  </p>
                  {promptTemplateState.status === "loading" ? (
                    <p className="source-inbox-loading" role="status"><LoaderCircle aria-hidden="true" className="spin" size={14} />正在读取版本化模板…</p>
                  ) : null}
                  {promptTemplateState.status === "error" ? (
                    <p className="source-inbox-notice error" role="alert"><AlertTriangle aria-hidden="true" size={14} />{promptTemplateState.error}</p>
                  ) : null}
                  {promptTemplateState.status === "ready" && promptTemplateState.template?.valid ? (
                    <>
                      <label htmlFor={`${promptTemplateId}-content`}>GPT 监控提示词（只读）</label>
                      <textarea
                        ref={promptTextareaRef}
                        id={`${promptTemplateId}-content`}
                        aria-label="GPT 监控提示词"
                        value={promptTemplateState.template.prompt}
                        readOnly
                        spellCheck="false"
                      />
                      <footer>
                        <code>{shortHash(promptTemplateState.template.templateSha256)}</code>
                        <button className="secondary compact" type="button" onClick={() => void copyPromptTemplate()}>
                          <ClipboardCopy aria-hidden="true" size={14} />复制 GPT 监控提示词
                        </button>
                      </footer>
                      {promptTemplateState.feedback ? <p className="source-inbox-notice" role="status" aria-live="polite">{promptTemplateState.feedback}</p> : null}
                      {promptTemplateState.error ? <p className="source-inbox-notice error" role="alert">{promptTemplateState.error}</p> : null}
                    </>
                  ) : null}
                </section>
              ) : null}
            </div>
            <label htmlFor={importTextareaId}>ChatGPT 来源包 JSON</label>
            <textarea
              id={importTextareaId}
              aria-label="ChatGPT 来源包 JSON"
              aria-describedby={importBoundaryId}
              value={importContent}
              maxLength={SOURCE_IMPORT_MAX_BYTES}
              placeholder={'粘贴 {"version":"source_import_packet_v1", ...}'}
              spellCheck="false"
              disabled={importActionState.status === "loading"}
              onChange={(event) => changeImportContent(event.target.value)}
            />
            <p
              id={importBoundaryId}
              className={importContentBytes > SOURCE_IMPORT_MAX_BYTES ? "source-inbox-import-meter invalid" : "source-inbox-import-meter"}
            >
              {importContentBytes} / {SOURCE_IMPORT_MAX_BYTES} UTF-8 bytes · 预览不读写数据库，不调用 Provider/市场，不创建正式 round。
            </p>
            {importError ? <p className="source-inbox-notice error" role="alert">{importError}</p> : null}
            <SourceImportIssues issues={importActionState.issues || []} />
            <SourceImportPreview headingRef={previewHeadingRef} state={importPreviewState} />
            <footer>
              <button className="secondary compact" type="button" disabled={importBusy} onClick={() => setImportOpen(false)}>暂时收起</button>
              <button className="secondary compact" type="button" disabled={!importContent && importPreviewState.status === "idle"} onClick={clearImportDraft}>清空</button>
              <button
                className="secondary compact"
                type="button"
                disabled={!importContent.trim() || importBusy || importContentBytes > SOURCE_IMPORT_MAX_BYTES}
                onClick={() => void previewImportPacket()}
              >
                {importPreviewState.status === "loading"
                  ? <LoaderCircle aria-hidden="true" className="spin" size={14} />
                  : <FileJson2 aria-hidden="true" size={14} />}
                预览导入内容
              </button>
              <button className="primary compact" type="button" disabled={!canConfirmImport} onClick={() => void importPacket()}>
                {importActionState.status === "loading"
                  ? <LoaderCircle aria-hidden="true" className="spin" size={14} />
                  : <Upload aria-hidden="true" size={14} />}
                确认仅导入收件箱
              </button>
            </footer>
          </section>
        ) : null}

        <SourceMonitoringHealth
          healthState={healthState}
          operatorActionState={operatorActionState}
          operatorControlState={operatorControlState}
          operatorPreviewHeadingRef={operatorPreviewHeadingRef}
          notificationState={notificationState}
          onCancelOperatorAction={() => setOperatorActionState(EMPTY_OPERATOR_ACTION_STATE)}
          onLoadOperatorControl={() => void loadOperatorControl()}
          onNotificationPreferenceChange={onNotificationPreferenceChange}
          onOperatorConfirmationChange={(confirmed) => setOperatorActionState(
            (current) => ({ ...current, confirmed }),
          )}
          onPrepareAdapterDisable={prepareAdapterDisable}
          onPrepareAdapterEnable={prepareAdapterEnable}
          onPreviewAdapterInitialization={(adapter) => void previewAdapterInitialization(adapter)}
          onSubmitAdapterEnablement={() => void submitAdapterEnablement()}
        />

        <div className="source-inbox-filter-groups">
          <fieldset className="source-inbox-filters">
            <legend>工作状态（全局计数）</legend>
            <div>
              {SOURCE_INBOX_FILTERS.map((filter) => (
                <button
                  key={filter.id || "all"}
                  type="button"
                  aria-pressed={stateFilter === filter.id}
                  onClick={() => setStateFilter(filter.id)}
                >
                  {filter.label}
                  {filter.id && Number.isFinite(listState.counts[filter.id])
                    ? <span>{listState.counts[filter.id]}</span>
                    : null}
                </button>
              ))}
            </div>
          </fieldset>
          <fieldset className="source-inbox-provenance-filters">
            <legend>来源与阅读状态（全局计数）</legend>
            <label>
              <span>按接入来源筛选</span>
              <select
                aria-label="按接入来源筛选"
                value={sourceFilter}
                onChange={(event) => setSourceFilter(event.target.value)}
              >
                <option value="">全部来源</option>
                {listState.sourceFacets?.filter((facet) => facet.valid).map((facet) => (
                  <option key={facet.id} value={facet.id}>
                    {facet.sourceTierCode} · {facet.sourceKey}（{facet.count}）
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              aria-pressed={unreadOnly}
              onClick={() => setUnreadOnly((value) => !value)}
            >
              仅看未读 <span>{Number(listState.unreadCount || 0)}</span>
            </button>
          </fieldset>
        </div>

        {feedback ? <p className="source-inbox-notice" role="status" aria-live="polite"><Check aria-hidden="true" size={15} />{feedback}</p> : null}
        {actionState.status === "error" ? <p className="source-inbox-notice error" role="alert"><AlertTriangle aria-hidden="true" size={15} />{actionState.error}</p> : null}
        {listState.status === "error" ? (
          <p className="source-inbox-notice error" role="alert">
            <AlertTriangle aria-hidden="true" size={15} />
            <span>{listState.error}</span>
            <button className="secondary compact" type="button" onClick={() => void loadList()}>重试</button>
          </p>
        ) : null}

        <div className="source-inbox-workspace">
          <aside className="source-inbox-list" aria-label={`来源列表，共 ${visibleCount} 条`}>
            <header>
              <strong>来源列表</strong>
              <small>{visibleCount} 条已显示 · {Number(listState.matchedCount || visibleCount)} 条匹配</small>
            </header>
            {listState.status === "loading" && !listState.items.length ? (
              <p className="source-inbox-loading" role="status"><LoaderCircle aria-hidden="true" className="spin" size={16} />正在读取来源…</p>
            ) : null}
            {listState.status === "ready" && !listState.items.length ? (
              <div className="source-inbox-empty">
                <img src={dutyCatArt} alt="值班喵守着空的来源收件箱" />
                <strong>暂时没有匹配来源</strong>
                <small>可调整筛选，或手动导入 ChatGPT 来源包 JSON。</small>
              </div>
            ) : null}
            <ul className="source-inbox-list-items">
              {listState.items.map((item) => (
                <li key={item.id}>
                  <SourceInboxListItem
                    item={item}
                    active={selectedItemId === item.id}
                    disabled={busy}
                    onSelect={() => selectItem(item.id)}
                  />
                </li>
              ))}
            </ul>
          </aside>

          <div className="source-inbox-detail-wrap">
            {detailState.status === "loading" ? (
              <p className="source-inbox-loading" role="status"><LoaderCircle aria-hidden="true" className="spin" size={16} />正在读取来源详情…</p>
            ) : null}
            {detailState.status === "error" ? (
              <p className="source-inbox-notice error" role="alert">
                <AlertTriangle aria-hidden="true" size={15} />
                <span>{detailState.error}</span>
                <button className="secondary compact" type="button" onClick={() => void loadDetail(selectedItemId)}>重试</button>
              </p>
            ) : null}
            {detailState.status === "ready" && selectedItem ? (
              <SourceInboxDetail
                item={selectedItem}
                rooms={roomOptions}
                roomId={selectedRoomId}
                acknowledgementChecked={acknowledgementChecked}
                objective={objective}
                actionState={actionState}
                onAcknowledgementChange={setAcknowledgementChecked}
                onAcknowledge={() => void acknowledgeItem()}
                onRoomChange={setSelectedRoomId}
                onAttach={() => void attachItem()}
                onCopyDeepLink={() => void copyDeepLink()}
                onObjectiveChange={setObjective}
                onCreateDraft={() => void createDraft()}
              />
            ) : null}
            {detailState.status === "idle" && listState.status === "ready" && listState.items.length ? (
              <p className="source-inbox-muted">选择一条来源查看详情。</p>
            ) : null}
          </div>
        </div>
      </section>
    </>
  );
}
