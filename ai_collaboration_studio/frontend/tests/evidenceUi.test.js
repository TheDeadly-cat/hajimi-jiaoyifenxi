import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  evidenceSourcePreviewPresentation,
  evidenceStatusPresentation,
} from "../src/evidenceUi.js";

test("evidence status presentation rejects negative, fractional, and non-finite counts", () => {
  const view = evidenceStatusPresentation({
    counts: { support: "2", counter: -1, conflict: 1.5, gap: Number.POSITIVE_INFINITY },
  });
  assert.deepEqual(view.items.map((item) => item.value), [2, 0, 0, 0]);
  assert.equal(view.total, 2);
  assert.match(view.ariaLabel, /支持 2/);

  const compact = evidenceStatusPresentation({ counts: {}, compact: true });
  assert.equal(compact.empty, true);
  assert.deepEqual(compact.items, []);
  assert.match(compact.ariaLabel, /未记录/);
});

test("frozen source presentation emits one calibrated incomplete warning and supports retry", () => {
  const incomplete = evidenceSourcePreviewPresentation({
    item: { type: "message", preview: "截断预览", previewComplete: false },
    sourceDetailRequired: true,
    sourceDetail: {
      status: "ready",
      preview: "完整读取后的受限内容",
      previewComplete: false,
    },
  });
  assert.equal(incomplete.visible, true);
  assert.equal(incomplete.statusLabel, "冻结受限");
  assert.match(incomplete.notice, /完整来源仍/);
  assert.equal(incomplete.boundaryNote, "");

  const failed = evidenceSourcePreviewPresentation({
    item: { type: "message", previewComplete: false },
    sourceDetailRequired: true,
    sourceDetail: { status: "error", error: "冻结轮次读取失败" },
  });
  assert.equal(failed.canLoad, true);
  assert.equal(failed.loadButtonLabel, "重试加载完整冻结来源");
  assert.equal(failed.error, "冻结轮次读取失败");
  assert.match(failed.boundaryNote, /不会访问实时市场/);
});

test("evidence entry components keep sanitization, contained scrolling, and mobile contracts", () => {
  const previewSource = readFileSync(
    new URL("../src/components/EvidenceSourcePreview.jsx", import.meta.url),
    "utf8",
  );
  const statusSource = readFileSync(
    new URL("../src/components/EvidenceStatusStrip.jsx", import.meta.url),
    "utf8",
  );
  const styles = readFileSync(new URL("../src/styles/evidence-ui.css", import.meta.url), "utf8");

  assert.match(previewSource, /safeExternalUrl/);
  assert.match(previewSource, /aria-busy/);
  assert.match(previewSource, /tabIndex=\{0\}/);
  assert.match(previewSource, /event\.key !== "Enter" && event\.key !== " "/);
  assert.match(previewSource, /event\.preventDefault\(\)/);
  assert.match(statusSource, /role="list"/);
  assert.match(styles, /white-space:\s*pre-wrap/);
  assert.match(styles, /@media \(max-width: 520px\)/);
});
