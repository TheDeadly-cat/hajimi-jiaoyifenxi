import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { api } from "../src/api.js";
import {
  FOOTBALL_EVIDENCE_CLASSES,
  buildFootballRoundContextAuthorization,
  footballEvidenceClaims,
  footballResearchInspectorActivation,
  footballRoundContextAuthorizationState,
  normalizeFootballResearchResponse,
  parseFootballResearchJson,
} from "../src/footballResearch.js";

const HASH = "a".repeat(64);
const REGISTRY_HASH = "b".repeat(64);

function evidence(value, evidenceClass, suffix) {
  return {
    claim_id: `claim-${suffix}`,
    value,
    evidence_class: evidenceClass,
    as_of_utc: "2026-08-12T09:00:00Z",
    source: {
      source_id: `source-${suffix}`,
      publisher: `Publisher ${suffix}`,
      source_uri: `urn:ai-studio:material:material-${suffix}:v1`,
      source_sha256: HASH,
      material_binding: {
        material_id: `material-${suffix}`,
        material_version: 1,
        content_sha256: HASH,
        snapshot_sha256: HASH,
      },
      publication: evidenceClass === "odds_proxy" ? {
        state: "observed",
        published_at_utc: null,
        observed_at_utc: "2026-08-12T08:00:00Z",
      } : {
        state: evidenceClass === "model_inference" ? "not_published" : "published",
        published_at_utc: evidenceClass === "model_inference" ? null : "2026-08-12T08:00:00Z",
        observed_at_utc: null,
      },
      retrieved_at_utc: "2026-08-12T09:10:00Z",
    },
  };
}

function contractFixture() {
  return {
    version: "football_research_contract_v1",
    capability_pack_id: "football_research_readonly",
    match_identity: {
      competition: evidence("Fixture League", "official_fact", "competition"),
      competition_id: evidence("competition-fixture-league", "official_fact", "competition-id"),
      season: evidence("2026-27", "official_fact", "season"),
      match_id: evidence("match-001", "official_fact", "match"),
      kickoff_utc: evidence("2026-08-13T19:00:00Z", "official_fact", "kickoff"),
      venue_id: evidence("venue-001", "official_fact", "venue-id"),
      venue: evidence("Fixture Stadium", "official_fact", "venue"),
    },
    data_cutoff_utc: "2026-08-12T10:00:00Z",
    teams: {
      home: {
        team_name: evidence("Home", "official_fact", "home"),
        tactical_context: evidence(["Wide build-up"], "model_inference", "tactics"),
      },
      away: {
        team_name: evidence("Away", "official_fact", "away"),
        availability: {
          injuries: evidence({ publication_state: "published", entries: [] }, "media_report", "injuries"),
        },
      },
    },
    odds_proxies: [evidence({ market: "three_way_full_time", selection: "home", decimal_odds: 2.1 }, "odds_proxy", "odds")],
    probability_state: "withheld_no_calibration",
    future_probability_available: false,
    probability_metrics_visible: false,
    odds_are_proxy_only: true,
    execution_capability: "none",
    betting_allowed: false,
    live_betting_allowed: false,
    automatic_betting_allowed: false,
    wallet_connection_allowed: false,
    order_placement_allowed: false,
    can_autonomously_decide: false,
    can_replace_user_decision: false,
    user_final_decision_required: true,
    contract_sha256: HASH,
  };
}

function responseFixture() {
  const contract = contractFixture();
  return {
    ok: true,
    football_research: {
      version: "football_research_view_model_v1",
      integrity_ok: true,
      metrics_visible: false,
      room_id: "room/一",
      contract,
      contract_sha256: HASH,
      data_cutoff_utc: contract.data_cutoff_utc,
      probability_state: "withheld_no_calibration",
      future_probability_available: false,
      probability_metrics_visible: false,
      odds_are_proxy_only: true,
      provider_calls_performed: 0,
      market_reads_performed: 0,
      business_writes_performed: 0,
      execution_capability: "none",
      live_trading_allowed: false,
      betting_allowed: false,
      automatic_betting_allowed: false,
      wallet_connection_allowed: false,
      order_placement_allowed: false,
      can_autonomously_decide: false,
      can_replace_user_decision: false,
      user_final_decision_required: true,
    },
  };
}

test("football response stays closed, room-bound and exposes four distinct evidence classes", () => {
  const view = normalizeFootballResearchResponse(responseFixture(), "room/一");
  assert.equal(view.valid, true);
  assert.deepEqual(
    [...new Set(footballEvidenceClaims(view.contract).map((claim) => claim.evidenceClass))].sort(),
    [...FOOTBALL_EVIDENCE_CLASSES].sort(),
  );

  const probabilityDrift = structuredClone(responseFixture());
  probabilityDrift.football_research.future_probability_available = true;
  assert.equal(normalizeFootballResearchResponse(probabilityDrift, "room/一").valid, false);

  const extraField = structuredClone(responseFixture());
  extraField.football_research.win_probability = 0.51;
  assert.equal(normalizeFootballResearchResponse(extraField, "room/一").valid, false);
  assert.equal(normalizeFootballResearchResponse(responseFixture(), "room/二").valid, false);
});

test("local JSON parser accepts only a non-empty object", () => {
  assert.deepEqual(parseFootballResearchJson('{"match_identity":{}}'), { match_identity: {} });
  assert.throws(() => parseFootballResearchJson(""), /粘贴或导入/);
  assert.throws(() => parseFootballResearchJson("[]"), /顶层必须是对象/);
  assert.throws(() => parseFootballResearchJson("{"), /JSON 格式无效/);
});

