import { AlertTriangle, Boxes, ChevronDown, ShieldCheck } from "lucide-react";
import { Children, memo, useEffect, useId, useMemo, useState } from "react";
import {
  capabilityRegistryView,
  shortPluginHash,
} from "../capabilityContributions";
import {
  capabilityRegistryPackPresentation,
  capabilityRegistrySelectionState,
  capabilityRegistrySnapshotPresentation,
} from "../capabilityRegistryUi";
import {
  pluginLifecycleCatalogView,
  pluginLifecycleTarget,
} from "../pluginLifecycle";
import "../styles/capability-registry.css";
import "../styles/capability-registry-refinement.css";

function exactRegistryKey(...parts) {
  return JSON.stringify(parts.map((part) => String(part ?? "")));
}

const ExactBindingGroup = memo(function ExactBindingGroup({ title, eyebrow, emptyLabel, children }) {
  const headingId = useId();
  const hasBindings = Children.count(children) > 0;
  return <section className="plugin-registry-binding-group" aria-labelledby={headingId}>
    <header><span><small>{eyebrow}</small><strong id={headingId}>{title}</strong></span></header>
    <div role={hasBindings ? "list" : undefined}>{hasBindings ? children : <p className="plugin-registry-empty-binding">{emptyLabel}</p>}</div>
  </section>;
});


const RegistryPackRow = memo(function RegistryPackRow({ pack, index }) {
  return <article className={pack.state} data-runtime-state={pack.state} role="listitem">
    <span className="plugin-registry-row-index" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
    <div className="plugin-registry-pack-copy">
      <strong>{pack.name}</strong>
      <small>{pack.id}@{pack.version}</small>
      <small>manifest {shortPluginHash(pack.manifestHash)}</small>
      <p>{pack.runtimeReason}</p>
    </div>
    <em>{pack.statusLabel}</em>
  </article>;
});


