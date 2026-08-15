const SHA256_PATTERN = /^[0-9a-f]{64}$/;

export const PLUGIN_LIFECYCLE_CATALOG_VERSION = "plugin_lifecycle_catalog_v1";
export const PLUGIN_LIFECYCLE_EVENT_VERSION_V1 = "plugin_lifecycle_event_v1";
export const PLUGIN_LIFECYCLE_EVENT_VERSION = "plugin_lifecycle_event_v2";
export const PLUGIN_LIFECYCLE_PREVIEW_REQUEST_VERSION = "plugin_lifecycle_preview_request_v1";
export const PLUGIN_LIFECYCLE_PREVIEW_VERSION_V1 = "plugin_lifecycle_impact_preview_v1";
export const PLUGIN_LIFECYCLE_PREVIEW_VERSION = "plugin_lifecycle_impact_preview_v2";
export const PLUGIN_LIFECYCLE_TRANSITION_REQUEST_VERSION = "plugin_lifecycle_transition_request_v1";
export const PLUGIN_LIFECYCLE_TRANSITION_RESULT_VERSION = "plugin_lifecycle_transition_result_v1";

const PLUGIN_REGISTRY_CATALOG_VERSIONS = new Set([
  "plugin_registry_catalog_v1",
  "plugin_registry_catalog_v2",
]);

const TARGET_KINDS = new Set(["capability_pack", "domain_adapter", "ui_contribution"]);
const CATALOG_STATES = new Set(["active", "deprecated", "tombstoned"]);
const ACTIVATION_STATES = new Set(["enabled", "disabled", "quarantined"]);
const RUNTIME_STATES = new Set([
  "ready",
  "disabled",
  "quarantined",
  "deprecated",
  "tombstoned",
  "implementation_unavailable",
  "lifecycle_integrity_failed",
]);
const REPLACEMENT_RUNTIME_STATES = new Set([
  ...RUNTIME_STATES,
  "replacement_unavailable",
]);
const LIFECYCLE_ACTIONS = new Set([
  "disable",
  "enable",
  "quarantine",
  "clear_quarantine",
  "deprecate",
  "reinstate",
  "tombstone",
]);
const SAFE_RETIREMENT_ACTIONS = new Set([
  "disable",
  "quarantine",
  "deprecate",
  "tombstone",
]);
const RECOVERY_ACTIONS = new Set([
  "enable",
  "clear_quarantine",
  "reinstate",
]);
const FIXED_SAFETY = Object.freeze({
  execution_capability: "none",
  live_trading_allowed: false,
  can_autonomously_decide: false,
  can_replace_user_decision: false,
  arbitrary_code_loading_allowed: false,
  user_final_decision_required: true,
});

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

function rows(value) {
  return Array.isArray(value) ? value : [];
}

function sha256(value) {
  const clean = text(value).toLowerCase();
  return SHA256_PATTERN.test(clean) ? clean : "";
}

function nonnegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function safetyMatches(value) {
  const safety = record(value);
  return Object.entries(FIXED_SAFETY).every(([key, expected]) => safety[key] === expected);
}

function forbiddenUserDecisionSlot(value) {
  const slotId = text(value);
  return slotId.includes("user_decision") || slotId.includes("user-decision");
}

function targetIdentity(target) {
  return `${target.kind}:${target.id}@${target.version}:${target.targetSha256}`;
}

function publicTargetRef(target) {
  return {
    kind: target.kind,
    id: target.id,
    version: target.version,
    sha256: target.targetSha256,
  };
}

function normalizeReplacementRef(raw, sourceTarget, errors, field = "生命周期替代声明") {
  if (raw === null || raw === undefined) return null;
  const value = record(raw);
  const replacement = {
    kind: text(value.kind).toLowerCase(),
    id: text(value.id),
    version: text(value.version),
    sha256: sha256(value.sha256),
  };
  if (!TARGET_KINDS.has(replacement.kind)
    || !replacement.id
    || !replacement.version
    || !replacement.sha256) {
    errors.push(`${field}身份无效`);
    return replacement;
  }
  if (replacement.kind !== sourceTarget.kind
    || replacement.id !== sourceTarget.id
    || replacement.version === sourceTarget.version) {
    errors.push(`${field}必须绑定同一稳定 ID 的不同精确版本`);
  }
  return replacement;
}

