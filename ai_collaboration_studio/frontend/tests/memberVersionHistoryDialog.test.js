import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
const dialogSource = readFileSync(
  new URL("../src/components/MemberVersionHistoryDialog.jsx", import.meta.url),
  "utf8",
);
const hostStyles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const dialogStyles = readFileSync(
  new URL("../src/styles/member-version-history.css", import.meta.url),
  "utf8",
);

test("member version history owns a complete modal focus contract", () => {
  assert.match(dialogSource, /import \{ useModalFocus \} from "\.\.\/useModalFocus";/);
  assert.match(dialogSource, /const dialogRef = useRef\(null\)/);
  assert.match(dialogSource, /const closeButtonRef = useRef\(null\)/);
  assert.match(dialogSource, /open: open && Boolean\(member\)/);
  assert.match(dialogSource, /containerRef: dialogRef/);
  assert.match(dialogSource, /initialFocusRef: closeButtonRef/);
  assert.match(dialogSource, /ref=\{dialogRef\}/);
  assert.match(dialogSource, /ref=\{closeButtonRef\}/);
  assert.match(dialogSource, /aria-modal="true"/);
  assert.match(dialogSource, /tabIndex=\{-1\}/);
  assert.match(dialogSource, /event\.target === event\.currentTarget/);
  assert.doesNotMatch(dialogSource, /window\.addEventListener\("keydown"/);
});

test("member version history CSS follows its lazy module and leaves the eager trigger styled", () => {
  assert.match(appSource, /const MemberVersionHistoryDialog = lazy\(\(\) => import\("\.\/components\/MemberVersionHistoryDialog\.jsx"\)/);
  assert.match(appSource, /<Suspense fallback=\{<DeferredSurfaceFallback label="成员版本历史" dialog \/>\}>/);
  assert.match(dialogSource, /import "\.\.\/styles\/member-version-history\.css";/);

  assert.match(dialogStyles, /\.member-history-dialog\s*\{/);
  assert.match(dialogStyles, /\.member-version-diff\s*\{/);
  assert.match(dialogStyles, /@media \(max-width: 760px\)/);
  assert.match(dialogStyles, /@media \(max-width: 620px\)/);
  assert.doesNotMatch(hostStyles, /\.member-history-dialog\s*\{/);
  assert.doesNotMatch(hostStyles, /\.member-version-diff\s*\{/);
  assert.match(hostStyles, /\.member-order-actions \.member-history-button\s*\{/);
});