test("round context authorization is created only by the explicit builder and drifts closed", () => {
  const view = normalizeFootballResearchResponse(responseFixture(), "room/一");
  const activation = {
    active: true,
    slot: { snapshotSha256: REGISTRY_HASH },
  };
  const authorization = buildFootballRoundContextAuthorization(view, activation);
  assert.equal(authorization.user_authorized, true);
  assert.equal(authorization.room_id, "room/一");
  assert.equal(authorization.match_id, "match-001");
  assert.equal(authorization.data_cutoff_utc, "2026-08-12T10:00:00Z");
  assert.equal(authorization.contract_sha256, HASH);
  assert.equal(authorization.plugin_registry_snapshot_sha256, REGISTRY_HASH);
  assert.notEqual(authorization.contract, view.contract);
  assert.equal(footballRoundContextAuthorizationState(authorization, {
    roomId: "room/一",
    contractSha256: HASH,
    pluginRegistrySnapshotSha256: REGISTRY_HASH,
  }).valid, true);
  assert.equal(footballRoundContextAuthorizationState(authorization, {
    roomId: "room/二",
    contractSha256: HASH,
    pluginRegistrySnapshotSha256: REGISTRY_HASH,
  }).valid, false);
  assert.equal(footballRoundContextAuthorizationState(authorization, {
    roomId: "room/一",
    contractSha256: HASH,
    pluginRegistrySnapshotSha256: "c".repeat(64),
  }).valid, false);
  assert.throws(
    () => buildFootballRoundContextAuthorization(view, { active: false, slot: {} }),
    /精确 v2 贡献/,
  );
});

test("football inspector API encodes the room and wraps only payload", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  const payload = { match_identity: { match_id: "match-001" } };
  const controller = new AbortController();
  globalThis.fetch = async (path, options) => {
    request = { path, options };
    return { ok: true, status: 200, json: async () => responseFixture() };
  };
  try {
    await api.inspectFootballResearch("room/一", payload, controller.signal);
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(request.path, "/api/rooms/room%2F%E4%B8%80/football-research/inspect");
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.signal, controller.signal);
  assert.deepEqual(JSON.parse(request.options.body), { payload });
});

test("host App activates the static inspector through the exact contribution helper", () => {
  const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const panelSource = readFileSync(
    new URL("../src/components/FootballResearchPanel.jsx", import.meta.url),
    "utf8",
  );
  const helperSource = readFileSync(new URL("../src/footballResearch.js", import.meta.url), "utf8");

  assert.match(appSource, /footballResearchInspectorActivation\(/);
  assert.match(appSource, /frozenContext: pendingRound \|\| room/);
  assert.match(appSource, /runtimeContext: room/);
  assert.match(appSource, /footballRoundContextAuthorization/);
  assert.match(appSource, /<FootballResearchPanel/);
  assert.match(helperSource, /slot\.snapshotVersion === "plugin_registry_snapshot_v2"/);
  assert.match(helperSource, /contribution\?\.contractVersion === "ui_contribution_contract_v2"/);
  assert.match(helperSource, /exactStringSet\(contribution\?\.allowedActions, \[FOOTBALL_RESEARCH_ACTION_ID\]\)/);

  assert.match(panelSource, /type="file" accept="\.json,application\/json"/);
  assert.match(panelSource, /future_probability_available=false/);
  assert.match(panelSource, /match\.competition_id/);
  assert.match(panelSource, /match\.competition/);
  assert.match(panelSource, /probability_metrics_visible=false/);
  assert.match(panelSource, /odds_are_proxy_only=true/);
  assert.match(panelSource, /显式用于下一轮/);
  assert.doesNotMatch(panelSource, /CandidateExperiment|createCandidateExperiment|股票模拟/);
});

test("formal launch requires explicit football authorization and clears every local drift", () => {
  const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const panelSource = readFileSync(
    new URL("../src/components/FootballResearchPanel.jsx", import.meta.url),
    "utf8",
  );
  const launchGateSource = appSource.slice(
    appSource.indexOf("const canAttemptNewRound"),
    appSource.indexOf("const roundBusy"),
  );
  const planningSource = appSource.slice(
    appSource.indexOf("const requestRoundLaunchPlan"),
    appSource.indexOf("const retryRoundLaunchPlan"),
  );
  const confirmationSource = appSource.slice(
    appSource.indexOf("const confirmRoundLaunch"),
    appSource.indexOf("const resumePausedRound"),
  );

  assert.match(
    launchGateSource,
    /!footballRoundContextRequired\s*\|\| activeFootballRoundContextAuthorization\.valid/,
  );
  assert.match(planningSource, /round_context_authorizations: context\.roundContextAuthorizations/);
  assert.doesNotMatch(planningSource, /project_round_focus_authorization/);
  assert.match(
    confirmationSource,
    /payload\?\.round_context_authorizations[\s\S]*context\.roundContextAuthorizations/,
  );
  assert.match(confirmationSource, /setFootballRoundContextAuthorization\(null\)/);
  assert.match(panelSource, /updateSource[\s\S]*onRoundContextAuthorizationChange\(null\)/);
  assert.match(panelSource, /const inspect[\s\S]*onRoundContextAuthorizationChange\(null\)/);
  assert.doesNotMatch(panelSource, /CandidateExperiment|createCandidateExperiment|候选实验/);
});

test("missing registry data never activates the football contribution", () => {
  const activation = footballResearchInspectorActivation({
    frozenContext: null,
    runtimeContext: null,
    pluginRegistry: null,
    pluginLifecycle: null,
  });
  assert.equal(activation.visible, false);
  assert.equal(activation.active, false);
});
