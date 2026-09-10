import assert from "node:assert/strict";
import test from "node:test";

import {
  buildSourceInboxDeepLink,
  isSourceInboxEventId,
  parseSourceInboxDeepLink,
  updateSourceInboxDeepLink,
} from "../src/sourceInboxDeepLink.js";

test("source inbox event ids match the backend identity boundary exactly", () => {
  assert.equal(isSourceInboxEventId("source_item.one:2-3"), true);
  assert.equal(isSourceInboxEventId(`a${"b".repeat(159)}`), true);
  for (const invalid of ["", " item", "item ", "item/one", "事件", `a${"b".repeat(160)}`]) {
    assert.equal(isSourceInboxEventId(invalid), false, invalid);
  }
});

test("deep-link parsing rejects ambiguity and invalid decoded ids", () => {
  assert.equal(
    parseSourceInboxDeepLink("https://studio.test/room?mode=review&source_event=event.one%3A2#details"),
    "event.one:2",
  );
  assert.equal(parseSourceInboxDeepLink("https://studio.test/room?mode=review"), "");
  assert.equal(parseSourceInboxDeepLink("https://studio.test/?source_event=event%2Fone"), "");
  assert.equal(
    parseSourceInboxDeepLink("https://studio.test/?source_event=one&source_event=two"),
    "",
  );
  assert.equal(parseSourceInboxDeepLink("javascript:?source_event=event.one"), "");
  assert.equal(parseSourceInboxDeepLink({ href: "not a valid absolute url" }), "");
});

test("building a deep link changes only source_event and preserves query and hash", () => {
  const current = "https://studio.test/workspace?room=one&filter=a%2Fb#timeline";
  assert.equal(
    buildSourceInboxDeepLink(current, "source:item.2"),
    "/workspace?room=one&filter=a%2Fb&source_event=source%3Aitem.2#timeline",
  );
  assert.equal(
    buildSourceInboxDeepLink(
      "https://studio.test/workspace?room=one&source_event=old&filter=a%2Fb#timeline",
      "",
    ),
    "/workspace?room=one&filter=a%2Fb#timeline",
  );
  assert.throws(
    () => buildSourceInboxDeepLink(current, "source/item"),
    /not a valid Source Inbox event id/,
  );
  assert.throws(
    () => buildSourceInboxDeepLink("data:text/plain,source_event=event.one", "event.one"),
    /require an HTTP\(S\) location/,
  );
});

test("history updates preserve state and use only explicit push or replace modes", () => {
  const calls = [];
  const historyObject = {
    state: { room: "one" },
    pushState(...args) { calls.push(["push", ...args]); },
    replaceState(...args) { calls.push(["replace", ...args]); },
  };
  const locationLike = { href: "https://studio.test/workspace?room=one#timeline" };

  assert.equal(updateSourceInboxDeepLink({
    eventId: "event.one",
    historyObject,
    locationLike,
    mode: "push",
  }), "/workspace?room=one&source_event=event.one#timeline");
  assert.deepEqual(calls[0], [
    "push",
    { room: "one" },
    "",
    "/workspace?room=one&source_event=event.one#timeline",
  ]);

  assert.equal(updateSourceInboxDeepLink({
    eventId: "",
    historyObject,
    locationLike: { href: "https://studio.test/workspace?room=one&source_event=event.one#timeline" },
  }), "/workspace?room=one#timeline");
  assert.equal(calls[1][0], "replace");
  assert.throws(
    () => updateSourceInboxDeepLink({ historyObject, locationLike, mode: "assign" }),
    /must be push or replace/,
  );
  assert.equal(calls.length, 2);
});
