import {
  ACTION_DESK_STATES,
  normalizeActionDeskItem,
} from "./actionDesk.js";

export const ACTION_DESK_OVERVIEW_VERSION = "artifact_action_desk_overview_v1";
export const ACTION_DESK_ROOM_SUMMARY_VERSION = "artifact_action_room_summary_v1";

const ROW_LIMIT = 500;
const ITEM_LIMIT = 2000;
const TEXT_LIMIT = 3000;
const ID_PATTERN = /^[A-Za-z0-9_-]{1,120}$/;
const STATE_SET = new Set(ACTION_DESK_STATES);

const OVERVIEW_KEYS = [
  "version",
  "integrity_ok",
  "rooms",
  "counts",
  "issues",
  "execution_capability",
  "external_write",
  "can_autonomously_decide",
  "can_replace_user_decision",
  "ranking_produced",
  "winner_claim",
  "user_final_decision_required",
];

const ROOM_KEYS = [
  "version",
  "room_id",
  "room_title",
  "integrity_ok",
  "items",
  "counts",
  "issues",
];

const ROOM_COUNT_KEYS = [
  "candidate_count",
  "item_count",
  "open_count",
  "in_progress_count",
  "blocked_count",
  "done_count",
  "cancelled_count",
];

const OVERVIEW_COUNT_KEYS = [
  "room_count",
  "healthy_room_count",
  "failed_room_count",
  ...ROOM_COUNT_KEYS,
];

const ISSUE_KEYS = ["code", "message"];
const ISSUE_CODE_SET = new Set([
  "ACTION_DESK_ORPHAN_ROOM_LINEAGE",
  "ACTION_DESK_ROOM_INTEGRITY_FAILED",
]);

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function exactKeys(value, expected) {
  const actual = Object.keys(record(value)).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length
    && actual.every((key, index) => key === wanted[index]);
}

function cleanText(value, limit = TEXT_LIMIT) {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized && normalized.length <= limit ? normalized : null;
}

function normalizedId(value) {
  const normalized = cleanText(value, 120);
  return normalized && ID_PATTERN.test(normalized) ? normalized : null;
}

function nonnegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function normalizeIssues(value) {
  if (!Array.isArray(value) || value.length > ROW_LIMIT) return null;
  const seen = new Set();
  const normalized = [];
  for (const rawValue of value) {
    const raw = record(rawValue);
    const code = cleanText(raw.code, 120);
    const message = cleanText(raw.message, TEXT_LIMIT);
    if (
      !exactKeys(raw, ISSUE_KEYS)
      || !code
      || !ISSUE_CODE_SET.has(code)
      || !message
      || seen.has(code)
    ) {
      return null;
    }
    seen.add(code);
    normalized.push({ code, message });
  }
  return normalized;
}

function normalizeCounts(value, keys) {
  const raw = record(value);
  if (!exactKeys(raw, keys)) return null;
  const result = {};
  for (const key of keys) {
    const normalized = nonnegativeInteger(raw[key]);
    if (normalized === null) return null;
    result[key] = normalized;
  }
  return result;
}

function roomCountsMatchItems(counts, items) {
  if (!counts || counts.item_count !== items.length) return false;
  const states = Object.fromEntries(ACTION_DESK_STATES.map((state) => [state, 0]));
  for (const item of items) states[item.state] += 1;
  return counts.open_count === states.open
    && counts.in_progress_count === states.in_progress
    && counts.blocked_count === states.blocked
    && counts.done_count === states.done
    && counts.cancelled_count === states.cancelled
    && counts.item_count === (
      counts.open_count
      + counts.in_progress_count
      + counts.blocked_count
      + counts.done_count
      + counts.cancelled_count
    );
}

function emptyOverview(issues) {
  return {
    valid: false,
    integrityOk: false,
    metricsVisible: false,
    countsVisible: false,
    rooms: [],
    failedRooms: [],
    items: [],
    counts: null,
    issues: [...new Set(issues)],
  };
}

