const DIRECTIONS = {
  UP: "看涨",
  DOWN: "看跌",
  NEUTRAL: "中性",
};

function objectRow(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function formText(value) {
  return typeof value === "string" ? value : "";
}

function displayText(value, fallback = "不可用") {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return fallback;
}

function finiteNumber(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function positiveVersion(value) {
  return Number.isSafeInteger(value) && value > 0;
}

export function reflectionPercentText(value, { signed = false } = {}) {
  const number = finiteNumber(value);
  if (number === null) return "不可用";
  return `${signed && number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}

export function reflectionDialogSourceState(reflection) {
  const record = objectRow(reflection);
  const snapshot = objectRow(record.source_snapshot);
  const benchmark = objectRow(snapshot.benchmark_result);
  const id = displayText(record.id, "");
  const observationId = displayText(record.observation_id, "");
  const version = record.version;
  const sourceHash = displayText(record.source_snapshot_hash, "").toLowerCase();
  const snapshotValid = Object.keys(snapshot).length > 0;
  const hashValid = /^[0-9a-f]{64}$/.test(sourceHash);
  const identityOk = Boolean(id && observationId && positiveVersion(version));
  const issues = [];

  if (reflection !== record || !Object.keys(record).length) issues.push("反思记录不是有效对象。");
  if (!id) issues.push("反思记录标识缺失。");
  if (!observationId) issues.push("来源观察标识缺失。");
  if (!positiveVersion(version)) issues.push("反思记录版本无效。");
  if (!snapshotValid) issues.push("冻结来源快照缺失。");
  if (!hashValid) issues.push("来源快照审计指纹无效。");

  const status = displayText(record.status, "DRAFT").toUpperCase();
  const direction = displayText(snapshot.direction, "");
  const relativeReturn = finiteNumber(snapshot.relative_return_pct);
  const hitText = snapshot.hit === true ? "命中" : snapshot.hit === false ? "未命中" : "命中状态不可用";
  const relativeHitText = snapshot.relative_hit === true
    ? "相对命中"
    : snapshot.relative_hit === false
      ? "相对未命中"
      : "相对状态不可用";

  return {
    form: {
      lesson: formText(record.lesson),
      caveat: formText(record.caveat),
      next_test: formText(record.next_test),
    },
    identityOk,
    confirmable: identityOk && snapshotValid && hashValid,
    integrityOk: issues.length === 0,
    issues,
    confirmed: status === "CONFIRMED",
    statusClass: status === "CONFIRMED" ? "confirmed" : "draft",
    source: {
      symbol: displayText(snapshot.symbol, "未知标的").replace("US.", ""),
      directionText: DIRECTIONS[direction] || direction || "方向不可用",
      horizonText: positiveVersion(snapshot.horizon_days) ? `${snapshot.horizon_days} 日` : "周期不可用",
      hitText,
      returnText: reflectionPercentText(snapshot.return_pct),
      peerReturnText: reflectionPercentText(benchmark.peer_equal_weight_return_pct),
      relativeText: relativeReturn === null
        ? "不可用"
        : `${reflectionPercentText(relativeReturn, { signed: true })} · ${relativeHitText}`,
      outcomeTime: displayText(snapshot.outcome_time, "未知"),
      hash: sourceHash,
      hashShort: hashValid ? sourceHash.slice(0, 12) : "不可用",
    },
  };
}

export function reflectionFormSubmission(form) {
  return {
    lesson: formText(form?.lesson).trim(),
    caveat: formText(form?.caveat).trim(),
    next_test: formText(form?.next_test).trim(),
  };
}

export function reflectionFormChanged(form, sourceForm) {
  const current = reflectionFormSubmission(form);
  const source = reflectionFormSubmission(sourceForm);
  return current.lesson !== source.lesson
    || current.caveat !== source.caveat
    || current.next_test !== source.next_test;
}

export function reflectionRequestErrorMessage(error, fallback) {
  if (error instanceof Error && typeof error.message === "string" && error.message.trim()) {
    return error.message.trim();
  }
  if (typeof error === "string" && error.trim()) return error.trim();
  return fallback;
}

export function reflectionSaveControl({
  mode,
  form,
  sourceState,
  changed,
  busy,
  saveHandlerAvailable,
  confirmHandlerAvailable,
  closeHandlerAvailable,
}) {
  const submission = reflectionFormSubmission(form);
  const confirming = mode === "confirm";
  const fieldsComplete = Boolean(submission.lesson && submission.caveat && submission.next_test);
  const checks = [
    { id: "fields", ok: fieldsComplete, label: "请完整填写教训、外推边界和下一次验证条件。" },
    { id: "identity", ok: sourceState?.identityOk === true, label: "反思记录身份或版本无效。" },
    { id: "change", ok: confirming || changed === true, label: "草稿没有待保存的变化。" },
    { id: "provenance", ok: !confirming || sourceState?.confirmable === true, label: "来源快照或审计指纹不完整，不能确认入记忆。" },
    { id: "handlers", ok: saveHandlerAvailable === true && closeHandlerAvailable === true && (!confirming || confirmHandlerAvailable === true), label: "当前操作所需处理器不可用。" },
    { id: "idle", ok: busy !== true, label: "正在处理复盘，请等待当前请求完成。" },
  ];
  const failed = checks.find((check) => !check.ok);
  return {
    checks,
    canSubmit: !failed,
    phase: busy ? "saving" : failed ? "blocked" : "ready",
    instruction: busy
      ? "正在处理复盘，请等待当前请求完成。"
      : failed?.label || (confirming ? "来源与内容允许确认入记忆。" : "草稿已通过本地保存前检查。"),
  };
}
