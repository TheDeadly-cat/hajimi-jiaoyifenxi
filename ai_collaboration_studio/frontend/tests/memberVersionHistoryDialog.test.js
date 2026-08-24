import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  memberHistoryIdentity,
  memberHistorySelectableRows,
  memberVersionListProjection,
  memberVersionPairProjection,
} from "../src/memberVersionHistoryUi.js";

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
  assert.match(dialogSource, /onClose\?\.\(\)/);
  assert.match(dialogSource, /data-list-state=\{listStatus\}/);
  assert.match(dialogSource, /disabled=\{loading\}/);
  assert.match(dialogSource, /function VersionOptions\(\{ rows, prefix \}\)[\s\S]*memberHistoryVersionNumber\(row\)[\s\S]*value=\{version\}/);
  assert.match(dialogSource, /memberHistoryText\(snapshot\.provider, "服务商未记录"\)/);
  assert.match(dialogSource, /memberHistoryText\(snapshot\.model, "模型未记录"\)/);
  assert.doesNotMatch(dialogSource, /window\.addEventListener\("keydown"/);
});

test("member version evidence projections reject malformed, duplicate, and mismatched responses", () => {
  assert.equal(memberHistoryIdentity({}, { id: "member-a", version: 2 }).integrityOk, false);
  assert.equal(memberVersionListProjection({ versions: "bad" }).ok, false);
  const rows = [
    { version: 2, integrity_ok: true },
    { version: 2, integrity_ok: true },
    { version: 1, integrity_ok: true },
    { version: 0, integrity_ok: true },
  ];
  const list = memberVersionListProjection({
    member: { current_version: 2 },
    versions: rows,
  });
  assert.equal(list.ok, true);
  assert.equal(list.integrityOk, false);
  assert.deepEqual(memberHistorySelectableRows(list.rows).map((row) => row.version), [1]);
  assert.equal(memberVersionPairProjection(
    { member_version: { version: 1, integrity_ok: true } },
    { member_version: { version: 3, integrity_ok: true } },
    { baseVersion: 1, targetVersion: 2 },
  ).ok, false);
});

test("member version history CSS follows its lazy module and leaves the eager trigger styled", () => {
  assert.match(appSource, /const MemberVersionHistoryDialog = lazy\(\(\) => import\("\.\/components\/MemberVersionHistoryDialog\.jsx"\)/);
  assert.match(appSource, /<Suspense fallback=\{<DeferredSurfaceFallback label="成员版本历史" dialog \/>\}>/);
  assert.match(dialogSource, /import "\.\.\/styles\/member-version-history\.css";/);

  assert.match(dialogStyles, /\.member-history-dialog\s*\{/);
  assert.match(dialogStyles, /\.member-version-diff\s*\{/);
  assert.match(dialogStyles, /@media \(max-width: 760px\)/);
  assert.match(dialogStyles, /@media \(max-width: 620px\)/);
  assert.match(dialogStyles, /\.member-history-status\s*\{/);
  assert.match(dialogStyles, /env\(safe-area-inset-top\)/);
  assert.match(dialogStyles, /prefers-reduced-motion: reduce/);
  assert.doesNotMatch(hostStyles, /\.member-history-dialog\s*\{/);
  assert.doesNotMatch(hostStyles, /\.member-version-diff\s*\{/);
  assert.match(hostStyles, /\.member-order-actions \.member-history-button\s*\{/);
});