function normalizeReplacementStatus(raw, replacement, errors, { required = false } = {}) {
  const present = raw && typeof raw === "object" && !Array.isArray(raw);
  if (!present) {
    if (required) errors.push("生命周期替代状态缺失");
    return null;
  }
  const value = record(raw);
  const status = {
    declared: value.declared === true,
    currentRuntimeState: text(value.current_runtime_state).toLowerCase(),
    currentRuntimeAvailable: value.current_runtime_available === true,
    integrityOk: value.integrity_ok === true,
    automaticMigrationPerformed: value.automatic_migration_performed === true,
    exactTargetFound: false,
    targetLabel: replacement?.id || "",
    usable: false,
  };
  const booleanFieldsValid = typeof value.declared === "boolean"
    && typeof value.current_runtime_available === "boolean"
    && typeof value.integrity_ok === "boolean"
    && typeof value.automatic_migration_performed === "boolean";
  if (!booleanFieldsValid) errors.push("生命周期替代状态布尔字段无效");
  if (status.automaticMigrationPerformed) {
    errors.push("生命周期替代状态禁止自动迁移");
  }
  if (status.declared !== Boolean(replacement)) {
    errors.push("生命周期替代声明与状态不一致");
  }
  if (!replacement) {
    if (status.declared
      || status.currentRuntimeState !== ""
      || status.currentRuntimeAvailable
      || !status.integrityOk) {
      errors.push("未声明替代版本时必须返回规范空状态");
    }
  } else {
    if (!REPLACEMENT_RUNTIME_STATES.has(status.currentRuntimeState)) {
      errors.push("生命周期替代版本运行状态无效");
    }
    if (status.currentRuntimeAvailable !== (status.currentRuntimeState === "ready")) {
      errors.push("生命周期替代版本运行布尔值不一致");
    }
  }
  return status;
}

function sameNullableTargetRef(left, right) {
  if (!left || !right) return !left && !right;
  return sameTargetRef(left, right);
}

function normalizeLifecycleEvent(raw, sourceTarget, errors) {
  const value = record(raw);
  const version = text(value.version);
  const sequenceNo = nonnegativeInteger(value.sequence_no);
  const action = text(value.action).toLowerCase();
  const versionSupported = version === PLUGIN_LIFECYCLE_EVENT_VERSION_V1
    || version === PLUGIN_LIFECYCLE_EVENT_VERSION;
  if (!versionSupported) errors.push("生命周期事件版本不受支持");
  if (!text(value.id)
    || sequenceNo === null
    || sequenceNo <= 0
    || !LIFECYCLE_ACTIONS.has(action)
    || !sha256(value.event_sha256)) {
    errors.push("生命周期事件身份或动作无效");
  }
  if (version === PLUGIN_LIFECYCLE_EVENT_VERSION
    && typeof value.implementation_available_at_event !== "boolean") {
    errors.push("生命周期 v2 事件缺少事件时实现可用性");
  }
  const replacement = normalizeReplacementRef(
    value.replacement,
    sourceTarget,
    errors,
    "生命周期事件替代声明",
  );
  const effectiveReplacement = normalizeReplacementRef(
    value.effective_replacement,
    sourceTarget,
    errors,
    "生命周期事件生效替代声明",
  );
  const implementationAvailableAtEvent = version === PLUGIN_LIFECYCLE_EVENT_VERSION
    ? value.implementation_available_at_event === true
    : true;
  if (version === PLUGIN_LIFECYCLE_EVENT_VERSION
    && !implementationAvailableAtEvent
    && RECOVERY_ACTIONS.has(action)) {
    errors.push("生命周期 v2 事件在实现不可用时禁止恢复类动作");
  }
  const normalizedSequenceNo = sequenceNo ?? 0;
  const eventSha256 = sha256(value.event_sha256);
  return {
    version,
    id: text(value.id),
    sequence_no: normalizedSequenceNo,
    sequenceNo: normalizedSequenceNo,
    action,
    catalog_state: text(value.catalog_state).toLowerCase(),
    activation_state: text(value.activation_state).toLowerCase(),
    reason: text(value.reason),
    replacement,
    effective_replacement: effectiveReplacement,
    effectiveReplacement,
    implementation_available_at_event: implementationAvailableAtEvent,
    implementationAvailableAtEvent,
    implementationAvailabilityAttested: version === PLUGIN_LIFECYCLE_EVENT_VERSION,
    created_at: nonnegativeInteger(value.created_at) ?? 0,
    event_sha256: eventSha256,
    eventSha256,
  };
}

