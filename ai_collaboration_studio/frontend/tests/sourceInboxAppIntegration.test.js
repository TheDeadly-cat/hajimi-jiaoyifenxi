import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { normalizeSourceInboxNotificationFeed } from "../src/sourceInbox.js";

const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");

function notificationPollingFixture() {
  const effect = appSource.match(
    /useEffect\(\(\) => \{\s*const feedState = sourceInboxNotificationFeedRef\.current;[\s\S]*?\}, \[openSourceInboxEvent, sourceInboxNotificationState\.enabled\]\);/,
  );
  assert.ok(effect, "execute the actual App notification polling effect");
  const intervalConstant = appSource.match(/const SOURCE_INBOX_NOTIFICATION_POLL_MS = [\d_]+;/);
  assert.ok(intervalConstant);
  const items = [];
  const requests = [];
  const notifications = [];
  const unreadCounts = [];
  const intervals = new Map();
  const feedRef = { current: { baselineReady: false, controller: null, cursor: "", polling: false } };
  const unreadRef = { current: 0 };
  const openRef = { current: false };
  const visibilityRef = { current: 0 };
  const announcementRef = { current: 0 };
  let nextInterval = 0;
  let cleanup;

  function payloadFor(after) {
    const index = after ? Number(after.slice("cursor-".length)) : items.length;
    assert.ok(Number.isInteger(index) && index >= 0 && index <= items.length);
    const cursor = `cursor-${items.length}`;
    return {
      source_notifications: {
        version: "source_inbox_notification_feed_v1",
        baseline: after === "",
        notifications: after === "" ? [] : items.slice(index),
        cursor,
        head_cursor: cursor,
        unread_count: items.length,
        has_more: false,
        limit: 50,
        safety: {
          external_claims_verification: "external_unverified",
          execution_capability: "none",
          live_trading_allowed: false,
          provider_calls_performed: 0,
          market_calls_performed: 0,
          formal_rounds_created: 0,
        },
      },
    };
  }

  function render(enabled) {
    cleanup?.();
    runInNewContext(`${intervalConstant[0]}\n${effect[0]}`, {
      AbortController,
      useEffect: (setup) => { cleanup = setup(); },
      api: {
        sourceInboxNotifications: ({ after, limit, signal }) => {
          assert.equal(limit, 50);
          // Capture the backend snapshot at request time. Deliberately allow a
          // response after abort so the real effect must reject a stale reply.
          const payload = payloadFor(after);
          return new Promise((resolve) => requests.push({ after, signal, payload, resolve }));
        },
      },
      normalizeSourceInboxNotificationFeed,
      sourceInboxNotificationFeedRef: feedRef,
      sourceInboxNotificationState: { enabled },
      sourceInboxOpenRef: openRef,
      sourceInboxVisibilityEpochRef: visibilityRef,
      sourceInboxUnreadCountRef: unreadRef,
      sourceInboxAnnouncementSequenceRef: announcementRef,
      setSourceInboxUnreadCount: (count) => { unreadRef.current = count; unreadCounts.push(count); },
      setSourceInboxRefreshToken: () => assert.fail("closed inbox should update the global count"),
      setSourceInboxAnnouncement: () => {},
      createSourceInboxNotification: (notification) => notifications.push(notification.eventId),
      openSourceInboxEvent: () => {},
      setInterval: (callback, milliseconds) => {
        assert.equal(milliseconds, 60_000);
        const id = ++nextInterval;
        intervals.set(id, callback);
        return id;
      },
      clearInterval: (id) => intervals.delete(id),
    }, { filename: "App.jsx notification polling effect", timeout: 1000 });
    assert.equal(typeof cleanup, "function");
    assert.equal(intervals.size, 1);
  }

  async function respond(request) {
    assert.equal(normalizeSourceInboxNotificationFeed(request.payload, { requestedCursor: request.after }).valid, true);
    request.resolve(request.payload);
    await new Promise((resolve) => setImmediate(resolve));
  }

  return {
    requests, notifications, unreadCounts, feedRef, render, respond,
    add(eventId) {
      items.push({
        version: "source_inbox_notification_v1", id: eventId,
        created_at: 1_777_777_777_000 + items.length,
        source_channel: "official_source_monitor", source_key: "sec_filings", source_tier: "official_source",
        item_type: "official_filing", severity: "high", occurred_at: "2026-08-28T12:55:00Z",
        headline: "Fixture external filing", acknowledged: false, external_claims_verification: "external_unverified",
        safety: { fact_confirmation: false, approval: false, execution_authorization: false },
      });
    },
    tick() { return intervals.values().next().value(); },
    dispose() {
      cleanup?.();
      for (const request of requests) request.resolve(request.payload);
      assert.equal(intervals.size, 0);
    },
  };
}

