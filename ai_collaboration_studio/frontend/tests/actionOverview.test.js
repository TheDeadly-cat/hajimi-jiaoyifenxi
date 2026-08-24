import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  ACTION_DESK_OVERVIEW_VERSION,
  ACTION_DESK_ROOM_SUMMARY_VERSION,
  filterActionDeskOverviewItems,
  normalizeActionDeskOverviewResponse,
} from "../src/actionOverview.js";
import { ACTION_DESK_ITEM_VERSION } from "../src/actionDesk.js";

function issue(code = "ACTION_DESK_ROOM_INTEGRITY_FAILED") {
  return {
    code,
    message: code === "ACTION_DESK_ORPHAN_ROOM_LINEAGE"
      ? "Action Desk lineage exists outside the verified room index and was hidden."
      : "The room action desk failed integrity verification.",
  };
}

function item(overrides = {}) {
  return {
    version: ACTION_DESK_ITEM_VERSION,
    artifact_id: "artifact_exact",
    artifact_version: 4,
    artifact_title: "确认项目纪要",
    action_id: "action_adopted",
    action_snapshot_sha256: "a".repeat(64),
    text: "复核供应商容量约束",
    owner: "项目负责人",
    due: "本周五",
    state: "blocked",
    evidence_count: 1,
    source_status: "confirmed_exact",
    revision: 2,
    note: "等待供应商补件",
    latest_event_id: "action_event_2",
    latest_event_sha256: "b".repeat(64),
    adopted_at: 1786320000000,
    updated_at: 1786323600000,
    source_current: true,
    current_artifact_version: 4,
    integrity_ok: true,
    ...overrides,
  };
}

function roomCounts(overrides = {}) {
  return {
    candidate_count: 1,
    item_count: 2,
    open_count: 1,
    in_progress_count: 0,
    blocked_count: 1,
    done_count: 0,
    cancelled_count: 0,
    ...overrides,
  };
}

function responseFixture() {
  const healthyItems = [
    item(),
    item({
      action_id: "action_open",
      action_snapshot_sha256: "c".repeat(64),
      text: "安排用户验收窗口",
      owner: "验收负责人",
      state: "open",
      note: "",
      latest_event_id: "action_event_3",
      latest_event_sha256: "d".repeat(64),
      revision: 1,
    }),
  ];
  return {
    ok: true,
    action_desk_overview: {
      version: ACTION_DESK_OVERVIEW_VERSION,
      integrity_ok: false,
      rooms: [
        {
          version: ACTION_DESK_ROOM_SUMMARY_VERSION,
          room_id: "room_healthy",
          room_title: "交付准备会",
          integrity_ok: true,
          items: healthyItems,
          counts: roomCounts(),
          issues: [],
        },
        {
          version: ACTION_DESK_ROOM_SUMMARY_VERSION,
          room_id: "room_failed",
          room_title: "风险复核会",
          integrity_ok: false,
          items: [],
          counts: roomCounts({
            candidate_count: 0,
            item_count: 0,
            open_count: 0,
            blocked_count: 0,
          }),
          issues: [issue()],
        },
      ],
      counts: {
        room_count: 2,
        healthy_room_count: 1,
        failed_room_count: 1,
        candidate_count: 1,
        item_count: 2,
        open_count: 1,
        in_progress_count: 0,
        blocked_count: 1,
        done_count: 0,
        cancelled_count: 0,
      },
      issues: [issue()],
      execution_capability: "none",
      external_write: false,
      can_autonomously_decide: false,
      can_replace_user_decision: false,
      ranking_produced: false,
      winner_claim: false,
      user_final_decision_required: true,
    },
  };
}

test("shared v1 fixture remains accepted by the frontend parser", () => {
  const fixture = JSON.parse(readFileSync(
    new URL("../../tests/fixtures/action_desk_overview_v1.json", import.meta.url),
    "utf8",
  ));
  const normalized = normalizeActionDeskOverviewResponse(fixture);
  assert.equal(normalized.valid, true);
  assert.equal(normalized.integrityOk, true);
  assert.deepEqual(
    normalized.rooms.map((room) => room.roomId),
    ["room_sports", "room_plan", "room_storage", "room_market", "room_project"],
  );
  assert.deepEqual(normalized.counts, {
    roomCount: 5,
    healthyRoomCount: 5,
    failedRoomCount: 0,
    candidateCount: 0,
    itemCount: 0,
    openCount: 0,
    inProgressCount: 0,
    blockedCount: 0,
    doneCount: 0,
    cancelledCount: 0,
  });
});