function exactReference(raw, kind) {
  const value = record(raw);
  if (kind === "capability_pack") {
    return {
      kind,
      id: text(value.id),
      version: text(value.pack_version),
      sha256: sha256(value.manifest_sha256),
    };
  }
  if (kind === "domain_adapter") {
    return {
      kind,
      id: text(value.adapter_id),
      version: text(value.adapter_version),
      sha256: sha256(value.contract_sha256),
    };
  }
  return {
    kind,
    id: text(value.contribution_id),
    version: text(value.contribution_version),
    sha256: sha256(value.contract_sha256),
  };
}

function expectedRuntimeState(target, exactImplementation) {
  if (!target.integrityOk) return "lifecycle_integrity_failed";
  if (target.catalogState === "tombstoned") return "tombstoned";
  if (target.activationState === "quarantined") return "quarantined";
  if (target.catalogState === "deprecated") return "deprecated";
  if (target.activationState === "disabled") return "disabled";
  return exactImplementation ? "ready" : "implementation_unavailable";
}

function normalizeTarget(raw, errors, { requireReplacementStatus = false } = {}) {
  const value = record(raw);
  const kind = text(value.kind).toLowerCase();
  const id = text(value.id);
  const version = text(value.version);
  const targetSha256 = sha256(value.target_sha256);
  const catalogState = text(value.catalog_state).toLowerCase();
  const activationState = text(value.activation_state).toLowerCase();
  const runtimeState = text(value.runtime_state).toLowerCase();
  const headSequence = nonnegativeInteger(value.head_sequence);
  const headSha256 = sha256(value.head_sha256);
  const availableActions = rows(value.available_actions).map((item) => text(item).toLowerCase());
  const identity = `${kind}:${id}@${version}`;
  const replacement = normalizeReplacementRef(
    value.replacement,
    { kind, id, version },
    errors,
  );
  const replacementStatus = normalizeReplacementStatus(
    value.replacement_status,
    replacement,
    errors,
    { required: requireReplacementStatus },
  );
  const history = rows(value.history).map((event) => normalizeLifecycleEvent(
    event,
    { kind, id, version },
    errors,
  ));

  if (!TARGET_KINDS.has(kind) || !id || !version || !targetSha256) {
    errors.push(`生命周期目标身份无效：${identity || "unknown"}`);
  }
  if (!CATALOG_STATES.has(catalogState) || !ACTIVATION_STATES.has(activationState)) {
    errors.push(`生命周期目标状态无效：${identity}`);
  }
  if (!RUNTIME_STATES.has(runtimeState)) errors.push(`生命周期运行状态无效：${identity}`);
  if (headSequence === null || !headSha256) errors.push(`生命周期 head 无效：${identity}`);
  if (availableActions.some((action) => !LIFECYCLE_ACTIONS.has(action))
    || new Set(availableActions).size !== availableActions.length) {
    errors.push(`生命周期动作列表无效：${identity}`);
  }
  if (value.system_managed === true && availableActions.length) {
    errors.push(`内核管理目标不能提供生命周期动作：${identity}`);
  }
  if (value.integrity_ok !== true && availableActions.length) {
    errors.push(`完整性异常目标不能提供生命周期动作：${identity}`);
  }
  if (value.implementation_available !== true
    && availableActions.some((action) => !SAFE_RETIREMENT_ACTIONS.has(action))) {
    errors.push(`本机实现不可用目标只能提供安全退休动作：${identity}`);
  }
  if (value.runtime_available !== (runtimeState === "ready")
    || value.new_bindings_allowed !== (runtimeState === "ready")) {
    errors.push(`生命周期运行布尔值不一致：${identity}`);
  }

  return {
    kind,
    id,
    version,
    targetSha256,
    label: text(value.label) || id,
    systemManaged: value.system_managed === true,
    ownerPackIds: rows(value.owner_pack_ids).map(text).filter(Boolean),
    dependencies: rows(value.dependencies).map(text).filter(Boolean),
    catalogState,
    activationState,
    runtimeState,
    integrityOk: value.integrity_ok === true,
    implementationAvailable: value.implementation_available === true,
    runtimeAvailable: value.runtime_available === true,
    newBindingsAllowed: value.new_bindings_allowed === true,
    headSequence: headSequence ?? 0,
    headSha256,
    currentEventId: text(value.current_event_id),
    currentEventSha256: sha256(value.current_event_sha256),
    reason: text(value.reason),
    replacement,
    replacementStatus,
    availableActions,
    integrityIssues: rows(value.integrity_issues).map(text).filter(Boolean),
    history,
  };
}