test("enabling notifications silently rebases pre-opt-in unread items before notifying a new delta", async () => {
  const fixture = notificationPollingFixture();
  try {
    fixture.render(false);
    await fixture.respond(fixture.requests[0]);
    assert.equal(fixture.feedRef.current.cursor, "cursor-0");

    fixture.add("source_item_before_opt_in");
    fixture.render(true);
    await fixture.respond(fixture.requests[1]);
    assert.deepEqual(fixture.notifications, [], "the unread item that predates opt-in must not create a notification");
    assert.equal(fixture.requests[1].after, "");
    assert.equal(fixture.feedRef.current.cursor, "cursor-1");
    assert.equal(fixture.unreadCounts.at(-1), 1, "rebasing must retain the unread count");

    fixture.add("source_item_after_opt_in");
    const nextPoll = fixture.tick();
    await fixture.respond(fixture.requests[2]);
    await nextPoll;
    assert.equal(fixture.requests[2].after, "cursor-1");
    assert.deepEqual(fixture.notifications, ["source_item_after_opt_in"]);
  } finally {
    fixture.dispose();
  }
});

test("an initially enabled page baselines historical unread items without notifying them", async () => {
  const fixture = notificationPollingFixture();
  try {
    fixture.add("source_item_history_one");
    fixture.add("source_item_history_two");
    fixture.render(true);
    await fixture.respond(fixture.requests[0]);
    assert.equal(fixture.requests[0].after, "");
    assert.equal(fixture.feedRef.current.cursor, "cursor-2");
    assert.equal(fixture.unreadCounts.at(-1), 2);
    assert.deepEqual(fixture.notifications, []);

    fixture.add("source_item_after_page_baseline");
    const nextPoll = fixture.tick();
    await fixture.respond(fixture.requests[1]);
    await nextPoll;
    assert.equal(fixture.requests[1].after, "cursor-2");
    assert.deepEqual(fixture.notifications, ["source_item_after_page_baseline"]);
  } finally {
    fixture.dispose();
  }
});

test("disposed polling cannot overwrite the opt-in baseline even if its aborted request resolves", async (t) => {
  for (const oldReplyFirst of [true, false]) {
    await t.test(oldReplyFirst ? "old reply before new baseline" : "old reply after new baseline", async () => {
      const fixture = notificationPollingFixture();
      try {
        fixture.render(false);
        await fixture.respond(fixture.requests[0]);
        fixture.add("source_item_old_request_snapshot");
        const oldPoll = fixture.tick();
        const oldRequest = fixture.requests[1];
        assert.equal(oldRequest.after, "cursor-0");

        fixture.add("source_item_before_new_baseline");
        fixture.render(true);
        const baselineRequest = fixture.requests[2];
        const newController = fixture.feedRef.current.controller;
        assert.equal(oldRequest.signal.aborted, true);
        assert.equal(baselineRequest.after, "");
        assert.equal(fixture.feedRef.current.cursor, "");

        if (oldReplyFirst) {
          await fixture.respond(oldRequest);
          await oldPoll;
          assert.equal(fixture.feedRef.current.baselineReady, false);
          assert.equal(fixture.feedRef.current.cursor, "");
          assert.equal(fixture.feedRef.current.polling, true);
          assert.equal(fixture.feedRef.current.controller, newController);
          await fixture.respond(baselineRequest);
        } else {
          await fixture.respond(baselineRequest);
          await fixture.respond(oldRequest);
          await oldPoll;
        }
        assert.equal(fixture.feedRef.current.baselineReady, true);
        assert.equal(fixture.feedRef.current.cursor, "cursor-2");
        assert.equal(fixture.feedRef.current.polling, false);
        assert.deepEqual(fixture.notifications, []);

        fixture.add("source_item_after_new_baseline");
        const nextPoll = fixture.tick();
        await fixture.respond(fixture.requests[3]);
        await nextPoll;
        assert.equal(fixture.requests[3].after, "cursor-2");
        assert.deepEqual(fixture.notifications, ["source_item_after_new_baseline"]);
      } finally {
        fixture.dispose();
      }
    });
  }
});

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
