import assert from "node:assert/strict";
import test from "node:test";

import {
  deriveOfficialSupplementCandidates,
  deriveStorageReadinessView,
  mergePreparedResearchEvidence,
  storageCoverageText,
  storageSourceStateLabel,
} from "../src/storageReadiness.js";

test("extracts only exact rejected official earnings candidates for manual supplementation", () => {
  const sharedCandidate = {
    symbol: "US.SNDK",
    title: "Q3FY26 Earnings Presentation",
    official_url: "https://investor.sandisk.com/static-files/official-file",
    fiscal_period: "FY2026-Q3",
    material_kind: "earnings_presentation",
  };
  const candidates = deriveOfficialSupplementCandidates({
    independent_evidence: {
      evidence: {
        official_earnings_materials: {
          rows: [
            {
              symbol: "US.SNDK",
              publisher: "Sandisk Investor Relations",
              source_errors: [
                { code: "EARNINGS_MATERIAL_HUB_TIMEOUT" },
                { code: "EARNINGS_MATERIAL_ACCESS_TIMEOUT" },
              ],
              rejected_curated_materials: [sharedCandidate, sharedCandidate],
            },
            {
              symbol: "US.WDC",
              publisher: "Western Digital Investor Relations",
              source_errors: [{ code: "EARNINGS_MATERIAL_ACCESS_ERROR" }],
              rejected_curated_materials: [{
                symbol: "US.WDC",
                title: "Third Quarter Fiscal 2026 Earnings Presentation",
                official_url: "https://investor.wdc.com/static-files/official-file",
                fiscal_period: "FY2026-Q3",
                material_kind: "earnings_presentation",
              }],
            },
            {
              symbol: "US.BAD",
              rejected_curated_materials: [{ ...sharedCandidate, symbol: "US.BAD" }],
            },
          ],
        },
      },
    },
  });

  assert.equal(candidates.length, 2);
  assert.deepEqual(candidates.map((candidate) => candidate.symbol), ["US.SNDK", "US.WDC"]);
  assert.deepEqual(candidates[0].error_codes, ["EARNINGS_MATERIAL_ACCESS_TIMEOUT"]);
  assert.deepEqual(candidates[1].error_codes, ["EARNINGS_MATERIAL_ACCESS_ERROR"]);
  assert.deepEqual(deriveOfficialSupplementCandidates({
    independent_evidence: { evidence: { official_earnings_materials: { rows: [{
      symbol: "US.SNDK",
      source_errors: [{ code: "EARNINGS_MATERIAL_HUB_TIMEOUT" }],
      rejected_curated_materials: [sharedCandidate],
    }] } } },
  }), []);
});

test("connection status stays honest before public sources are fetched", () => {
  const view = deriveStorageReadinessView({
    sdk_available: true,
    opend_reachable: false,
    host: "127.0.0.1",
    port: 11111,
    sec_edgar: { configured: false },
  }, null);

  assert.equal(view.checked, false);
  assert.equal(view.sources.find((source) => source.id === "futu_sdk").state, "ready");
  assert.equal(view.sources.find((source) => source.id === "futu_opend").state, "blocked");
  assert.equal(view.sources.find((source) => source.id === "company_ir").state, "unchecked");
  assert.match(
    view.sources.find((source) => source.id === "sec_edgar").action,
    /SEC_USER_AGENT/,
  );
});

test("strict current market gate reconciles Futu admission before public evidence refresh", () => {
  const view = deriveStorageReadinessView({
    sdk_available: true,
    opend_reachable: false,
    sec_edgar: { configured: false },
  }, null, {
    required: true,
    ready: true,
    state: "ready",
    readyCount: 4,
    snapshotId: "futu_current",
    capturedAt: "2026-08-02T00:57:03.295Z",
  });

  const futu = view.sources.find((source) => source.id === "futu_opend");
  assert.equal(view.checked, false);
  assert.equal(view.roundAdmission.ready, true);
  assert.equal(view.roundAdmission.snapshot_id, "futu_current");
  assert.equal(futu.state, "ready");
  assert.equal(futu.ready, true);
  assert.equal(storageCoverageText(futu), "4/4");
  assert.ok(!view.convergence.blockers.some((blocker) => blocker.source_id === "futu_opend"));
  assert.equal(view.convergence.ready, false);
});

