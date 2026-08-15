import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const panelSource = readFileSync(
  new URL("../src/components/PaperPortfolioPanel.jsx", import.meta.url),
  "utf8",
);
const dialogStart = panelSource.indexOf("export function PaperPortfolioDialog");
assert.ok(dialogStart >= 0, "PaperPortfolioDialog source should be discoverable");
const dialogSource = panelSource.slice(dialogStart);

test("paper portfolio launch controls preserve their payload and pass the exact trigger last", () => {
  assert.match(panelSource, /onClick=\{\(event\) => onAdd\(\{\}, event\.currentTarget\)\}/);
  assert.match(panelSource, /onClick=\{\(event\) => onEdit\(\{[\s\S]*\.\.\.portfolio,[\s\S]*lineage_source: lineage\.source[\s\S]*\}, event\.currentTarget\)\}/);
});

test("paper portfolio dialog owns focus only after the matching draft surface initializes", () => {
  assert.match(panelSource, /import \{ useModalFocus \} from "\.\.\/useModalFocus"/);
  assert.match(panelSource, /import \{ useEffect, useLayoutEffect, useMemo, useRef, useState \} from "react"/);
  assert.match(dialogSource, /restoreFocusRef/);
  assert.match(dialogSource, /useLayoutEffect\(\(\) => \{[\s\S]*setInitializedPortfolio\(null\)[\s\S]*setDraft\(portfolioPlan\(portfolio\)\)[\s\S]*setInitializedPortfolio\(portfolio\)/);
  assert.match(dialogSource, /const surfaceOpen = Boolean\(open && portfolio && initializedPortfolio === portfolio\)/);
  assert.match(dialogSource, /useModalFocus\(\{[\s\S]*open: surfaceOpen,[\s\S]*containerRef: dialogRef,[\s\S]*initialFocusRef: closeButtonRef,[\s\S]*restoreFallbackRef: restoreFocusRef,[\s\S]*onClose: busy \? null : requestClose/);
  assert.match(dialogSource, /ref=\{dialogRef\}[\s\S]*role="dialog"[\s\S]*aria-modal="true"[\s\S]*aria-busy=\{busy\}[\s\S]*tabIndex=\{-1\}/);
  assert.match(dialogSource, /ref=\{closeButtonRef\}[\s\S]*aria-label="关闭模拟组合设置"/);
  assert.doesNotMatch(dialogSource, /\bautoFocus\b/);
});

test("paper portfolio dialog keeps every dismissal path fail closed while recomputing", () => {
  assert.match(dialogSource, /const requestClose = \(\) => \{\s*if \(!busy\) closeDialog\(\)/);
  assert.match(dialogSource, /onClose: busy \? null : requestClose/);
  assert.match(dialogSource, /surfaceOpen && busy[\s\S]*dialogRef\.current\?\.focus\(\{ preventScroll: true \}\)/);
  assert.match(dialogSource, /event\.target === event\.currentTarget && !busy/);
  assert.match(dialogSource, /onMouseDown=\{\(event\) => event\.stopPropagation\(\)\}/);
  assert.match(dialogSource, /aria-label="关闭模拟组合设置"[\s\S]*onClick=\{requestClose\} disabled=\{busy\}/);
  assert.match(dialogSource, /onClick=\{requestClose\} disabled=\{busy\}>取消/);
  assert.match(dialogSource, /const submit = async \(event\) => \{\s*event\.preventDefault\(\);\s*if \(busy\) return/);
});

test("focus hardening preserves the single request session and submit payload", () => {
  assert.equal((dialogSource.match(/await onSubmit\(\{/g) || []).length, 1);
  assert.match(dialogSource, /const requestSession = requestSessionRef\.current \+ 1;\s*requestSessionRef\.current = requestSession/);
  assert.match(dialogSource, /if \(requestSessionRef\.current === requestSession\) closeDialog\(\)/);
  assert.match(dialogSource, /if \(requestSessionRef\.current === requestSession\) setError\(requestError\.message\)/);
  assert.match(dialogSource, /if \(requestSessionRef\.current === requestSession\) setBusy\(false\)/);
  assert.match(dialogSource, /expected_version: portfolio\.version/);
  assert.match(dialogSource, /user_decision_id: lineageSource\.user_decision_id/);
  assert.match(dialogSource, /candidate_simulation_confirmation: \{[\s\S]*expected_source_sha256: candidateSeed\.source_sha256[\s\S]*strategy_rule_id: CANDIDATE_SIMULATION_RULE_ID[\s\S]*user_confirmed: true/);
});
