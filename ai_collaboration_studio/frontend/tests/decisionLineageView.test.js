import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  buildDecisionLineagePanelModel,
  buildPortfolioLineageIndex,
  createdTime,
  decisionPackageSource,
  lineageDisplayText,
  runPortfolioVersion,
} from "../src/decisionLineageView.js";

test("decision lineage projection fails closed for malformed collections", () => {
  const model = buildDecisionLineagePanelModel({
    decisionPackages: "package",
    members: [{ id: "member_one" }, { id: "member_one" }],
    paperPortfolios: [],
    observations: null,
    walkForwardRunsByPortfolio: [],
  });
  assert.equal(model.integrityOk, false);
  assert.deepEqual(model.current, []);
  assert.match(model.issues.join("\n"), /必须是数组|重复/);

  const identityModel = buildDecisionLineagePanelModel({
    decisionPackages: [
      { package_id: "duplicate", lineage: [null, { id: "event" }, { id: "event" }] },
      { package_id: "duplicate", lineage: [] },
    ],
    members: [],
    paperPortfolios: [],
    observations: [{ id: "proposal" }, { id: "proposal" }],
    walkForwardRunsByPortfolio: {},
  });
  assert.equal(identityModel.integrityOk, false);
  assert.match(identityModel.issues.join("\n"), /决策包身份重复/);
  assert.match(identityModel.issues.join("\n"), /谱系包含无效条目/);
  assert.match(identityModel.issues.join("\n"), /谱系事件身份重复/);
  assert.match(identityModel.issues.join("\n"), /观察与提案身份重复/);
});

test("lineage index and revisions reject boolean, blank, and fractional coercion", () => {
  assert.equal(runPortfolioVersion(true), 0);
  assert.equal(runPortfolioVersion({ portfolio_version: "" }), 0);
  assert.equal(runPortfolioVersion({ portfolio_version: 1.5 }), 0);
  assert.equal(runPortfolioVersion({ portfolio_version: "2" }), 2);
  const decisionPackage = {
    package_id: "package_one",
    lineage: [{ id: "event_one", sequence_no: 1, resource_type: "simulation.paper_portfolio", resource_id: "portfolio_one", resource_revision: 2 }],
  };
  assert.equal(buildPortfolioLineageIndex("package").size, 0);
  assert.equal(buildPortfolioLineageIndex([decisionPackage]).get("portfolio_one").length, 1);
  assert.equal(decisionPackageSource({ anchor: { artifact_version: true } }).artifact_version, 0);
  assert.notEqual(createdTime("1720000000000"), "时间未知");
  assert.equal(lineageDisplayText({}, "安全回退"), "安全回退");
});

test("lineage model separates current, historical, broken, and unlinked evidence", () => {
  const model = buildDecisionLineagePanelModel({
    decisionPackages: [
      { package_id: "current", state: "active", integrity_ok: true, anchor: {}, lineage: [] },
      { package_id: "old", state: "stale", integrity_ok: true, anchor: {}, lineage: [] },
      { package_id: "broken", state: "chain_broken", integrity_ok: false, anchor: {}, lineage: [] },
    ],
    members: [],
    paperPortfolios: [{ id: "unlinked_portfolio" }],
    observations: [{ id: "unlinked_observation" }],
    walkForwardRunsByPortfolio: {},
  });
  assert.equal(model.integrityOk, true);
  assert.deepEqual(Object.values(model.stats), [2, 1, 1, 2]);
  assert.equal(model.hasUnlinked, true);

  const ordered = buildDecisionLineagePanelModel({
    decisionPackages: [
      { package_id: "older", state: "active", integrity_ok: true, anchor: { created_at: "1710000000000" }, lineage: [] },
      { package_id: "newer", state: "active", integrity_ok: true, anchor: { created_at: "1720000000000" }, lineage: [] },
    ],
    members: [],
    paperPortfolios: [],
    observations: [],
    walkForwardRunsByPortfolio: {},
  });
  assert.deepEqual(ordered.current.map((item) => item.package_id), ["newer", "older"]);
});

test("decision lineage owns styles, async epochs, and a pure Paper Portfolio dependency", () => {
  const componentSource = readFileSync(new URL("../src/components/DecisionLineagePanel.jsx", import.meta.url), "utf8");
  const portfolioSource = readFileSync(new URL("../src/components/PaperPortfolioPanel.jsx", import.meta.url), "utf8");
  const ownedCss = readFileSync(new URL("../src/styles/decision-lineage.css", import.meta.url), "utf8");
  const globalCss = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(componentSource, /styles\/decision-lineage\.css/);
  assert.match(componentSource, /requestRef\.current !== sequence/);
  assert.match(componentSource, /aria-busy=\{busy\}/);
  assert.match(componentSource, /data-model-state/);
  assert.match(portfolioSource, /from "\.\.\/decisionLineageView"/);
  assert.doesNotMatch(portfolioSource, /from "\.\/DecisionLineagePanel"/);
  assert.doesNotMatch(globalCss, /\.decision-lineage-panel/);
  assert.match(ownedCss, /container-name: decision-lineage/);
  assert.match(ownedCss, /container-type: inline-size/);
  assert.match(ownedCss, /@container decision-lineage \(max-width: 440px\)/);
});

test("decision lineage exposes named packages, review signals, and bounded proposal notes", () => {
  const componentSource = readFileSync(new URL("../src/components/DecisionLineagePanel.jsx", import.meta.url), "utf8");
  const ownedCss = readFileSync(new URL("../src/styles/decision-lineage.css", import.meta.url), "utf8");

  assert.match(componentSource, /const packageTitleId = useId\(\)/);
  assert.match(componentSource, /aria-labelledby=\{packageTitleId\}/);
  assert.match(componentSource, /<h4 id=\{packageTitleId\}>/);
  assert.match(componentSource, /className="decision-lineage-visually-hidden"/);
  assert.match(componentSource, /const reviewCount = model\.stats\.broken \+ model\.stats\.unlinked/);
  assert.match(componentSource, /className=\{`decision-lineage-review-signal \$\{reviewSignal\.tone\}`\}/);
  assert.match(componentSource, /不代表研究结论获批/);
  assert.match(componentSource, /<data value=\{model\.stats\.broken\}>/);
  assert.match(componentSource, /const noteHelpId = useId\(\)/);
  assert.match(componentSource, /aria-describedby=\{noteHelpId\}/);
  assert.match(componentSource, /<output aria-label=\{`已输入 \$\{derivationNote\.length\} \/ 1000 字`\}>/);
  assert.match(componentSource, /key=\{`\$\{item\.id\}:\$\{proposalIndex\}`\}/);
  assert.match(ownedCss, /\.decision-lineage-review-signal \{[\s\S]*grid-template-columns: auto minmax\(0, 1fr\) auto/);
  assert.match(ownedCss, /\.decision-lineage-note-meter output \{[\s\S]*font:/);
  assert.match(ownedCss, /@container decision-lineage \(max-width: 440px\)[\s\S]*\.decision-lineage-review-signal > data \{ grid-column: 2; justify-self: start; \}/);
  assert.match(ownedCss, /@media \(forced-colors: active\)[\s\S]*\.decision-lineage-review-signal > data/);
  assert.equal((ownedCss.match(/\{/g) || []).length, (ownedCss.match(/\}/g) || []).length);
});
