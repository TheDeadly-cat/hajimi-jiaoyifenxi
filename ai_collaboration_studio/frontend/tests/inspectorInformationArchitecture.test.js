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
const convergenceStyles = readFileSync(
  new URL("../src/styles/convergence-polish.css", import.meta.url),
  "utf8",
);

test("evidence dossier puts objective and project focus before status and configuration detail", () => {
  const objective = inspectorSource.indexOf('className="inspector-section objective-section"');
  const convergence = inspectorSource.indexOf("<ConvergenceCard");
  const focus = inspectorSource.indexOf("<ProjectRoundFocusCard");
  const routing = inspectorSource.indexOf("<ProviderRoutingPanel");
  const workflow = inspectorSource.indexOf('className="inspector-section workflow-summary-section"');

  assert.ok(objective >= 0);
  assert.ok(objective < focus);
  assert.ok(focus < convergence);
  assert.ok(convergence < routing);
  assert.ok(routing < workflow);
  assert.equal(inspectorSource.match(/<ConvergenceCard/g)?.length, 1);
});

test("mobile inspector publishes a stable section index without replacing the desktop rail", () => {
  const focusSource = readFileSync(
    new URL("../src/components/ProjectRoundFocusCard.jsx", import.meta.url),
    "utf8",
  );
  const inspectorStyleSource = readFileSync(
    new URL("../src/styles/room-inspector-refinement.css", import.meta.url),
    "utf8",
  );

  assert.match(
    inspectorSource,
    /<nav className="room-inspector-section-nav" aria-label="房间信息分区">[\s\S]*href="#inspector-rooms"[\s\S]*href="#inspector-project-focus"[\s\S]*href="#inspector-convergence"[\s\S]*href="#inspector-members"[\s\S]*href="#inspector-materials"[\s\S]*href="#inspector-artifacts"/,
  );
  assert.match(focusSource, /id="inspector-project-focus"/);
  assert.match(focusSource, /id="inspector-project-focus"[\s\S]{0,80}tabIndex=\{-1\}/);
  assert.match(focusSource, /aria-labelledby=\{headingId\}/);
  assert.equal(convergenceSource.match(/id="inspector-convergence"/g)?.length, 2);
  assert.equal(convergenceSource.match(/id="inspector-convergence" tabIndex=\{-1\}/g)?.length, 2);
  assert.equal(convergenceSource.match(/aria-label="收敛与决策门"/g)?.length, 2);
  for (const [target, label] of [
    ["rooms", "本轮目标"],
    ["members", "成员与身份"],
    ["materials", "共享资料"],
    ["artifacts", "结论与待办"],
  ]) {
    assert.match(
      inspectorSource,
      new RegExp(`id="inspector-${target}" tabIndex=\\{-1\\} aria-label="${label}"`),
    );
  }
  assert.match(inspectorStyleSource, /\.room-inspector-section-nav \{ display: none; \}/);
  assert.match(
    inspectorStyleSource,
    /@media \(max-width: 760px\)[\s\S]*\.room-inspector-section-nav \{[\s\S]*position: sticky;[\s\S]*overflow-x: auto/,
  );
  assert.match(
    inspectorStyleSource,
    /\.room-inspector-section-nav a \{[\s\S]*min-width: 48px;[\s\S]*min-height: 44px/,
  );
  const mobileInspectorStyleSource = inspectorStyleSource.split(
    "/* Desktop inspector section index */",
  )[0];
  assert.equal(
    mobileInspectorStyleSource.match(/:has\(#inspector-[a-z-]+:target\)/g)?.length,
    6,
  );
assert.match(
  inspectorStyleSource,
  /a\[href="#inspector-artifacts"\] \{[\s\S]*border-color: #5e94a2;[\s\S]*background: #dcebed/,
);
assert.match(
  inspectorStyleSource,
  /\.room-inspector-section-nav \{[\s\S]*display: grid;[\s\S]*grid-template-columns: repeat\(6, minmax\(44px, 1fr\)\);[\s\S]*gap: 1px;[\s\S]*padding-inline: 0;/,
);
assert.match(
  inspectorStyleSource,
  /\.room-inspector-section-nav a \{[\s\S]*min-width: 44px;[\s\S]*width: auto;/,
);
  assert.match(inspectorStyleSource, /scroll-margin-top: 72px/);
});

test("convergence keeps the first blocker, next action and user boundary visible", () => {
  const overview = convergenceSource.indexOf('className={`convergence-overview ${tone}`}');
  const progress = convergenceSource.indexOf('className="convergence-progress"');
  const primaryBlocker = convergenceSource.indexOf('className="convergence-primary-blocker"');
  const nextAction = convergenceSource.indexOf('className="convergence-next"');
  const userDecision = convergenceSource.indexOf("<UserDecisionGateRow");
  const details = convergenceSource.indexOf('className="convergence-details"');
  const boundary = convergenceSource.indexOf('className="convergence-boundary"');

  assert.ok(overview >= 0);
  assert.ok(overview < progress);
  assert.ok(progress < primaryBlocker);
  assert.ok(primaryBlocker < nextAction);
  assert.ok(nextAction < userDecision);
  assert.ok(userDecision < details);
  assert.ok(details < boundary);
  assert.match(convergenceSource, /blockers\[0\]\.title/);
  assert.match(convergenceSource, /blockers\.slice\(1\)/);
  assert.doesNotMatch(convergenceSource, /Math\.min\(blockers\.length/);
  assert.doesNotMatch(convergenceSource, /<details[^>]*\sopen(?:=|\s|>)/);
  assert.equal(convergenceSource.match(/<UserDecisionGateRow/g)?.length, 1);
  assert.match(convergenceSource, /const technicalGateCount = technicalGateStates\.length/);
  assert.match(convergenceSource, /const satisfiedTechnicalGateCount = technicalGateStates\.filter\(Boolean\)\.length/);
  assert.match(convergenceSource, /技术门均已满足；仍需用户复核，不产生执行授权。/);
  assert.match(convergenceSource, /<progress[\s\S]*max=\{technicalGateCount\}[\s\S]*value=\{satisfiedTechnicalGateCount\}/);
  assert.match(convergenceSource, /key=\{`\$\{blocker\.code \|\| "blocker"\}:\$\{blockerIndex\}`\}/);
  assert.match(convergenceStyles, /\.convergence-progress progress::\-webkit-progress-value\s*\{[\s\S]*#477d92[\s\S]*#58a2ad/);
  assert.doesNotMatch(convergenceStyles, /#(?:16a34a|22c55e|10b981|059669|34d399)/i);
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

test("short mobile inspectors surface project focus without shrinking controls", () => {
  const inspectorStyleSource = readFileSync(
    new URL("../src/styles/room-inspector-refinement.css", import.meta.url),
    "utf8",
  );
  const artifactStyleSource = readFileSync(
    new URL("../src/styles/artifact-panel.css", import.meta.url),
    "utf8",
  );

  assert.match(
    inspectorStyleSource,
    /@media \(max-width: 760px\) \{[\s\S]*?\.round-controls button \{[\s\S]*?min-height: 44px/,
  );
  assert.match(
    inspectorStyleSource,
    /\.section-heading \.text-action \{[\s\S]*?min-width: 44px;[\s\S]*?min-height: 44px/,
  );
  assert.match(
    artifactStyleSource,
    /@container artifact-panel \(max-width: 520px\)[\s\S]*?\.artifact-synthesizer \{[\s\S]*?grid-template-columns: minmax\(0, 1fr\)/,
  );
  assert.match(
    inspectorStyleSource,
    /@media \(max-width: 760px\) and \(max-height: 600px\)/,
  );
  assert.match(inspectorStyleSource, /\.objective-section \{[\s\S]*padding-block: 10px/);
  assert.match(inspectorStyleSource, /\.objective-section > p \{[\s\S]*line-height: 1\.55/);
  assert.doesNotMatch(
    inspectorStyleSource,
    /@media \(max-width: 760px\) and \(max-height: 600px\)[\s\S]*\.round-controls[\s\S]*min-height:/,
  );
});