function publicCounts(raw) {
  return {
    roomCount: raw.room_count,
    healthyRoomCount: raw.healthy_room_count,
    failedRoomCount: raw.failed_room_count,
    candidateCount: raw.candidate_count,
    itemCount: raw.item_count,
    openCount: raw.open_count,
    inProgressCount: raw.in_progress_count,
    blockedCount: raw.blocked_count,
    doneCount: raw.done_count,
    cancelledCount: raw.cancelled_count,
  };
}

function aggregateHealthyRooms(rooms) {
  const totals = Object.fromEntries(OVERVIEW_COUNT_KEYS.map((key) => [key, 0]));
  totals.room_count = rooms.length;
  for (const room of rooms) {
    if (!room.integrityOk) {
      totals.failed_room_count += 1;
      continue;
    }
    totals.healthy_room_count += 1;
    for (const key of ROOM_COUNT_KEYS) totals[key] += room.rawCounts[key];
  }
  return totals;
}

function countsEqual(left, right, keys) {
  return keys.every((key) => left?.[key] === right?.[key]);
}

export function normalizeActionDeskOverviewResponse(payload) {
  const envelope = record(payload);
  const raw = record(envelope.action_desk_overview);
  const fatalIssues = [];
  if (!exactKeys(envelope, ["ok", "action_desk_overview"]) || envelope.ok !== true) {
    fatalIssues.push("RESPONSE_NOT_OK");
  }
  if (!exactKeys(raw, OVERVIEW_KEYS)) fatalIssues.push("OVERVIEW_SHAPE_INVALID");
  if (raw.version !== ACTION_DESK_OVERVIEW_VERSION) fatalIssues.push("OVERVIEW_VERSION_INVALID");
  if (raw.integrity_ok !== true && raw.integrity_ok !== false) {
    fatalIssues.push("OVERVIEW_INTEGRITY_STATE_INVALID");
  }
  if (
    raw.execution_capability !== "none"
    || raw.external_write !== false
    || raw.can_autonomously_decide !== false
    || raw.can_replace_user_decision !== false
    || raw.ranking_produced !== false
    || raw.winner_claim !== false
    || raw.user_final_decision_required !== true
  ) {
    fatalIssues.push("SAFETY_BOUNDARY_DRIFT");
  }
  const topIssues = normalizeIssues(raw.issues);
  if (!topIssues) fatalIssues.push("ISSUES_INVALID");
  const declaredCounts = normalizeCounts(raw.counts, OVERVIEW_COUNT_KEYS);
  if (!declaredCounts) fatalIssues.push("COUNTS_INVALID");
  if (!Array.isArray(raw.rooms) || raw.rooms.length > ROW_LIMIT) fatalIssues.push("ROOMS_INVALID");
  if (fatalIssues.length) return emptyOverview(fatalIssues);

  const rooms = [];
  const roomIds = new Set();
  const globalItemKeys = new Set();
  let totalItems = 0;
  for (let roomIndex = 0; roomIndex < raw.rooms.length; roomIndex += 1) {
    const roomRaw = record(raw.rooms[roomIndex]);
    if (!exactKeys(roomRaw, ROOM_KEYS) || roomRaw.version !== ACTION_DESK_ROOM_SUMMARY_VERSION) {
      fatalIssues.push(`ROOM_SHAPE_INVALID:${roomIndex}`);
      continue;
    }
    const roomId = normalizedId(roomRaw.room_id);
    const roomTitle = cleanText(roomRaw.room_title, 500);
    const roomIssues = normalizeIssues(roomRaw.issues);
    const roomCounts = normalizeCounts(roomRaw.counts, ROOM_COUNT_KEYS);
    if (
      !roomId
      || !roomTitle
      || roomIds.has(roomId)
      || (roomRaw.integrity_ok !== true && roomRaw.integrity_ok !== false)
      || !roomIssues
      || !roomCounts
      || !Array.isArray(roomRaw.items)
      || roomRaw.items.length > ROW_LIMIT
    ) {
      fatalIssues.push(`ROOM_CONTENT_INVALID:${roomIndex}`);
      continue;
    }
    roomIds.add(roomId);
    totalItems += roomRaw.items.length;
    if (totalItems > ITEM_LIMIT) {
      fatalIssues.push("ITEM_LIMIT_EXCEEDED");
      continue;
    }

    if (!roomRaw.integrity_ok) {
      const zeroCounts = ROOM_COUNT_KEYS.every((key) => roomCounts[key] === 0);
      if (
        roomRaw.items.length !== 0
        || !zeroCounts
        || roomIssues.length !== 1
        || roomIssues[0].code !== "ACTION_DESK_ROOM_INTEGRITY_FAILED"
      ) {
        fatalIssues.push(`FAILED_ROOM_NOT_REDACTED:${roomIndex}`);
        continue;
      }
      rooms.push({
        valid: true,
        integrityOk: false,
        roomId,
        roomTitle,
        items: [],
        rawCounts: roomCounts,
        issueCount: roomIssues.length,
      });
      continue;
    }

    const items = roomRaw.items.map((value, itemIndex) => {
      const item = normalizeActionDeskItem(value, itemIndex);
      return {
        ...item,
        roomId,
        roomTitle,
        overviewKey: `${roomId}:${item.sourceKey}`,
      };
    });
    if (
      roomIssues.length !== 0
      || !items.every((item) => item.valid)
      || !roomCountsMatchItems(roomCounts, items)
    ) {
      fatalIssues.push(`HEALTHY_ROOM_ITEMS_INVALID:${roomIndex}`);
      continue;
    }
    for (const item of items) {
      if (globalItemKeys.has(item.overviewKey)) {
        fatalIssues.push(`ITEM_DUPLICATED:${roomIndex}:${item.index}`);
      }
      globalItemKeys.add(item.overviewKey);
    }
    rooms.push({
      valid: true,
      integrityOk: true,
      roomId,
      roomTitle,
      items,
      rawCounts: roomCounts,
      issueCount: roomIssues.length,
    });
  }

  if (rooms.length !== raw.rooms.length) fatalIssues.push("ROOM_SET_INCOMPLETE");
  const computedCounts = aggregateHealthyRooms(rooms);
  if (!countsEqual(declaredCounts, computedCounts, OVERVIEW_COUNT_KEYS)) {
    fatalIssues.push("AGGREGATE_COUNTS_MISMATCH");
  }
  const failedRooms = rooms.filter((room) => !room.integrityOk);
  if (
    (raw.integrity_ok === true && topIssues.length !== 0)
    || (raw.integrity_ok === false && topIssues.length === 0)
    || (
      failedRooms.length > 0
      && !topIssues.some((issue) => issue.code === "ACTION_DESK_ROOM_INTEGRITY_FAILED")
    )
  ) {
    fatalIssues.push("OVERVIEW_INTEGRITY_INCONSISTENT");
  }
  if (fatalIssues.length) return emptyOverview(fatalIssues);

  return {
    valid: true,
    integrityOk: raw.integrity_ok,
    metricsVisible: true,
    countsVisible: true,
    rooms,
    failedRooms,
    items: rooms.flatMap((room) => room.items),
    counts: publicCounts(declaredCounts),
    issues: topIssues.map((issue) => issue.code),
  };
}

function searchableText(item) {
  return [
    item.roomTitle,
    item.text,
    item.owner,
    item.due,
    item.note,
    item.artifactTitle,
    item.artifactId,
    item.actionId,
  ].join("\n").toLocaleLowerCase("zh-CN");
}

export function filterActionDeskOverviewItems(items, { state = "all", query = "" } = {}) {
  const normalizedState = state === "all" || STATE_SET.has(state) ? state : "all";
  const normalizedQuery = String(query || "").trim().slice(0, 200).toLocaleLowerCase("zh-CN");
  return (Array.isArray(items) ? items : []).filter((item) => (
    item?.valid
    && (normalizedState === "all" || item.state === normalizedState)
    && (!normalizedQuery || searchableText(item).includes(normalizedQuery))
  ));
}