export const CapabilityRegistrySnapshot = memo(function CapabilityRegistrySnapshot({
  room,
  capabilityPacks,
  pluginLifecycle,
  pendingPackIds,
}) {
  const titleId = useId();
  const auditTitleId = useId();
  const auditDescriptionId = useId();
  const detailRegionId = useId();
  const [detailsExpanded, setDetailsExpanded] = useState(false);
  const view = useMemo(
    () => capabilityRegistryView(room, capabilityPacks),
    [room, capabilityPacks],
  );
  const lifecycleView = useMemo(
    () => pluginLifecycleCatalogView(pluginLifecycle),
    [pluginLifecycle],
  );
  const selection = useMemo(
    () => capabilityRegistrySelectionState(
      room?.capability_pack_ids,
      pendingPackIds ?? room?.capability_pack_ids,
    ),
    [pendingPackIds, room?.capability_pack_ids],
  );
  const packRows = useMemo(() => view.packs.map((pack) => {
    const lifecycle = lifecycleView.integrityOk ? pluginLifecycleTarget(lifecycleView, {
      kind: "capability_pack",
      id: pack.id,
      version: pack.version,
      sha256: pack.manifestHash,
    }) : null;
    return capabilityRegistryPackPresentation(pack, lifecycle, {
      lifecycleIntegrityOk: lifecycleView.integrityOk,
    });
  }), [lifecycleView, view.packs]);
  const presentation = useMemo(() => capabilityRegistrySnapshotPresentation({
    view,
    lifecycleIntegrityOk: lifecycleView.integrityOk,
    packRows,
  }), [lifecycleView.integrityOk, packRows, view]);
  const exactBindingCount = presentation.stats.packs
    + presentation.stats.adapters
    + presentation.stats.contributions;

  useEffect(() => {
    setDetailsExpanded(false);
  }, [view.hash]);

  if (!view.integrityOk) {
    return <section className="plugin-registry-snapshot integrity-failed" aria-label="房间插件合同异常" role="alert">
      <header><span><AlertTriangle aria-hidden="true" size={16} /><strong>插件合同无法验证</strong></span><small>{shortPluginHash(view.hash)}</small></header>
      <p>能力包、领域适配器和界面贡献已失败关闭；不会加载未知组件或静默换算版本。</p>
      <ul>{view.errors?.map((error, index) => <li key={exactRegistryKey("integrity_error", error, index)}>{error}</li>)}</ul>
    </section>;
  }

  return <section
    aria-labelledby={titleId}
    className="plugin-registry-snapshot"
    data-trust-state={presentation.trustState}
  >
    <header className="plugin-registry-header">
      <div className="plugin-registry-mark" aria-hidden="true"><Boxes size={18} /><span>FROZEN<br />REGISTRY</span></div>
      <div className="plugin-registry-title">
        <small>ROOM CONTRACT / EXACT BINDINGS</small>
        <h4 id={titleId}>当前房间插件合同</h4>
        <p>这是当前轮次使用的冻结合同，不随尚未保存的选择或目录状态静默变化。</p>
      </div>
      <span className="plugin-registry-trust"><i aria-hidden="true" />{presentation.trustLabel}</span>
    </header>

    <div className="plugin-registry-seal">
      <span><small>合同版本</small><strong>{view.version}</strong></span>
      <span><small>快照封印</small><strong>{shortPluginHash(view.hash)}</strong></span>
    </div>

    <dl className="plugin-registry-stats" aria-label="冻结插件合同摘要">
      <div><dt>能力包</dt><dd>{presentation.stats.packs}</dd></div>
      <div><dt>领域适配</dt><dd>{presentation.stats.adapters}</dd></div>
      <div><dt>宿主贡献</dt><dd>{presentation.stats.contributions}</dd></div>
      <div><dt>新绑定可用</dt><dd>{presentation.stats.currentReady ?? "—"}</dd></div>
    </dl>

    {!selection.integrityOk ? <p className="plugin-registry-selection-warning" role="alert"><AlertTriangle aria-hidden="true" size={13} />待保存的能力包选择无法核验：{selection.issue}。冻结合同不受影响，保存前应修正选择。</p> : null}
    {selection.integrityOk && selection.changed ? <p className="plugin-registry-next-note" role="note">能力包选择已变化；保存时由服务端解析新版本，当前轮次继续使用这里的冻结合同。</p> : null}
    {presentation.trustState === "sealed-frozen-only" ? <p className="plugin-registry-lifecycle-warning" role="alert"><AlertTriangle aria-hidden="true" size={13} />当前生命周期状态无法完整核验；新绑定和插件动作保持关闭，但冻结合同本身仍可独立审计。</p> : null}

    <section className="plugin-registry-audit" aria-labelledby={auditTitleId}>
      <div className="plugin-registry-audit-control">
        <span>
          <small>FROZEN DETAIL / READ ONLY</small>
          <strong id={auditTitleId}>{exactBindingCount} 项精确冻结绑定</strong>
          <p id={auditDescriptionId}>先核对合同版本与封印；需要逐项审计时再展开能力包、适配器和宿主贡献。</p>
        </span>
        <button
          aria-controls={detailRegionId}
          aria-describedby={auditDescriptionId}
          aria-expanded={detailsExpanded}
          onClick={() => setDetailsExpanded((current) => !current)}
          type="button"
        >
          <span>{detailsExpanded ? "收起冻结绑定" : "查看冻结绑定"}</span>
          <ChevronDown aria-hidden="true" size={15} />
        </button>
      </div>

      <div
        aria-labelledby={auditTitleId}
        className="plugin-registry-exact-bindings"
        hidden={!detailsExpanded}
        id={detailRegionId}
        role="region"
      >
        <section className="plugin-registry-pack-section" aria-label="冻结能力包">
          <header><span>01</span><div><small>CAPABILITY PACKS</small><strong>精确能力包绑定</strong></div></header>
          <div className="plugin-registry-pack-list" role="list">
            {packRows.map((pack, index) => (
              <RegistryPackRow
                key={exactRegistryKey("capability_pack", pack.id, pack.version, pack.manifestHash)}
                pack={pack}
                index={index}
              />
            ))}
          </div>
        </section>

        <div className="plugin-registry-bindings">
          <ExactBindingGroup title="领域适配器" eyebrow="02 / DOMAIN ADAPTERS" emptyLabel="此冻结合同无需领域适配器">
            {view.adapters.length ? view.adapters.map((adapter) => <article key={exactRegistryKey("domain_adapter", adapter.id, adapter.version, adapter.contractHash)} role="listitem">
              <strong>{adapter.id}@{adapter.version}</strong>
              <small>contract {shortPluginHash(adapter.contractHash)}</small>
              {adapter.ports.length ? <details><summary>{adapter.ports.length} 个精确端口</summary><ul>{adapter.ports.map((port) => <li key={exactRegistryKey("domain_port", adapter.id, adapter.version, port.id, port.version, port.contractHash)}>{port.id}@{port.version} · {shortPluginHash(port.contractHash)}</li>)}</ul></details> : <small>无端口声明</small>}
            </article>) : null}
          </ExactBindingGroup>
          <ExactBindingGroup title="宿主界面贡献" eyebrow="03 / HOST UI" emptyLabel="此冻结合同没有宿主界面贡献">
            {view.contributions.length ? view.contributions.map((contribution) => <article key={exactRegistryKey("ui_contribution", contribution.id, contribution.version, contribution.contractHash, contribution.slotId)} role="listitem">
              <strong>{contribution.label}</strong>
              <small>{contribution.id}@{contribution.version}</small>
              <span>{contribution.slotId}</span>
              <small>contract {shortPluginHash(contribution.contractHash)}</small>
            </article>) : null}
          </ExactBindingGroup>
        </div>
      </div>
    </section>

    <p className="plugin-registry-safety" role="note"><ShieldCheck aria-hidden="true" size={14} />无执行能力、无自动决定、无任意代码加载；最终决定区始终由群聊内核和用户掌控。</p>
  </section>;
});
