import { AlertTriangle, CheckCircle2, ClipboardCheck, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import {
  normalizeProjectReadinessResponse,
  projectReadinessLoadPlan,
} from "../projectReadiness";

function shortHash(value) {
  const hash = String(value || "").trim();
  return hash.length === 64 ? `${hash.slice(0, 10)}…${hash.slice(-8)}` : "未封印";
}

function initialState() {
  return { status: "idle", projection: null, error: "" };
}

function GapList({ title, rows, emptyText }) {
  return (
    <section className="project-readiness-gap-group">
      <header><strong>{title}</strong><em>{rows.length} 项</em></header>
      {rows.length ? (
        <ul>
          {rows.map((row) => (
            <li key={`${row.code}:${row.itemKey}`}>
              <span><strong>{row.message}</strong><small>{row.itemKey || "产物整体"}</small></span>
              <code>{row.code}</code>
            </li>
          ))}
        </ul>
      ) : <p>{emptyText}</p>}
    </section>
  );
}

export function ProjectReadinessPanel({
  room,
  artifact,
  slot,
  contribution,
  showLegacyFallback = false,
}) {
  const plan = useMemo(() => projectReadinessLoadPlan({
    room,
    artifact,
    slot,
    contribution,
    showLegacyFallback,
  }), [artifact, contribution, room, showLegacyFallback, slot]);
  const [state, setState] = useState(initialState);

  useEffect(() => {
    if (!plan.shouldLoad || !plan.expected) {
      setState(initialState);
      return undefined;
    }
    let cancelled = false;
    const controller = new AbortController();
    setState({ status: "loading", projection: null, error: "" });
    api.projectReadiness(
      plan.expected.roomId,
      plan.expected.artifactId,
      plan.expected.artifactVersion,
      controller.signal,
    ).then((payload) => {
      if (cancelled) return;
      const projection = normalizeProjectReadinessResponse(payload, plan.expected);
      setState({
        status: projection.valid ? "ready" : "integrity_failed",
        projection,
        error: "",
      });
    }).catch((error) => {
      if (cancelled || error?.name === "AbortError") return;
      setState({
        status: "error",
        projection: null,
        error: error?.message || "项目就绪投影暂时无法读取。",
      });
    });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [plan]);

  if (!plan.visible) return null;
  const projection = state.projection;
  const totalGapCount = projection?.valid
    ? projection.structuralGaps.length + projection.blockers.length + projection.evidenceGaps.length
    : 0;

  return (
    <section
      className={`project-readiness-panel ${plan.shouldLoad ? "available" : "read-only"}`}
      aria-label="项目就绪度只读复核"
    >
      <header className="project-readiness-heading">
        <span>
          <ClipboardCheck size={17} />
          <strong>项目就绪度只读复核</strong>
          <small>只检查精确产物版本与证据图的结构缺口，不修改产物。</small>
        </span>
        <em>v{artifact?.version || "?"} · 只读</em>
      </header>

      {!plan.shouldLoad ? (
        <p className="project-readiness-state warning" role="note">
          <AlertTriangle size={15} />
          <span><strong>当前不生成项目就绪指标</strong><small>{plan.reason}</small></span>
        </p>
      ) : null}
      {plan.shouldLoad && state.status === "loading" ? (
        <p className="project-readiness-state" aria-live="polite">
          正在读取绑定 v{plan.expected.artifactVersion} 的确定性结构投影……
        </p>
      ) : null}
      {plan.shouldLoad && state.status === "error" ? (
        <p className="project-readiness-state error" role="alert">
          <AlertTriangle size={15} />
          <span><strong>投影读取失败，未展示任何指标</strong><small>{state.error}</small></span>
        </p>
      ) : null}
      {plan.shouldLoad && state.status === "integrity_failed" ? (
        <p className="project-readiness-state error" role="alert">
          <AlertTriangle size={15} />
          <span>
            <strong>投影绑定或安全字段校验失败</strong>
            <small>{projection?.issues?.[0] || "完整性无法验证；全部指标已隐藏。"}</small>
          </span>
        </p>
      ) : null}

      {plan.shouldLoad && state.status === "ready" && projection?.valid ? (
        <>
          <div className={`project-readiness-summary ${totalGapCount ? "has-gaps" : "complete"}`}>
            {totalGapCount ? <AlertTriangle size={17} /> : <CheckCircle2 size={17} />}
            <span>
              <strong>{totalGapCount ? `记录到 ${totalGapCount} 项结构性待处理内容` : "未发现合同定义的结构缺口"}</strong>
              <small>这不是批准、推荐、成功率预测或用户最终决定。</small>
            </span>
          </div>
          <div className="project-readiness-metrics" aria-label="项目结构缺口数量">
            <span><small>结构缺口</small><strong>{projection.structuralGaps.length}</strong></span>
            <span><small>阻断条件</small><strong>{projection.blockers.length}</strong></span>
            <span><small>证据缺口</small><strong>{projection.evidenceGaps.length}</strong></span>
          </div>
          <div className="project-readiness-gaps">
            <GapList title="结构缺口" rows={projection.structuralGaps} emptyText="未记录结构缺口。" />
            <GapList title="阻断条件" rows={projection.blockers} emptyText="未记录阻断条件。" />
            <GapList title="证据缺口" rows={projection.evidenceGaps} emptyText="未记录证据缺口。" />
          </div>
          <details className="project-readiness-binding">
            <summary><ShieldCheck size={14} />查看精确只读绑定</summary>
            <dl>
              <div><dt>产物快照</dt><dd><code title={projection.hashes.artifactSnapshotSha256}>{shortHash(projection.hashes.artifactSnapshotSha256)}</code></dd></div>
              <div><dt>证据图</dt><dd><code title={projection.hashes.evidenceGraphSha256}>{shortHash(projection.hashes.evidenceGraphSha256)}</code></dd></div>
              <div><dt>插件快照</dt><dd><code title={projection.hashes.pluginRegistrySnapshotSha256}>{shortHash(projection.hashes.pluginRegistrySnapshotSha256)}</code></dd></div>
              <div><dt>Adapter</dt><dd>{projection.resolution.adapter.adapter_id} · {projection.resolution.adapter.adapter_version}</dd></div>
              <div><dt>Port</dt><dd>{projection.resolution.port.port_id} · {projection.resolution.port.port_version}</dd></div>
            </dl>
          </details>
        </>
      ) : null}

      <p className="project-readiness-boundary">
        Provider 调用 0 · 市场读取 0 · 业务写入 0 · 不排名 · 不宣称赢家 · 不产生批准；最终决定始终由用户完成。
      </p>
    </section>
  );
}
