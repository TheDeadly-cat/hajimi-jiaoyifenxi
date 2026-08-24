import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const component = readFileSync(
  new URL("../src/components/ChatTimeline.jsx", import.meta.url),
  "utf8",
);
const styles = readFileSync(
  new URL("../src/styles/chat-timeline.css", import.meta.url),
  "utf8",
);

test("empty collaboration state presents one ordered, non-interactive launch route", () => {
  const routeStart = component.indexOf('<ol className="conversation-empty-route"');
  const routeEnd = component.indexOf("</ol>", routeStart);

  assert.notEqual(routeStart, -1);
  assert.ok(routeEnd > routeStart);

  const route = component.slice(routeStart, routeEnd);
  const labels = ["明确问题", "核对证据", "确认启动"];
  let previousIndex = -1;

  for (const label of labels) {
    const currentIndex = route.indexOf(label);
    assert.ok(currentIndex > previousIndex, `${label} should keep its launch-order position`);
    previousIndex = currentIndex;
  }

  assert.equal((route.match(/<li>/g) || []).length, 3);
  assert.doesNotMatch(route, /onClick=|role="button"|tabIndex=/);
});

test("empty collaboration state compacts for narrow or short visual viewports", () => {
  const compactStart = styles.indexOf(
    "@media (max-width: 760px), (max-height: 600px)",
  );
  const compactEnd = styles.indexOf("@media (max-width: 700px)", compactStart);

  assert.notEqual(compactStart, -1);
  assert.ok(compactEnd > compactStart);

  const compactStyles = styles.slice(compactStart, compactEnd);
  assert.match(compactStyles, /grid-template-areas:[\s\S]*?"route route";/);
  assert.match(
    compactStyles,
    /\.chat-timeline-workspace \.conversation-empty-copy\s*\{\s*display:\s*none;\s*\}/,
  );
  assert.match(
    compactStyles,
    /\.chat-timeline-workspace \.conversation-empty-route small\s*\{\s*display:\s*none;\s*\}/,
  );
  assert.match(compactStyles, /width:\s*min\(720px, 100%\);/);
  assert.match(compactStyles, /margin:\s*0;/);
});
