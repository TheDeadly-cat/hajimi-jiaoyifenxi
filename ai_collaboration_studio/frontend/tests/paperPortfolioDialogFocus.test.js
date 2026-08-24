import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const panelSource = readFileSync(
  new URL("../src/components/PaperPortfolioPanel.jsx", import.meta.url),
  "utf8",
);
const dialogStart = panelSource.indexOf("export const PaperPortfolioDialog");
assert.ok(dialogStart >= 0, "PaperPortfolioDialog source should be discoverable");
const dialogSource = panelSource.slice(dialogStart);

test("paper portfolio launch controls preserve their payload and pass the exact trigger last", () => {
  assert.match(panelSource, /const canAdd = typeof onAdd === "function"/);
  assert.match(panelSource, /const canEdit = typeof onEdit === "function"/);
  assert.match(panelSource, /onClick=\{\(event\) => canAdd && onAdd\(\{\}, event\.currentTarget\)\} disabled=\{loading \|\| !canAdd\}/);
  assert.match(panelSource, /onClick=\{\(event\) => \{\s*if \(!canEdit\) return;\s*onEdit\(\{[\s\S]*\.\.\.portfolio,[\s\S]*\.\.\.\(lineage\.source \? \{ lineage_source: lineage\.source \} : \{\}\),[\s\S]*\}, event\.currentTarget\)/);
  assert.match(panelSource, /disabled=\{loading \|\| walkForwardBusy \|\| lineageLocked \|\| !canEdit\}/);
});

test("paper portfolio dialog owns focus only after the matching draft surface initializes", () => {
  assert.match(panelSource, /import \{ useModalFocus \} from "\.\.\/useModalFocus"/);
  assert.match(panelSource, /import \{[^}]*useId[^}]*useLayoutEffect[^}]*\} from "react"/);
  assert.match(dialogSource, /restoreFocusRef/);
  assert.match(dialogSource, /useLayoutEffect\(\(\) => \{[\s\S]*setInitializedPortfolio\(null\)[\s\S]*setDraft\(portfolioPlan\(portfolio\)\)[\s\S]*setInitializedPortfolio\(portfolio\)/);
  assert.match(dialogSource, /const surfaceOpen = Boolean\(open && portfolio && initializedPortfolio === portfolio\)/);
  assert.match(dialogSource, /const canClose = typeof onClose === "function"/);
  assert.match(dialogSource, /const canSubmit = typeof onSubmit === "function"/);
  assert.match(dialogSource, /useModalFocus\(\{[\s\S]*open: surfaceOpen,[\s\S]*containerRef: dialogRef,[\s\S]*initialFocusRef: closeButtonRef,[\s\S]*restoreFallbackRef: restoreFocusRef,[\s\S]*onClose: busy \|\| !canClose \? null : requestClose/);
  assert.match(dialogSource, /ref=\{dialogRef\}[\s\S]*role="dialog"[\s\S]*aria-modal="true"[\s\S]*aria-busy=\{busy\}[\s\S]*tabIndex=\{-1\}/);
  assert.match(dialogSource, /ref=\{closeButtonRef\}[\s\S]*aria-label="关闭模拟组合设置"/);
  assert.doesNotMatch(dialogSource, /\bautoFocus\b/);
});

test("paper portfolio dialog keeps every dismissal path fail closed while recomputing", () => {
  assert.match(dialogSource, /const requestClose = \(\) => \{\s*if \(!busy\) closeDialog\(\)/);
  assert.match(dialogSource, /onClose: busy \|\| !canClose \? null : requestClose/);
  assert.match(dialogSource, /surfaceOpen && busy[\s\S]*dialogRef\.current\?\.focus\(\{ preventScroll: true \}\)/);
  assert.match(dialogSource, /event\.target === event\.currentTarget && !busy/);
  assert.match(dialogSource, /onMouseDown=\{\(event\) => event\.stopPropagation\(\)\}/);
  assert.match(dialogSource, /aria-label="关闭模拟组合设置"[\s\S]*onClick=\{requestClose\} disabled=\{busy \|\| !canClose\}/);
  assert.match(dialogSource, /onClick=\{requestClose\} disabled=\{busy \|\| !canClose\}>取消/);
  assert.match(dialogSource, /const submit = async \(event\) => \{\s*event\.preventDefault\(\);\s*if \(busy\) return;\s*if \(!canSubmit\)/);
});

test("focus hardening preserves the single request session and submit payload", () => {
  assert.equal((dialogSource.match(/await onSubmit\(\{/g) || []).length, 1);
  assert.match(dialogSource, /const requestSession = requestSessionRef\.current \+ 1;\s*requestSessionRef\.current = requestSession/);
  assert.match(dialogSource, /if \(requestSessionRef\.current === requestSession\) closeDialog\(\)/);
  assert.match(dialogSource, /if \(requestSessionRef\.current === requestSession\) \{\s*setError\(boundedText\(requestError, "模拟组合保存失败。"\)\)/);
  assert.match(dialogSource, /if \(requestSessionRef\.current === requestSession\) setBusy\(false\)/);
  assert.match(dialogSource, /expected_version: portfolio\.version/);
  assert.match(dialogSource, /user_decision_id: lineageSource\.user_decision_id/);
  assert.match(dialogSource, /candidate_simulation_confirmation: \{[\s\S]*expected_source_sha256: candidateSeed\.source_sha256[\s\S]*strategy_rule_id: CANDIDATE_SIMULATION_RULE_ID[\s\S]*user_confirmed: true/);
});

test("paper portfolio host exposes a named ledger, honest counts, and progressive rows", () => {
  assert.match(panelSource, /const \[visibleLimit, setVisibleLimit\] = useState\(4\)/);
  assert.match(panelSource, /const panelTitleId = useId\(\)/);
  assert.match(panelSource, /<section className="paper-portfolio-panel" aria-labelledby=\{panelTitleId\}/);
  assert.match(panelSource, /<h3 id=\{panelTitleId\}>纸面组合与历史验证<\/h3>/);
  assert.match(panelSource, /const portfolioCollectionKey = useMemo/);
  assert.match(panelSource, /useEffect\(\(\) => setVisibleLimit\(4\), \[portfolioCollectionKey\]\)/);
  assert.match(panelSource, /portfolioRows\.slice\(0, visibleLimit\)/);
  assert.match(panelSource, /className="paper-portfolio-summary"/);
  assert.match(panelSource, /风险门内不代表推荐、批准或可执行/);
  assert.match(panelSource, /aria-labelledby=\{cardTitleId\}/);
  assert.match(panelSource, /<h4 id=\{cardTitleId\}>\{portfolioTitle\}<\/h4>/);
  assert.match(panelSource, /setVisibleLimit\(\(current\) => Math\.min\(current \+ 4, portfolioRows\.length\)\)/);
  assert.match(panelSource, /再显示 \{Math\.min\(4, hiddenPortfolioCount\)\} 个组合/);
  assert.match(panelSource, /className="paper-portfolio-empty" role="note"/);
  assert.match(panelSource, /不替代用户决定/);
});
