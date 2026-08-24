const EVIDENCE_STATUS_ITEMS = Object.freeze([
  Object.freeze({ key: "support", label: "支持" }),
  Object.freeze({ key: "counter", label: "反证" }),
  Object.freeze({ key: "conflict", label: "冲突" }),
  Object.freeze({ key: "gap", label: "缺口" }),
]);

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

function normalizedCount(value) {
  const numeric = Number(value);
  return Number.isSafeInteger(numeric) && numeric > 0 ? numeric : 0;
}

export function evidenceStatusPresentation({ counts, relationFlags, compact = false } = {}) {
  const hasCounts = counts !== null && counts !== undefined;
  const values = record(hasCounts ? counts : relationFlags);
  const allItems = EVIDENCE_STATUS_ITEMS.map((item) => {
    const value = hasCounts
      ? normalizedCount(values[item.key])
      : values[item.key] === true ? 1 : 0;
    return { ...item, value, active: value > 0 };
  });
  const visibleItems = compact ? allItems.filter((item) => item.active) : allItems;
  const total = allItems.reduce((sum, item) => sum + item.value, 0);
  return {
    items: visibleItems,
    total,
    empty: visibleItems.length === 0,
    ariaLabel: total
      ? allItems.map((item) => item.label + " " + item.value).join("，")
      : "未记录支持、反证、冲突或缺口关系",
  };
}

export function evidenceSourcePreviewPresentation({
  item,
  sourceDetail,
  sourceDetailRequired = false,
  selected = false,
  locatorLabel = "",
  sourceUrl = "",
} = {}) {
  const source = record(item);
  const detail = record(sourceDetail);
  const status = text(detail.status).toLowerCase();
  const detailOwnsPreview = Object.hasOwn(detail, "preview");
  const detailOwnsMeta = Object.hasOwn(detail, "sourceMeta");
  const preview = sourceDetailRequired && detailOwnsPreview
    ? text(detail.preview)
    : text(source.preview);
  const meta = sourceDetailRequired && detailOwnsMeta
    ? text(detail.sourceMeta)
    : text(source.sourceMeta);
  const detailReady = sourceDetailRequired && status === "ready";
  const previewComplete = detailReady
    ? detail.previewComplete === true
    : source.previewComplete === true;
  const material = text(source.type) === "material";
  const hasDisplayContent = Boolean(
    preview || meta || sourceUrl || sourceDetailRequired || !previewComplete || material,
  );

  let notice = "";
  if (detailReady && !previewComplete) {
    notice = "完整来源仍因硬上限或凭证脱敏而不完整，不能标记为已核对。";
  } else if (!sourceDetailRequired && !material && !previewComplete) {
    notice = "当前仅有截断、脱敏或预算受限的来源预览，暂不能标记为已核对；外部链接也不会自动视为已核验。";
  }
  const boundaryNote = sourceDetailRequired && !detailReady
    ? "完整内容只从该产物绑定的冻结轮次读取，不会访问实时市场或用最新版替代。"
    : "";
  const statusLabel = status === "loading"
    ? "加载中"
    : status === "ready"
      ? previewComplete ? "冻结完整" : "冻结受限"
      : status === "error"
        ? "读取失败"
        : sourceDetailRequired
          ? "待读取"
          : previewComplete
            ? "完整预览"
            : "受限预览";
  return {
    visible: hasDisplayContent,
    summaryLabel: selected ? "查看被引用的精确来源" : "预览权威精确来源",
    status,
    statusLabel,
    preview,
    meta,
    locatorLabel: text(locatorLabel),
    sourceUrl: text(sourceUrl),
    showLoadControl: sourceDetailRequired,
    canLoad: sourceDetailRequired && !["loading", "ready"].includes(status),
    loadButtonLabel: status === "loading"
      ? "正在加载完整冻结来源…"
      : status === "ready"
        ? "完整冻结来源已加载"
        : status === "error"
          ? "重试加载完整冻结来源"
          : "加载完整冻结来源",
    error: status === "error" ? text(detail.error) || "完整冻结来源读取失败。" : "",
    notice,
    boundaryNote,
    previewComplete,
  };
}