test("partial room isolation keeps healthy adopted actions while redacting failed rooms", () => {
  const overview = normalizeActionDeskOverviewResponse(responseFixture());

  assert.equal(overview.valid, true);
  assert.equal(overview.integrityOk, false);
  assert.equal(overview.metricsVisible, true);
  assert.equal(overview.items.length, 2);
  assert.equal(overview.items[0].roomId, "room_healthy");
  assert.equal(overview.failedRooms.length, 1);
  assert.equal(overview.failedRooms[0].roomTitle, "风险复核会");
  assert.deepEqual(overview.failedRooms[0].items, []);
  assert.equal(overview.counts.itemCount, 2);
  assert.equal(overview.counts.failedRoomCount, 1);
});

test("top integrity false remains readable for a global issue without a failed room", () => {
  const response = responseFixture();
  response.action_desk_overview.rooms = [response.action_desk_overview.rooms[0]];
  response.action_desk_overview.counts.room_count = 1;
  response.action_desk_overview.counts.failed_room_count = 0;
  response.action_desk_overview.issues = [issue("ACTION_DESK_ORPHAN_ROOM_LINEAGE")];

  const overview = normalizeActionDeskOverviewResponse(response);

  assert.equal(overview.valid, true);
  assert.equal(overview.integrityOk, false);
  assert.equal(overview.failedRooms.length, 0);
  assert.equal(overview.items.length, 2);
});

test("envelope, fixed safety, and aggregate drift fail the whole overview closed", () => {
  const mutations = [
    (value) => { value.action_desk_overview.ranking_produced = true; },
    (value) => { value.action_desk_overview.winner_claim = true; },
    (value) => { value.action_desk_overview.external_write = true; },
    (value) => { value.action_desk_overview.counts.item_count = 99; },
    (value) => { value.action_desk_overview.unexpected = true; },
    (value) => { value.unexpected = true; },
  ];

  for (const mutate of mutations) {
    const response = responseFixture();
    mutate(response);
    const overview = normalizeActionDeskOverviewResponse(response);
    assert.equal(overview.valid, false);
    assert.equal(overview.metricsVisible, false);
    assert.deepEqual(overview.items, []);
    assert.deepEqual(overview.failedRooms, []);
    assert.equal(overview.counts, null);
  }
});

test("a failed room must contain no action rows or nonzero inferred counts", () => {
  const leakedItem = responseFixture();
  leakedItem.action_desk_overview.rooms[1].items = [item()];
  const leakedCounts = responseFixture();
  leakedCounts.action_desk_overview.rooms[1].counts.item_count = 1;

  for (const response of [leakedItem, leakedCounts]) {
    const overview = normalizeActionDeskOverviewResponse(response);
    assert.equal(overview.valid, false);
    assert.deepEqual(overview.rooms, []);
    assert.deepEqual(overview.items, []);
  }
});

test("a structurally invalid item in a declared healthy room fails the overview closed", () => {
  const response = responseFixture();
  response.action_desk_overview.rooms[0].items[0].source_status = "latest";

  const overview = normalizeActionDeskOverviewResponse(response);

  assert.equal(overview.valid, false);
  assert.deepEqual(overview.items, []);
  assert.equal(overview.counts, null);
});

test("local status and text filters preserve backend order without producing a ranking", () => {
  const overview = normalizeActionDeskOverviewResponse(responseFixture());

  assert.deepEqual(
    filterActionDeskOverviewItems(overview.items, { state: "all", query: "" })
      .map((row) => row.actionId),
    ["action_adopted", "action_open"],
  );
  assert.deepEqual(
    filterActionDeskOverviewItems(overview.items, { state: "open", query: "验收负责人" })
      .map((row) => row.actionId),
    ["action_open"],
  );
  assert.deepEqual(
    filterActionDeskOverviewItems(overview.items, { state: "blocked", query: "确认项目纪要" })
      .map((row) => row.actionId),
    ["action_adopted"],
  );
});

test("drawer integration is read-only, abortable, and opens the exact room only after a successful load", () => {
  const drawerSource = readFileSync(
    new URL("../src/components/ActionOverviewDrawer.jsx", import.meta.url),
    "utf8",
  );
  const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const stylesSource = readFileSync(
    new URL("../src/styles/action-overview.css", import.meta.url),
    "utf8",
  );

  assert.match(drawerSource, /new AbortController\(\)/);
  assert.match(drawerSource, /controller\?\.abort\(\)/);
  assert.match(drawerSource, /await api\.actionDeskOverview\(controller\.signal\)/);
  assert.doesNotMatch(drawerSource, /transitionActionDesk|startRound|createArtifactUserDecision/);
  const navigation = appSource.slice(
    appSource.indexOf("const openActionOverviewRoom"),
    appSource.indexOf("const selectRoom"),
  );
  assert.match(navigation, /const loaded = await loadRoom\(targetRoomId\)/);
  assert.match(navigation, /if \(!loaded \|\| activeRoomIdRef\.current !== targetRoomId\) return false/);
  assert.match(navigation, /setActionOverviewOpen\(false\)/);
  assert.match(navigation, /inspector-action-desk/);
  assert.match(navigation, /setInspectorNavigation/);
  assert.doesNotMatch(navigation, /requestAnimationFrame/);
  assert.match(stylesSource, /\.action-overview-item\s*\{[^}]*content-visibility: auto/s);
});