function replacementGraphHasCycle(targets, targetByIdentity) {
  const edges = new Map();
  for (const target of targets) {
    if (!target.replacement || !target.replacementStatus?.exactlyVerified) continue;
    const sourceIdentity = targetIdentity(target);
    const replacementIdentity = `${target.replacement.kind}:${target.replacement.id}@${target.replacement.version}:${target.replacement.sha256}`;
    if (targetByIdentity.has(replacementIdentity)) edges.set(sourceIdentity, replacementIdentity);
  }
  const visitState = new Map();
  const visit = (identity) => {
    const state = visitState.get(identity) || 0;
    if (state === 1) return true;
    if (state === 2) return false;
    visitState.set(identity, 1);
    const next = edges.get(identity);
    if (next && visit(next)) return true;
    visitState.set(identity, 2);
    return false;
  };
  return [...edges.keys()].some((identity) => visit(identity));
}

export function pluginLifecycleCatalogView(value) {
  const source = record(value);
  const errors = [];
  if (source.version !== PLUGIN_LIFECYCLE_CATALOG_VERSION) {
    errors.push("生命周期目录版本不受支持");
  }
  if (!sha256(source.view_sha256)) errors.push("生命周期目录封印无效");
  if (!safetyMatches(source.safety)) errors.push("生命周期目录安全字段漂移");
  if (source.integrity_ok !== true) errors.push("服务端未确认生命周期目录完整性");

  const pluginRegistry = record(source.plugin_registry);
  const pluginRegistryVersion = text(pluginRegistry.version);
  const registryVersionShapeValid = pluginRegistryVersion === "plugin_registry_catalog_v1"
    ? !Object.hasOwn(pluginRegistry, "domain_adapter_ports")
      && !Object.hasOwn(pluginRegistry, "ui_view_model_schemas")
    : Array.isArray(pluginRegistry.domain_adapter_ports)
      && pluginRegistry.domain_adapter_ports.length > 0
      && Array.isArray(pluginRegistry.ui_view_model_schemas)
      && pluginRegistry.ui_view_model_schemas.length > 0;
  if (!PLUGIN_REGISTRY_CATALOG_VERSIONS.has(pluginRegistryVersion)
    || !registryVersionShapeValid
    || !sha256(pluginRegistry.catalog_sha256)
    || !safetyMatches(pluginRegistry.safety)) {
    errors.push("生命周期目录绑定的插件目录无效");
  }
  if (rows(pluginRegistry.ui_contributions).some((item) => forbiddenUserDecisionSlot(item?.slot_id))) {
    errors.push("用户最终决定区禁止插件贡献");
  }

  const references = new Map();
  for (const pack of rows(source.capability_packs)) {
    const ref = exactReference(pack, "capability_pack");
    references.set(`${ref.kind}:${ref.id}@${ref.version}:${ref.sha256}`, { ref, raw: pack });
  }
  for (const adapter of rows(pluginRegistry.domain_adapters)) {
    const ref = exactReference(adapter, "domain_adapter");
    references.set(`${ref.kind}:${ref.id}@${ref.version}:${ref.sha256}`, { ref, raw: adapter });
  }
  for (const contribution of rows(pluginRegistry.ui_contributions)) {
    const ref = exactReference(contribution, "ui_contribution");
    references.set(`${ref.kind}:${ref.id}@${ref.version}:${ref.sha256}`, { ref, raw: contribution });
  }

  const targets = rows(source.targets).map((item) => normalizeTarget(
    item,
    errors,
    { requireReplacementStatus: true },
  ));
  const identities = targets.map((target) => `${target.kind}:${target.id}@${target.version}`);
  if (identities.some((identity) => identity.endsWith("@"))
    || new Set(identities).size !== identities.length) {
    errors.push("生命周期目标身份重复或缺失");
  }
  for (const target of targets) {
    const exactImplementation = references.has(targetIdentity(target));
    if (target.implementationAvailable !== exactImplementation) {
      errors.push(`生命周期实现可用性与精确目录不一致：${target.kind}:${target.id}@${target.version}`);
    }
    const expected = expectedRuntimeState(target, exactImplementation);
    if (target.runtimeState !== expected) {
      errors.push(`生命周期目标状态组合不一致：${target.kind}:${target.id}@${target.version}`);
    }
  }

  const targetByIdentity = new Map(targets.map((target) => [targetIdentity(target), target]));
  for (const target of targets) {
    if (!target.replacement || !target.replacementStatus?.declared) continue;
    const replacementTarget = targetByIdentity.get(
      `${target.replacement.kind}:${target.replacement.id}@${target.replacement.version}:${target.replacement.sha256}`,
    ) || null;
    const replacementStatusMatches = Boolean(
      replacementTarget
      && target.replacementStatus.currentRuntimeState === replacementTarget.runtimeState
      && target.replacementStatus.currentRuntimeAvailable === replacementTarget.runtimeAvailable
      && target.replacementStatus.integrityOk === replacementTarget.integrityOk,
    );
    if (replacementTarget) {
      if (!replacementStatusMatches) {
        errors.push(`生命周期替代状态与精确目标不一致：${target.kind}:${target.id}@${target.version}`);
      }
    } else {
      errors.push(`声明的替代目标未保留在生命周期目录：${target.kind}:${target.id}@${target.version}`);
      if (target.replacementStatus.currentRuntimeState !== "replacement_unavailable"
        || target.replacementStatus.currentRuntimeAvailable
        || target.replacementStatus.integrityOk) {
        errors.push(`缺失替代目标必须明确标记为不可用：${target.kind}:${target.id}@${target.version}`);
      }
    }
    target.replacementStatus = {
      ...target.replacementStatus,
      exactTargetFound: Boolean(replacementTarget),
      exactlyVerified: Boolean(
        replacementStatusMatches
        && target.replacementStatus.integrityOk
        && replacementTarget?.integrityOk,
      ),
      targetLabel: replacementTarget?.label || target.replacement.id,
      usable: Boolean(
        replacementStatusMatches
        && target.replacementStatus.integrityOk
        && target.replacementStatus.currentRuntimeAvailable,
      ),
    };
  }
  if (replacementGraphHasCycle(targets, targetByIdentity)) {
    errors.push("生命周期替代声明图存在环，管理动作已关闭");
  }
  const capabilityPacks = rows(source.capability_packs).map((raw) => {
    const ref = exactReference(raw, "capability_pack");
    const lifecycle = targetByIdentity.get(`${ref.kind}:${ref.id}@${ref.version}:${ref.sha256}`) || null;
    if (!lifecycle) errors.push(`能力包缺少精确生命周期目标：${ref.id}@${ref.version}`);
    return {
      ...raw,
      lifecycle,
    };
  });

  return {
    status: errors.length ? "integrity_failed" : "ready",
    integrityOk: errors.length === 0,
    errors: [...new Set(errors)],
    viewSha256: sha256(source.view_sha256),
    targets,
    targetByIdentity,
    capabilityPacks,
    replacementDeclarations: targets.filter(
      (target) => target.replacement && target.replacementStatus?.declared,
    ),
    pluginRegistry,
    safety: FIXED_SAFETY,
  };
}

