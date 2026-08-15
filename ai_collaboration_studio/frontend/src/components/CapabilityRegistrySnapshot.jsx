import { AlertTriangle, Boxes, ShieldCheck } from "lucide-react";
import { useMemo } from "react";
import {
  capabilityRegistryView,
  shortPluginHash,
} from "../capabilityContributions";
import {
  pluginLifecycleCatalogView,
  pluginLifecycleRuntimeReason,
  pluginLifecycleStateLabel,
  pluginLifecycleTarget,
} from "../pluginLifecycle";

function sameSelection(left, right) {
  const a = [...new Set(left || [])].sort();
  const b = [...new Set(right || [])].sort();
  return a.length === b.length && a.every((item, index) => item === b[index]);
}

export function CapabilityRegistrySnapshot({ room, capabilityPacks, pluginLifecycle, pendingPackIds }) {
  const view = useMemo(
    () => capabilityRegistryView(room, capabilityPacks),
    [room, capabilityPacks],
  );
  const lifecycleView = useMemo(
    () => pluginLifecycleCatalogView(pluginLifecycle),
    [pluginLifecycle],
  );
  const pendingChanged = !sameSelection(
    room?.capability_pack_ids,
    pendingPackIds ?? room?.capability_pack_ids,
  );

  if (!view.integrityOk) {
    return <section className="plugin-registry-snapshot integrity-failed" aria-label="房间插件合同异常">
      <header><span><AlertTriangle size={16} /><strong>插件合同无法验证</strong></span><small>{shortPluginHash(view.hash)}</small></header>
      <p>能力包、领域适配器和界面贡献已失败关闭；不会加载未知组件或静默换算版本。</p>
      <ul>{view.errors.map((error) => <li key={error}>{error}</li>)}</ul>
    </section>;
  }

  return <section className="plugin-registry-snapshot" aria-label="当前房间插件合同">
    <header>
      <span><Boxes size={16} /><strong>当前房间插件合同</strong></span>
      <small>{view.version} · {shortPluginHash(view.hash)}</small>
    </header>
    {pendingChanged ? <p className="plugin-registry-next-note">能力包选择已变化；保存时由服务端解析新版本，当前轮次继续使用这里的冻结合同。</p> : null}
    {!lifecycleView.integrityOk ? <p className="plugin-registry-lifecycle-warning"><AlertTriangle size={13} />当前生命周期状态无法验证；新绑定和插件动作已关闭，但冻结合同本身仍可独立审计。</p> : null}
    <div className="plugin-registry-pack-list">
      {view.packs.map((pack) => {
        const lifecycle = pluginLifecycleTarget(lifecycleView, {
          kind: "capability_pack",
          id: pack.id,
          version: pack.version,
          sha256: pack.manifestHash,
        });
        return <article className={lifecycle?.runtimeState || "lifecycle-unverified"} key={pack.id}>
          <span><strong>{pack.name}</strong><small>{pack.id}@{pack.version} · {shortPluginHash(pack.manifestHash)}</small>{lifecycle && !lifecycle.runtimeAvailable ? <small>{pluginLifecycleRuntimeReason(lifecycle)}</small> : null}</span>
          <em>{pack.systemManaged ? "内核管理" : lifecycle ? pluginLifecycleStateLabel(lifecycle) : "状态未验证"}</em>
        </article>;
      })}
    </div>
    <div className="plugin-registry-bindings">
      <section><strong>领域适配器</strong>{view.adapters.length
        ? view.adapters.map((adapter) => <span key={adapter.id}>{adapter.id}@{adapter.version} · {shortPluginHash(adapter.contractHash)}</span>)
        : <span>无需领域适配器</span>}</section>
      <section><strong>宿主界面贡献</strong>{view.contributions.map((contribution) => <span key={contribution.id}>{contribution.label}<small>{contribution.slotId}</small></span>)}</section>
    </div>
    <p className="plugin-registry-safety"><ShieldCheck size={14} />无执行能力、无自动决定、无任意代码加载；最终决定区始终由群聊内核和用户掌控。</p>
  </section>;
}
