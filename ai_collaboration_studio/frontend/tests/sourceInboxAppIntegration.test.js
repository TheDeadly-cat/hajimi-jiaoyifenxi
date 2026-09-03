import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");

test("App owns strict Source Inbox deep links without external URL navigation", () => {
  assert.match(appSource, /parseSourceInboxDeepLink\(globalThis\.location\)/);
  assert.match(appSource, /addEventListener\?\.\("popstate", syncSourceInboxDeepLink\)/);
  assert.match(appSource, /updateSourceInboxDeepLink\(\{ eventId: target, mode: historyMode \}\)/);
  assert.match(appSource, /updateSourceInboxDeepLink\(\{ eventId: "", mode: "replace" \}\)/);
  assert.match(appSource, /const navigateRail[\s\S]*clearSourceInboxTarget\(\);[\s\S]*setSourceInboxOpen\(false\)/);
  assert.match(appSource, /requestedItemId=\{sourceInboxEventId\}/);
  assert.match(appSource, /onEventTargetChange=\{\(eventId\) =>/);
  assert.doesNotMatch(appSource, /location\.(?:assign|replace)\(|window\.open\(/);
});

test("global unread polling baselines history and notifies only after explicit opt-in", () => {
  assert.match(appSource, /api\.sourceInboxNotifications\(\{/);
  assert.match(appSource, /feedState\.baselineReady \? feedState\.cursor : ""/);
  assert.match(appSource, /if \(!feedState\.baselineReady \|\| feed\.baseline\)/);
  assert.match(appSource, /feedState\.cursor = feed\.headCursor \|\| feed\.cursor/);
  assert.match(appSource, /if \(sourceInboxNotificationState\.enabled\) \{[\s\S]*createSourceInboxNotification\(\{/);
  assert.match(appSource, /requestSourceInboxNotificationPermissionFromUserGesture\(\)/);
  assert.match(appSource, /onNotificationPreferenceChange=\{changeSourceInboxNotificationPreference\}/);
  assert.match(appSource, /sourceInboxUnreadCount=\{sourceInboxUnreadCount\}/);
  assert.match(appSource, /screen-reader-announcer[\s\S]*sourceInboxAnnouncement/);
  assert.match(appSource, /sourceInboxOpenRef\.current/);
  assert.match(appSource, /sourceInboxAnnouncementSequenceRef\.current \+= 1/);
  assert.match(appSource, /sourceInboxVisibilityEpochRef\.current \+= 1/);
  assert.match(appSource, /sourceInboxVisibilityEpochRef\.current !== visibilityEpochAtRequest/);
  assert.match(appSource, /normalizeSourceInboxNotificationFeed\(payload, \{ requestedCursor \}\)/);
  assert.match(appSource, /sourceInboxOpenRef\.current[\s\S]*setSourceInboxRefreshToken/);
  assert.match(appSource, /refreshToken=\{sourceInboxRefreshToken\}/);

  const permissionRequestCount = (
    appSource.match(/requestSourceInboxNotificationPermissionFromUserGesture\(\)/g) || []
  ).length;
  assert.equal(permissionRequestCount, 1);
});

test("notification polling remains a read-only supplemental path", () => {
  const pollingBlock = appSource.slice(
    appSource.indexOf("const pollSourceInbox = async"),
    appSource.indexOf("const room = active?.room"),
  );
  assert.match(pollingBlock, /normalizeSourceInboxNotificationFeed/);
  assert.doesNotMatch(pollingBlock, /if \(sourceInboxOpen\)[\s\S]*return undefined/);
  assert.match(pollingBlock, /error\?\.name !== "AbortError"/);
  assert.doesNotMatch(
    pollingBlock,
    /streamRound|streamMessage|preflightProviders|storageSnapshot|\/orders|\/trades/,
  );
});