export function pluginLifecycleTarget(view, { kind, id, version, sha256: targetSha256 }) {
  if (!view?.integrityOk) return null;
  return view.targetByIdentity.get(
    `${text(kind).toLowerCase()}:${text(id)}@${text(version)}:${sha256(targetSha256)}`,
  ) || null;
}

export function pluginLifecyclePack(view, packId) {
  const id = text(packId);
  return view?.capabilityPacks?.find((pack) => text(pack.id) === id) || null;
}

export function packSelectionAvailability(view, packId, { selected = false } = {}) {
  const pack = pluginLifecyclePack(view, packId);
  const lifecycle = pack?.lifecycle || null;
  const canAdd = Boolean(view?.integrityOk && lifecycle?.newBindingsAllowed);
  return {
    pack,
    lifecycle,
    canAdd,
    canToggle: Boolean(selected || canAdd),
    reason: !view?.integrityOk
      ? "能力包状态无法验证，不能建立新绑定。"
      : !lifecycle
        ? "能力包没有精确生命周期记录，不能建立新绑定。"
        : canAdd
          ? ""
          : pluginLifecycleRuntimeReason(lifecycle),
  };
}

export function filterNewPackBindings(packIds, view) {
  const allowed = [];
  const blocked = [];
  for (const rawId of rows(packIds)) {
    const id = text(rawId);
    if (!id || allowed.includes(id) || blocked.includes(id)) continue;
    if (packSelectionAvailability(view, id).canAdd) allowed.push(id);
    else blocked.push(id);
  }
  return { allowed, blocked };
}

