const ACTION_PRESENTATIONS = Object.freeze({
  disable: { tone: "restrict", eyebrow: "可逆限制", summary: "停止新绑定和插件动作，历史记录继续只读保留。" },
  enable: { tone: "restore", eyebrow: "恢复可用", summary: "在精确版本和实现仍可核验时，恢复新绑定能力。" },
  quarantine: { tone: "caution", eyebrow: "隔离处置", summary: "立即隔离新绑定与插件动作，等待单独复核。" },
  clear_quarantine: { tone: "restore", eyebrow: "解除隔离", summary: "解除隔离，但不迁移历史房间或替换既有版本。" },
  deprecate: { tone: "caution", eyebrow: "计划退役", summary: "标记为弃用并关闭新绑定，现有历史仍保持原版本。" },
  reinstate: { tone: "restore", eyebrow: "撤销退役", summary: "撤销弃用状态；不会自动改变既有房间的绑定。" },
  tombstone: { tone: "terminal", eyebrow: "不可逆状态", summary: "永久关闭该精确版本的新绑定；历史数据不会删除。" },
});

function cleanText(value) {
  return typeof value === "string" ? value.trim() : "";
}

function list(value) {
  return Array.isArray(value) ? value : [];
}

export function pluginLifecycleTargetKey(target) {
  if (!target) return "";
  return JSON.stringify([
    cleanText(target.kind),
    cleanText(target.id),
    cleanText(target.version),
    cleanText(target.targetSha256 || target.sha256).toLowerCase(),
  ]);
}

export function pluginLifecycleActionPresentation(action) {
  return ACTION_PRESENTATIONS[action] || {
    tone: "restrict",
    eyebrow: "受控变更",
    summary: "仅在影响预览与精确目标仍一致时允许提交。",
  };
}

export function pluginLifecycleReviewControl({
  review,
  reason,
  historyConfirmed,
  migrationConfirmed,
  tombstoneConfirmation,
} = {}) {
  const exists = Boolean(review);
  const previewReady = review?.preview?.integrityOk === true;
  const reasonLength = cleanText(reason).length;
  const reasonReady = reasonLength >= 4 && reasonLength <= 500;
  const historyReady = historyConfirmed === true;
  const migrationReady = migrationConfirmed === true;
  const tombstoneRequired = review?.action === "tombstone";
  const targetLabel = cleanText(review?.target?.label) || cleanText(review?.target?.id);
  const tombstoneReady = !tombstoneRequired
    || (Boolean(targetLabel) && cleanText(tombstoneConfirmation) === targetLabel);
  const permitChecks = [
    { id: "preview", label: "影响预览已封印并绑定精确版本", passed: previewReady },
    { id: "reason", label: "变更原因已记录（4–500 字符）", passed: reasonReady },
    { id: "history", label: "历史记录只读保留", passed: historyReady },
    { id: "migration", label: "不自动迁移或替换", passed: migrationReady },
    ...(tombstoneRequired
      ? [{ id: "tombstone", label: `已精确输入“${targetLabel || "能力包名称"}”`, passed: tombstoneReady }]
      : []),
  ];
  const canSubmit = exists && !review.busy && permitChecks.every((check) => check.passed);
  let phase = "idle";
  if (review?.busy && !review.preview) phase = "previewing";
  else if (review?.busy) phase = "submitting";
  else if (previewReady && canSubmit) phase = "ready";
  else if (previewReady) phase = "confirming";
  else if (exists) phase = "blocked";
  const firstMissing = permitChecks.find((check) => !check.passed);
  const instruction = phase === "previewing"
    ? "正在读取服务端冻结影响范围。"
    : phase === "submitting"
      ? "正在提交与本次预览绑定的生命周期变更。"
      : phase === "ready"
        ? "许可清单完整，可以提交本次受控变更。"
        : firstMissing?.label || "先选择一个生命周期动作并读取影响预览。";
  return {
    canSubmit,
    instruction,
    permitChecks,
    phase,
    previewReady,
    reasonLength,
    tombstoneRequired,
    tombstoneReady,
  };
}

export function pluginLifecycleCatalogPresentation(view) {
  const packs = list(view?.capabilityPacks);
  const targets = packs.map((pack) => pack?.lifecycle).filter(Boolean);
  return {
    total: packs.length,
    ready: targets.filter((target) => target.runtimeAvailable === true).length,
    restricted: targets.filter((target) => !target.systemManaged && target.runtimeAvailable !== true).length,
    actionable: targets.filter((target) => list(target.availableActions).length > 0).length,
    systemManaged: targets.filter((target) => target.systemManaged === true).length,
  };
}
