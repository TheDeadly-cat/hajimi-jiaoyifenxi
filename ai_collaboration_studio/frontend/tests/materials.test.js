import test from "node:test";
import assert from "node:assert/strict";
import {
  buildOfficialAttestationConfirmation,
  buildOfficialSupplementDraft,
  confirmedOfficialAttestationDialogState,
  materialPromptQuarantine,
  materialSourceLabel,
  officialAttestationPreviewView,
  OFFICIAL_ATTESTATION_ACCESS_CODE_LABEL,
  OFFICIAL_ATTESTATION_ACCESS_CODE_NOTE,
} from "../src/materials.js";

test("builds a locked official supplement draft from a server readiness candidate", () => {
  const draft = buildOfficialSupplementDraft({
    symbol: "US.WDC",
    title: "Third Quarter Fiscal 2026 Earnings Presentation",
    publisher: "Western Digital Investor Relations",
    official_url: "https://investor.wdc.com/static-files/official-file",
    fiscal_period: "FY2026-Q3",
    material_kind: "earnings_presentation",
    error_codes: [
      "EARNINGS_MATERIAL_ACCESS_TIMEOUT",
      "EARNINGS_MATERIAL_HUB_TIMEOUT",
      "EARNINGS_MATERIAL_ACCESS_TIMEOUT",
    ],
  });

  assert.equal(draft.kind, "file_excerpt");
  assert.deepEqual(draft.metadata.symbols, ["US.WDC"]);
  assert.equal(draft.metadata.source_type, "company_ir");
  assert.equal(draft.metadata.event_type, "earnings");
  assert.deepEqual(draft.official_supplement_v1, {
    version: "official_supplement_v1",
    symbol: "US.WDC",
    official_url: "https://investor.wdc.com/static-files/official-file",
    fiscal_period: "FY2026-Q3",
    material_kind: "earnings_presentation",
    original_error_codes: ["EARNINGS_MATERIAL_ACCESS_TIMEOUT"],
    user_confirmed: false,
  });
  assert.throws(
    () => buildOfficialSupplementDraft({
      symbol: "US.WDC",
      official_url: "https://investor.wdc.com/static-files/official-file",
      fiscal_period: "FY2026-Q3",
      material_kind: "earnings_presentation",
      error_codes: ["EARNINGS_MATERIAL_HUB_TIMEOUT"],
    }),
    /可补证阻断码/,
  );
});

test("copies only the server attestation confirmation whitelist", () => {
  const hashA = "a".repeat(64);
  const hashB = "b".repeat(64);
  const hashC = "c".repeat(64);
  const confirmation = buildOfficialAttestationConfirmation({
    confirm_payload: {
      attestation_id: "attestation_123",
      expected_version: 2,
      source_sha256: hashA,
      content_sha256: hashB,
      material_snapshot_sha256: hashC,
      user_confirmed: false,
      injected: "must-not-pass",
    },
  });

  assert.deepEqual(confirmation, {
    attestation_id: "attestation_123",
    expected_version: 2,
    source_sha256: hashA,
    content_sha256: hashB,
    material_snapshot_sha256: hashC,
    user_confirmed: true,
  });
  assert.equal(buildOfficialAttestationConfirmation({ confirm_payload: {
    attestation_id: "attestation_123",
    expected_version: 2,
    source_sha256: "derived-client-value",
    content_sha256: hashB,
    material_snapshot_sha256: hashC,
  } }), null);
});

test("a confirmed attestation response replaces staged dialog state and closes it", () => {
  const result = {
    material: { id: "material_sndk", version: 2, title: "SNDK FY2026-Q3" },
    official_attestation: {
      id: "attestation_sndk",
      status: "CONFIRMED",
      state: "confirmed",
      material_id: "material_sndk",
    },
  };

  const nextState = confirmedOfficialAttestationDialogState(result);
  assert.equal(nextState.form.id, "material_sndk");
  assert.equal(nextState.form.official_attestation.status, "CONFIRMED");
  assert.equal(nextState.officialAttestation.state, "confirmed");
  assert.equal(nextState.shouldClose, true);
  assert.throws(
    () => confirmedOfficialAttestationDialogState({
      ...result,
      official_attestation: { ...result.official_attestation, status: "STAGED", state: "staged" },
    }),
    /已确认/,
  );
});

test("official attestation preview trusts the server symbol and states extraction limits precisely", () => {
  const view = officialAttestationPreviewView({
    symbol: "US.SNDK",
    fiscal_period: "FY2026-Q3",
    material_kind: "earnings_presentation",
    page_count: 37,
    truncated: false,
    original_error_codes: ["EARNINGS_MATERIAL_ACCESS_TIMEOUT"],
    preview: {
      symbol: "US.FAKE",
      publisher: "Client supplied publisher",
      page_count: 1,
      truncated: true,
    },
  }, {
    title: "Client material",
    metadata: { publisher: "Client supplied publisher", page_count: 2, truncated: true },
  }, {
    symbol: "US.WDC",
    original_error_codes: ["EARNINGS_MATERIAL_ACCESS_ERROR"],
  });

  assert.equal(view.symbol, "US.SNDK");
  assert.equal(view.pageCount, 37);
  assert.equal(view.truncated, false);
  assert.deepEqual(view.accessCodes, ["EARNINGS_MATERIAL_ACCESS_TIMEOUT"]);
  assert.equal(view.accessCodeLabel, OFFICIAL_ATTESTATION_ACCESS_CODE_LABEL);
  assert.equal(view.accessCodeLabel, "待匹配访问阻断码");
  assert.equal(view.accessCodeNote, OFFICIAL_ATTESTATION_ACCESS_CODE_NOTE);
  assert.match(view.accessCodeNote, /仅对新生成快照/);
  assert.match(view.accessCodeNote, /不证明历史访问错误/);
  assert.doesNotMatch(view.symbol, /Client supplied publisher/);
  const missingExtractionState = officialAttestationPreviewView({ symbol: "US.WDC" });
  assert.equal(missingExtractionState.pageCount, null);
  assert.equal(missingExtractionState.truncated, null);
});

test("marks only recognized server risk flags as quarantined", () => {
  const material = {
    kind: "url",
    metadata: {
      publisher: "Official IR",
      prompt_injection_risk: {
        flagged: true,
        flags: ["tool_execution", "unknown", "tool_execution", "financial_execution"],
      },
    },
  };

  assert.deepEqual(materialPromptQuarantine(material), {
    quarantined: true,
    flags: ["tool_execution", "financial_execution"],
    labels: ["调用工具", "资金动作"],
  });
  assert.equal(materialSourceLabel(material), "网页抓取 · Official IR · AI 已隔离");
});

test("does not trust a flagged boolean without a recognized server flag", () => {
  const material = {
    kind: "note",
    metadata: {
      prompt_injection_risk: { flagged: true, flags: ["client_only_claim"] },
    },
  };

  assert.equal(materialPromptQuarantine(material).quarantined, false);
  assert.equal(materialSourceLabel(material), "研究笔记");
});