export function pluginLifecycleStateLabel(target) {
  const state = typeof target === "string" ? target : target?.runtimeState;
  if (state === "ready") return "可使用";
  if (state === "disabled") return "已停用";
  if (state === "quarantined") return "已隔离";
  if (state === "deprecated") return "准备停用";
  if (state === "tombstoned") return "已永久停用";
  if (state === "implementation_unavailable") return "当前版本不可用";
  if (state === "lifecycle_integrity_failed") return "状态无法验证";
  return "状态未知";
}

export function pluginLifecycleRuntimeReason(target) {
  if (!target) return "没有可核验的生命周期记录。";
  if (target.runtimeState === "disabled") return "该能力包已停用；已有历史保留只读，可由允许的恢复操作重新启用。";
  if (target.runtimeState === "quarantined") return "该能力包已紧急隔离；插件动作和新绑定均已关闭。";
  if (target.runtimeState === "deprecated") return "该能力包已标记为准备停用；不会建立新绑定或执行插件动作。";
  if (target.runtimeState === "tombstoned") return "该能力包已永久停用；只保留历史记录，不能恢复。";
  if (target.runtimeState === "implementation_unavailable") return "冻结合同仍可核验，但本机没有精确匹配的实现。";
  if (target.runtimeState === "lifecycle_integrity_failed") return "生命周期事件链无法验证，相关动作已失败关闭。";
  return target.reason || "";
}

export function pluginLifecycleActionLabel(action) {
  if (action === "disable") return "停用";
  if (action === "enable") return "恢复启用";
  if (action === "quarantine") return "紧急隔离";
  if (action === "clear_quarantine") return "解除隔离";
  if (action === "deprecate") return "标记为准备停用";
  if (action === "reinstate") return "取消准备停用";
  if (action === "tombstone") return "永久停用并保留历史";
  return action;
}

