import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const inspectorSource = readFileSync(
  new URL("../src/components/RoomInspector.jsx", import.meta.url),
  "utf8",
);
const convergenceSource = readFileSync(
  new URL("../src/components/ConvergenceCard.jsx", import.meta.url),
  "utf8",
);

test("evidence dossier puts status and next action before configuration detail", () => {
  const objective = inspectorSource.indexOf('className="inspector-section objective-section"');
  const convergence = inspectorSource.indexOf("<ConvergenceCard");
  const focus = inspectorSource.indexOf("<ProjectRoundFocusCard");
  const routing = inspectorSource.indexOf("<ProviderRoutingPanel");
  const workflow = inspectorSource.indexOf('className="inspector-section workflow-summary-section"');

  assert.ok(objective >= 0);
  assert.ok(objective < convergence);
  assert.ok(convergence < focus);
  assert.ok(focus < routing);
  assert.ok(routing < workflow);
  assert.equal(inspectorSource.match(/<ConvergenceCard/g)?.length, 1);
});

test("convergence keeps the first blocker, next action and user boundary visible", () => {
  const overview = convergenceSource.indexOf('className="convergence-overview"');
  const primaryBlocker = convergenceSource.indexOf('className="convergence-primary-blocker"');
  const nextAction = convergenceSource.indexOf('className="convergence-next"');
  const userDecision = convergenceSource.indexOf("<UserDecisionGateRow");
  const details = convergenceSource.indexOf('className="convergence-details"');
  const boundary = convergenceSource.indexOf('className="convergence-boundary"');

  assert.ok(overview >= 0);
  assert.ok(overview < primaryBlocker);
  assert.ok(primaryBlocker < nextAction);
  assert.ok(nextAction < userDecision);
  assert.ok(userDecision < details);
  assert.ok(details < boundary);
  assert.match(convergenceSource, /blockers\[0\]\.title/);
  assert.match(convergenceSource, /blockers\.slice\(1\)/);
  assert.doesNotMatch(convergenceSource, /Math\.min\(blockers\.length/);
  assert.doesNotMatch(convergenceSource, /<details[^>]*\sopen(?:=|\s|>)/);
  assert.equal(convergenceSource.match(/<UserDecisionGateRow/g)?.length, 1);
});

test("paused rounds retain an explicit end icon and lazy panels retain stable boundaries", () => {
  assert.match(inspectorSource, /Users, X \} from "lucide-react"/);
  assert.match(
    inspectorSource,
    /<PluginActionBoundary[^>]*label="模拟组合与风险预算">\s*<Suspense/,
  );
  assert.match(
    inspectorSource,
    /<PluginActionBoundary[^>]*label="模拟观察与验证">\s*<Suspense/,
  );
});

test("technical disclosure resets only when the room or round identity changes", () => {
  assert.match(
    convergenceSource,
    /key=\{`\$\{convergence\.room_id \|\| "room"\}:\$\{convergence\.round_id \|\| "round"\}`\}/,
  );
  assert.match(convergenceSource, /<span>门禁与其他阻断<\/span>/);
});