test("strict current market gate overrides stale readiness in both directions", () => {
  const staleReadiness = {
    version: "storage_research_readiness_v1",
    round_admission: { ready: true, coverage_ready: 4, coverage_total: 4 },
    convergence_readiness: { ready: false, blockers: [] },
    sources: [
      { id: "futu_opend", group: "round_admission", state: "ready", ready: true, coverage_ready: 4, coverage_total: 4 },
      { id: "company_ir", group: "convergence", state: "ready", ready: true, coverage_ready: 4, coverage_total: 4 },
    ],
  };
  const view = deriveStorageReadinessView({}, staleReadiness, {
    required: true,
    ready: false,
    state: "offline",
    readyCount: 0,
    reason: "当前 OpenD 快照不可用。",
    code: "FUTU_OPEND_OFFLINE",
  });

  const futu = view.sources.find((source) => source.id === "futu_opend");
  assert.equal(view.roundAdmission.ready, false);
  assert.equal(view.roundAdmission.reason_code, "FUTU_OPEND_OFFLINE");
  assert.equal(futu.state, "blocked");
  assert.equal(futu.ready, false);
  assert.ok(view.convergence.blockers.some((blocker) => blocker.source_id === "futu_opend"));
});

test("verified readiness keeps limited earnings materials distinct from ready", () => {
  const view = deriveStorageReadinessView({}, {
    version: "storage_research_readiness_v1",
    round_admission: { ready: false, coverage_ready: 0, coverage_total: 4 },
    convergence_readiness: {
      ready: false,
      preparation_usable: true,
      blockers: [{ source_id: "sec_edgar", label: "SEC", action: "配置" }],
    },
    safety: { ready: true },
    sources: [
      { id: "company_ir", group: "convergence", state: "ready", ready: true, coverage_ready: 4, coverage_total: 4 },
      { id: "earnings_materials", group: "convergence", state: "partial", ready: false, coverage_ready: 4, coverage_total: 4, action: "核验官方材料" },
      { id: "futu_opend", group: "round_admission", state: "blocked", ready: false, coverage_ready: 0, coverage_total: 4, action: "启动 OpenD" },
      { id: "sec_edgar", group: "convergence", state: "blocked", ready: false, coverage_ready: 0, coverage_total: 4, action: "刷新 SEC" },
    ],
  });

  assert.equal(view.checked, true);
  assert.equal(view.readyCount, 1);
  assert.equal(view.partialCount, 1);
  assert.equal(view.convergence.preparation_usable, true);
  assert.deepEqual(
    view.convergence.blockers.map((blocker) => blocker.source_id),
    ["earnings_materials", "futu_opend", "sec_edgar"],
  );
  assert.equal(
    view.convergence.blockers.find((blocker) => blocker.source_id === "earnings_materials").action,
    "核验官方材料",
  );
  assert.equal(storageSourceStateLabel(view.sources[1]), "部分可用");
});

test("unchecked convergence sources remain visible as blockers before refresh", () => {
  const view = deriveStorageReadinessView({
    sdk_available: true,
    opend_reachable: true,
    sec_edgar: { configured: true },
  }, null);

  assert.deepEqual(
    view.convergence.blockers.map((blocker) => blocker.source_id),
    ["futu_opend", "sec_edgar", "company_ir", "earnings_materials", "industry_proxies"],
  );
  assert.equal(
    view.sources.find((source) => source.id === "earnings_materials").group,
    "convergence",
  );
});

test("coverage text does not turn an unavailable count into a misleading zero of zero", () => {
  assert.equal(storageCoverageText({ coverage_ready: null, coverage_total: null }), "—");
  assert.equal(storageCoverageText({ coverage_ready: 4, coverage_total: 4 }), "4/4");
  assert.equal(storageCoverageText({ coverage_ready: "5", coverage_total: "5" }), "5/5");
});

test("manual official substitution stays explicitly amber instead of becoming unknown or normal ready", () => {
  const source = {
    id: "earnings_materials",
    state: "ready_with_manual_substitution",
    ready: true,
    coverage_ready: 4,
    coverage_total: 4,
  };
  const view = deriveStorageReadinessView({}, {
    version: "storage_research_readiness_v1",
    round_admission: { ready: true },
    convergence_readiness: { ready: true, blockers: [] },
    sources: [source],
  });

  assert.equal(view.sources[0].state, "ready_with_manual_substitution");
  assert.equal(view.readyCount, 1);
  assert.equal(storageSourceStateLabel(view.sources[0]), "人工核验副本");
  assert.notEqual(storageSourceStateLabel(view.sources[0]), "已就绪");
});

test("prepared official evidence replaces only skipped live sources", () => {
  const merged = mergePreparedResearchEvidence({
    state: "offline",
    company_ir_releases: { state: "skipped", rows: [] },
    official_filings: { state: "ready", rows: [{ symbol: "US.MU", filings: [{}] }] },
    technical: { rows: [] },
  }, {
    company_ir_releases: { state: "ready", rows: [{ symbol: "US.MU", releases: [{}] }] },
    official_filings: { state: "partial", rows: [{ symbol: "US.WDC", filings: [{}] }] },
  });

  assert.equal(merged.state, "offline");
  assert.equal(merged.company_ir_releases.rows[0].symbol, "US.MU");
  assert.equal(merged.official_filings.rows[0].symbol, "US.MU");
  assert.deepEqual(merged.technical, { rows: [] });
});
