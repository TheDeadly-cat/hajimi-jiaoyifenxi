import {
  pluginLifecycleRuntimeReason,
  pluginLifecycleStateLabel,
} from "./pluginLifecycle.js";

function rows(value) {
  return Array.isArray(value) ? value : [];
}

function normalizedSelection(value) {
  if (value === null || value === undefined) {
    return { integrityOk: true, ids: [], issue: "" };
  }
  if (!Array.isArray(value)) {
    return { integrityOk: false, ids: [], issue: "能力包选择必须是数组" };
  }
  const ids = value.map((item) => typeof item === "string" ? item.trim() : "");
  if (ids.some((id) => !id)) {
    return { integrityOk: false, ids: [], issue: "能力包选择包含空或非文本 ID" };
  }
  if (new Set(ids).size !== ids.length) {
    return { integrityOk: false, ids: [], issue: "能力包选择包含重复 ID" };
  }
  return { integrityOk: true, ids: [...ids].sort(), issue: "" };
}

export function capabilityRegistrySelectionState(currentPackIds, pendingPackIds) {
  const current = normalizedSelection(currentPackIds);
  const pending = normalizedSelection(pendingPackIds);
  const integrityOk = current.integrityOk && pending.integrityOk;
  const changed = !integrityOk
    || current.ids.length !== pending.ids.length
    || current.ids.some((id, index) => id !== pending.ids[index]);
  return {
    changed,
    currentIds: current.ids,
    integrityOk,
    issue: current.issue || pending.issue,
    pendingIds: pending.ids,
  };
}

export function capabilityRegistryPackPresentation(pack, lifecycle, {
  lifecycleIntegrityOk = false,
} = {}) {
  const lifecycleVerified = lifecycleIntegrityOk && Boolean(lifecycle);
  if (!lifecycleVerified) {
    return {
      ...pack,
      lifecycle: null,
      lifecycleVerified: false,
      runtimeAvailable: false,
      runtimeReason: "当前生命周期精确状态无法核验；冻结合同仍可独立审计。",
      state: "lifecycle-unverified",
      statusLabel: pack?.systemManaged ? "内核管理 / 未核验" : "状态未验证",
    };
  }
  const runtimeAvailable = lifecycle.runtimeAvailable === true;
  return {
    ...pack,
    lifecycle,
    lifecycleVerified: true,
    runtimeAvailable,
    runtimeReason: runtimeAvailable
      ? "当前精确版本可用于新绑定和插件操作。"
      : pluginLifecycleRuntimeReason(lifecycle),
    state: pack?.systemManaged ? "system-managed" : lifecycle.runtimeState || "lifecycle-unverified",
    statusLabel: pack?.systemManaged ? "内核管理" : pluginLifecycleStateLabel(lifecycle),
  };
}

export function capabilityRegistrySnapshotPresentation({
  view,
  lifecycleIntegrityOk,
  packRows,
} = {}) {
  const packs = rows(packRows);
  const frozenIntegrityOk = view?.integrityOk === true;
  const currentLifecycleVerified = lifecycleIntegrityOk === true
    && packs.every((pack) => pack.lifecycleVerified);
  const trustState = !frozenIntegrityOk
    ? "failed"
    : currentLifecycleVerified
      ? "sealed-current"
      : "sealed-frozen-only";
  return {
    trustState,
    trustLabel: trustState === "sealed-current"
      ? "冻结合同与当前状态均已核验"
      : trustState === "sealed-frozen-only"
        ? "冻结合同可审计 / 当前状态未核验"
        : "冻结合同无法验证",
    stats: {
      packs: packs.length,
      adapters: rows(view?.adapters).length,
      contributions: rows(view?.contributions).length,
      currentReady: currentLifecycleVerified
        ? packs.filter((pack) => pack.runtimeAvailable).length
        : null,
    },
  };
}