export function buildPluginLifecyclePreviewRequest(target, action) {
  if (!target || !LIFECYCLE_ACTIONS.has(action)) throw new Error("生命周期目标或动作无效。");
  return {
    version: PLUGIN_LIFECYCLE_PREVIEW_REQUEST_VERSION,
    target: publicTargetRef(target),
    action,
    expected_head_sequence: target.headSequence,
    expected_head_sha256: target.headSha256,
    replacement: null,
  };
}

function sameTargetRef(left, right) {
  return left?.kind === right?.kind
    && left?.id === right?.id
    && left?.version === right?.version
    && left?.sha256 === right?.sha256;
}

export function pluginLifecycleImpactPreviewView(value, { target, action } = {}) {
  const source = record(value);
  const errors = [];
  const rawTarget = record(source.target);
  const previewTarget = {
    kind: text(rawTarget.kind).toLowerCase(),
    id: text(rawTarget.id),
    version: text(rawTarget.version),
    sha256: sha256(rawTarget.sha256),
  };
  const expectedTarget = target ? publicTargetRef(target) : null;
  const previewVersion = text(source.version);
  const versionSupported = previewVersion === PLUGIN_LIFECYCLE_PREVIEW_VERSION_V1
    || previewVersion === PLUGIN_LIFECYCLE_PREVIEW_VERSION;
  if (!versionSupported) errors.push("影响预览版本不受支持");
  if (!previewTarget.sha256 || !TARGET_KINDS.has(previewTarget.kind)) errors.push("影响预览目标无效");
  if (expectedTarget && !sameTargetRef(previewTarget, expectedTarget)) errors.push("影响预览目标已经变化");
  if (!LIFECYCLE_ACTIONS.has(source.action) || (action && source.action !== action)) {
    errors.push("影响预览动作已经变化");
  }
  if (source.expected_head_sequence !== target?.headSequence
    || source.expected_head_sha256 !== target?.headSha256) {
    errors.push("影响预览生命周期 head 已变化");
  }
  if (!sha256(source.preview_sha256)) errors.push("影响预览封印无效");
  if (!safetyMatches(source.safety)) errors.push("影响预览安全字段漂移");

  const current = record(source.current);
  const result = record(source.result);
  const implementationAvailabilityAttested = previewVersion === PLUGIN_LIFECYCLE_PREVIEW_VERSION;
  if (implementationAvailabilityAttested
    && typeof current.implementation_available !== "boolean") {
    errors.push("影响预览 v2 缺少当前实现可用性");
  }
  if (implementationAvailabilityAttested
    && target
    && current.implementation_available !== target.implementationAvailable) {
    errors.push("影响预览当前实现可用性已经变化");
  }
  const normalizedCurrent = {
    catalog_state: text(current.catalog_state).toLowerCase(),
    activation_state: text(current.activation_state).toLowerCase(),
    runtime_state: text(current.runtime_state).toLowerCase(),
    ...(implementationAvailabilityAttested
      ? { implementation_available: current.implementation_available === true }
      : {}),
  };
  if (!CATALOG_STATES.has(current.catalog_state)
    || !ACTIVATION_STATES.has(current.activation_state)
    || !RUNTIME_STATES.has(current.runtime_state)
    || !CATALOG_STATES.has(result.catalog_state)
    || !ACTIVATION_STATES.has(result.activation_state)
    || !RUNTIME_STATES.has(result.runtime_state)
    || result.runtime_available !== (result.runtime_state === "ready")
    || result.new_bindings_allowed !== (result.runtime_state === "ready")) {
    errors.push("影响预览状态组合无效");
  }

  const impact = record(source.impact);
  const counts = [
    "affected_room_count",
    "running_round_count",
    "paused_round_count",
    "historical_round_count",
    "historical_artifact_count",
  ];
  if (counts.some((key) => nonnegativeInteger(impact[key]) === null)) {
    errors.push("影响预览数量无效");
  }
  const affectedRooms = rows(impact.affected_rooms).map((room) => ({
    id: text(room?.id),
    title: text(room?.title),
  })).filter((room) => room.id && room.title);
  if (affectedRooms.length !== rows(impact.affected_rooms).length
    || affectedRooms.length > Number(impact.affected_room_count || 0)) {
    errors.push("影响房间列表无效");
  }
  if (impact.historical_records_preserved !== true
    || impact.automatic_replacement_performed !== false
    || impact.data_deletion_performed !== false
    || impact.user_final_decision_unaffected !== true) {
    errors.push("影响预览历史或用户决定边界漂移");
  }

  return {
    integrityOk: errors.length === 0,
    errors: [...new Set(errors)],
    version: previewVersion,
    target: previewTarget,
    action: text(source.action),
    current: normalizedCurrent,
    currentImplementationAvailable: implementationAvailabilityAttested
      ? current.implementation_available === true
      : null,
    implementationAvailabilityAttested,
    result,
    impact: {
      affectedRoomCount: Number(impact.affected_room_count || 0),
      affectedRooms,
      runningRoundCount: Number(impact.running_round_count || 0),
      pausedRoundCount: Number(impact.paused_round_count || 0),
      historicalRoundCount: Number(impact.historical_round_count || 0),
      historicalArtifactCount: Number(impact.historical_artifact_count || 0),
      workspaceLabels: rows(impact.workspace_labels).map(text).filter(Boolean),
      effectiveBoundary: text(impact.effective_boundary),
      userFinalDecisionUnaffected: impact.user_final_decision_unaffected === true,
    },
    previewSha256: sha256(source.preview_sha256),
  };
}

