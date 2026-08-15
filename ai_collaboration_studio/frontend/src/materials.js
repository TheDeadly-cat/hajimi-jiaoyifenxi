export const MAX_MATERIAL_FILE_BYTES = 2_000_000;

const storageSymbols = new Set(["US.MU", "US.SNDK", "US.WDC", "US.STX"]);

const promptRiskLabels = Object.freeze({
  instruction_override: "覆盖指令",
  secret_exfiltration: "索取秘密",
  tool_execution: "调用工具",
  financial_execution: "资金动作",
});

export const OFFICIAL_ATTESTATION_ACCESS_CODE_LABEL = "待匹配访问阻断码";
export const OFFICIAL_ATTESTATION_ACCESS_CODE_NOTE = "这些代码来自用户选择的 readiness 候选；确认后仅对新生成快照按代码、标的与官方 URL 精确匹配生效，不证明历史访问错误。";

function bytesToBase64(bytes) {
  let binary = "";
  const chunkSize = 32_768;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return window.btoa(binary);
}

export async function fileToMaterialPayload(file, title = "", materialId = "", metadata = {}) {
  if (!file) throw new Error("请先选择文件");
  if (file.size <= 0) throw new Error("文件内容为空");
  if (file.size > MAX_MATERIAL_FILE_BYTES) throw new Error("文件超过 2 MB 上限");
  const bytes = new Uint8Array(await file.arrayBuffer());
  return {
    material_id: materialId,
    title: title.trim(),
    filename: file.name,
    content_type: file.type || "application/octet-stream",
    content_base64: bytesToBase64(bytes),
    metadata,
  };
}

export function buildOfficialSupplementDraft(candidate = {}) {
  const symbol = String(candidate.symbol || "").trim().toUpperCase();
  const officialUrl = String(candidate.official_url || "").trim();
  const fiscalPeriod = String(candidate.fiscal_period || "").trim();
  const materialKind = String(candidate.material_kind || "").trim();
  const originalErrorCodes = [...new Set((Array.isArray(candidate.error_codes) ? candidate.error_codes : [])
    .map((code) => String(code || "").trim())
    .filter((code) => [
      "EARNINGS_MATERIAL_ACCESS_TIMEOUT",
      "EARNINGS_MATERIAL_ACCESS_ERROR",
    ].includes(code)))];
  if (
    !storageSymbols.has(symbol)
    || !officialUrl.startsWith("https://")
    || !fiscalPeriod
    || !materialKind
    || !originalErrorCodes.length
  ) {
    throw new Error("官方补证候选缺少有效标的、期间、类型、HTTPS 来源或可补证阻断码");
  }
  return {
    title: String(candidate.title || `${symbol.replace("US.", "")} ${fiscalPeriod} 官方业绩材料`).trim(),
    kind: "file_excerpt",
    source_url: "",
    content: "",
    metadata: {
      source_type: "company_ir",
      event_type: "earnings",
      publisher: String(candidate.publisher || "公司 Investor Relations").trim(),
      published_at: "",
      symbols: [symbol],
      fiscal_period: fiscalPeriod,
    },
    official_supplement_v1: {
      version: "official_supplement_v1",
      symbol,
      official_url: officialUrl,
      fiscal_period: fiscalPeriod,
      material_kind: materialKind,
      original_error_codes: originalErrorCodes,
      user_confirmed: false,
    },
  };
}

export function buildOfficialAttestationConfirmation(attestation = {}) {
  const source = attestation?.confirm_payload;
  if (!source || typeof source !== "object") return null;
  const attestationId = source.attestation_id;
  const expectedVersion = source.expected_version;
  const sourceSha256 = source.source_sha256;
  const contentSha256 = source.content_sha256;
  const materialSnapshotSha256 = source.material_snapshot_sha256;
  const sha256Pattern = /^[a-f0-9]{64}$/i;
  if (
    typeof attestationId !== "string"
    || !attestationId
    || attestationId.length > 200
    || !Number.isInteger(expectedVersion)
    || expectedVersion < 1
    || typeof sourceSha256 !== "string"
    || typeof contentSha256 !== "string"
    || typeof materialSnapshotSha256 !== "string"
    || !sha256Pattern.test(sourceSha256)
    || !sha256Pattern.test(contentSha256)
    || !sha256Pattern.test(materialSnapshotSha256)
  ) return null;
  return {
    attestation_id: attestationId,
    expected_version: expectedVersion,
    source_sha256: sourceSha256,
    content_sha256: contentSha256,
    material_snapshot_sha256: materialSnapshotSha256,
    user_confirmed: true,
  };
}