test("exact-room navigation delegates late lazy alignment to the bounded DOM binder", () => {
  const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const inspectorSource = readFileSync(
    new URL("../src/components/RoomInspector.jsx", import.meta.url),
    "utf8",
  );
  const navigationSource = readFileSync(
    new URL("../src/inspectorTargetNavigation.js", import.meta.url),
    "utf8",
  );
  const deskSource = readFileSync(
    new URL("../src/components/ActionDeskPanel.jsx", import.meta.url),
    "utf8",
  );
  const stylesSource = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

  assert.match(appSource, /scrollTargetId=\{inspectorNavigation\.targetId\}/);
  assert.match(appSource, /scrollRequestId=\{inspectorNavigation\.requestId\}/);
  assert.match(inspectorSource, /bindInspectorTargetNavigation\(inspectorRef\.current, scrollTargetId\)/);
  assert.match(navigationSource, /new ResizeObserverClass\(align\)/);
  assert.match(navigationSource, /new MutationObserverClass\(\(\) => \{/);
  assert.match(navigationSource, /observeChildren\(\);\s*align\(\)/);
  assert.match(navigationSource, /mutationObserver\.observe\(inspector, \{ childList: true, subtree: true \}\)/);
  assert.match(navigationSource, /inspector\.scrollTop \+= offset/);
  assert.doesNotMatch(navigationSource, /headerOffset/);
  assert.match(navigationSource, /INSPECTOR_TARGET_NAVIGATION_LIFETIME_MS = 4000/);
  assert.match(navigationSource, /resolvedTarget\.focus\(\{ preventScroll: true \}\)/);
  assert.match(inspectorSource, /id="inspector-paper-portfolio" tabIndex=\{-1\}/);
  assert.match(inspectorSource, /id="inspector-observations" tabIndex=\{-1\}/);
  assert.match(deskSource, /id="inspector-action-desk"[^>]*tabIndex=\{-1\}/);
  assert.match(stylesSource, /\.action-desk-panel:focus\s*\{[^}]*outline: 2px solid #78aaf0/s);
});

test("drawer mounts exact action identities through a bounded progressive window", () => {
  const drawerSource = readFileSync(
    new URL("../src/components/ActionOverviewDrawer.jsx", import.meta.url),
    "utf8",
  );
  const stylesSource = readFileSync(
    new URL("../src/styles/action-overview.css", import.meta.url),
    "utf8",
  );

  assert.match(drawerSource, /const ACTION_OVERVIEW_BATCH_SIZE = 6/);
  assert.match(drawerSource, /useState\(ACTION_OVERVIEW_BATCH_SIZE\)/);
  assert.match(drawerSource, /const mountedItems = visibleItems\.slice\(0, visibleItemLimit\)/);
  assert.match(drawerSource, /mountedItems\.map\(\(item\) =>/);
  assert.match(drawerSource, /key=\{item\.overviewKey\}/);
  assert.doesNotMatch(drawerSource, /key=\{JSON\.stringify\(\[item\.overviewKey, itemIndex\]\)\}/);
  assert.match(drawerSource, /JSON\.stringify\(\["warning", failedRoom\.roomId\]\)/);
  assert.doesNotMatch(drawerSource, /failedRoomIndex/);
  assert.match(drawerSource, /aria-controls=\{overviewListId\}/);
  assert.match(drawerSource, /current \+ ACTION_OVERVIEW_BATCH_SIZE/);
  assert.match(drawerSource, /setVisibleItemLimit\(ACTION_OVERVIEW_BATCH_SIZE\)/);
  assert.match(stylesSource, /\.action-overview-progress\s*\{[^}]*grid-template-columns: minmax\(0, 1fr\) auto/s);
  assert.match(stylesSource, /\.action-overview-progress > button\s*\{[^}]*min-height: 44px/s);
  assert.match(stylesSource, /@media \(max-width: 520px\)[\s\S]*\.action-overview-progress\s*\{[^}]*grid-template-columns: minmax\(0, 1fr\)/);
});