export function buildPluginLifecycleTransitionRequest({
  target,
  action,
  preview,
  clientRequestId,
  reason,
}) {
  if (!preview?.integrityOk || !target || !LIFECYCLE_ACTIONS.has(action)) {
    throw new Error("必须先取得完整且未漂移的影响预览。");
  }
  return {
    version: PLUGIN_LIFECYCLE_TRANSITION_REQUEST_VERSION,
    client_request_id: text(clientRequestId),
    target: publicTargetRef(target),
    action,
    expected_head_sequence: target.headSequence,
    expected_head_sha256: target.headSha256,
    replacement: null,
    impact_preview_sha256: preview.previewSha256,
    reason: text(reason),
    user_confirmed_history_preserved: true,
    user_confirmed_no_automatic_migration: true,
  };
}

export function pluginLifecycleTransitionResultView(value, { target, action } = {}) {
  const source = record(value?.transition || value);
  const errors = [];
  if (source.version !== PLUGIN_LIFECYCLE_TRANSITION_RESULT_VERSION) {
    errors.push("生命周期提交结果版本不受支持");
  }
  if (!safetyMatches(source.safety)) errors.push("生命周期提交结果安全字段漂移");
  const resultTargetErrors = [];
  const resultTarget = normalizeTarget(source.target, resultTargetErrors);
  errors.push(...resultTargetErrors);
  if (target && (
    resultTarget.kind !== target.kind
    || resultTarget.id !== target.id
    || resultTarget.version !== target.version
    || resultTarget.targetSha256 !== target.targetSha256
  )) errors.push("生命周期提交结果目标不一致");
  const rawEvent = record(source.event);
  const event = normalizeLifecycleEvent(rawEvent, resultTarget, errors);
  if (action && event.action !== action) errors.push("生命周期提交事件动作不一致");
  if (!Object.hasOwn(rawEvent, "effective_replacement")) {
    errors.push("生命周期提交事件缺少冻结替代声明");
  }
  if (!sameNullableTargetRef(resultTarget.replacement, event.effectiveReplacement)) {
    errors.push("生命周期提交结果替代声明不一致");
  }
  if (resultTarget.implementationAvailable !== event.implementationAvailableAtEvent) {
    errors.push("生命周期提交结果的事件时实现可用性不一致");
  }
  return {
    integrityOk: errors.length === 0,
    errors: [...new Set(errors)],
    target: resultTarget,
    event,
  };
}

export function newPluginLifecycleClientRequestId() {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) return `plugin-lifecycle:${uuid}`;
  return `plugin-lifecycle:${Date.now()}:${Math.random().toString(16).slice(2)}`;
}