export function confirmedOfficialAttestationDialogState(result = {}) {
  const material = result?.material;
  const attestation = result?.official_attestation || material?._official_attestation;
  const confirmed = String(attestation?.status || "").toUpperCase() === "CONFIRMED"
    && String(attestation?.state || "").toLowerCase() === "confirmed";
  if (!material?.id || !attestation?.id || !confirmed) {
    throw new Error("服务端未返回已确认的官方补证记录；对话框保持打开，请重试或刷新房间。");
  }
  return {
    form: { ...material, official_attestation: attestation },
    officialAttestation: attestation,
    shouldClose: true,
  };
}

export function officialAttestationPreviewView(attestation = {}, material = {}, supplement = {}) {
  const preview = attestation?.preview && typeof attestation.preview === "object"
    ? attestation.preview
    : {};
  const file = preview.file && typeof preview.file === "object"
    ? preview.file
    : attestation?.file && typeof attestation.file === "object" ? attestation.file : {};
  const metadata = material?.metadata || {};
  const rawPageCount = attestation?.page_count
    ?? preview.page_count
    ?? file.page_count
    ?? metadata.page_count;
  const pageCount = Number(rawPageCount);
  const rawTruncated = attestation?.truncated
    ?? preview.truncated
    ?? file.truncated
    ?? metadata.truncated;
  const truncated = typeof rawTruncated === "boolean" ? rawTruncated : null;
  const accessCodes = [...new Set((Array.isArray(attestation?.original_error_codes)
    ? attestation.original_error_codes
    : Array.isArray(preview.original_error_codes)
      ? preview.original_error_codes
      : Array.isArray(supplement?.original_error_codes) ? supplement.original_error_codes : [])
    .map((code) => String(code || "").trim())
    .filter(Boolean))];
  return {
    symbol: String(attestation?.symbol || preview.symbol || supplement?.symbol || "未知标的"),
    fiscalPeriod: String(attestation?.fiscal_period || preview.fiscal_period || supplement?.fiscal_period || metadata.fiscal_period || "未知"),
    materialKind: String(attestation?.material_kind || preview.material_kind || supplement?.material_kind || "未知"),
    fileName: String(attestation?.file_name || file.filename || file.original_name || preview.file_name || preview.filename || metadata.original_name || material?.title || "未知文件"),
    sourceBytes: Number(attestation?.source_bytes ?? file.source_bytes ?? preview.source_bytes ?? metadata.source_bytes),
    contentType: String(attestation?.content_type || file.content_type || preview.content_type || metadata.content_type || ""),
    officialUrl: String(attestation?.official_url || preview.official_url || supplement?.official_url || ""),
    pageCount: Number.isInteger(pageCount) && pageCount > 0 ? pageCount : null,
    truncated,
    accessCodes,
    accessCodeLabel: OFFICIAL_ATTESTATION_ACCESS_CODE_LABEL,
    accessCodeNote: OFFICIAL_ATTESTATION_ACCESS_CODE_NOTE,
  };
}

export function materialSourceLabel(material) {
  const publisher = material.metadata?.publisher;
  const publishedAt = material.metadata?.published_at;
  const quarantine = materialPromptQuarantine(material);
  const suffix = [publisher, publishedAt, quarantine.quarantined ? "AI 已隔离" : ""]
    .filter(Boolean)
    .join(" · ");
  const withEvidence = (label) => suffix ? `${label} · ${suffix}` : label;
  if (material.kind === "url") return withEvidence("网页抓取");
  if (material.kind === "file_excerpt") return withEvidence("文件解析");
  return withEvidence("研究笔记");
}

export function materialPromptQuarantine(material = {}) {
  const risk = material?.metadata?.prompt_injection_risk;
  const rawFlags = Array.isArray(risk?.flags) ? risk.flags : [];
  const flags = [...new Set(rawFlags)]
    .filter((flag) => Object.hasOwn(promptRiskLabels, flag));
  return {
    quarantined: risk?.flagged === true && flags.length > 0,
    flags,
    labels: flags.map((flag) => promptRiskLabels[flag]),
  };
}
