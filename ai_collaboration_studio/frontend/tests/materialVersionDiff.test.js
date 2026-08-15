import test from "node:test";
import assert from "node:assert/strict";
import {
  buildMaterialVersionDiff,
  formatMaterialVersionTime,
  materialVersionSnapshot,
} from "../src/materialVersionDiff.js";

function record(version, snapshot) {
  return { version, changed_at: 1_700_000_000_000 + version, snapshot };
}

test("reports content, provenance, symbols, publication time, and risk-label changes", () => {
  const left = record(1, {
    title: "旧财报摘要",
    kind: "note",
    source_url: "",
    content: "旧正文",
    active: true,
    metadata: {
      source_type: "internal_note",
      event_type: "earnings",
      publisher: "研究组",
      published_at: "2026-07-01T08:00:00+08:00",
      extraction_method: "manual",
      symbols: ["US.MU", "US.WDC"],
      prompt_injection_risk: {
        flagged: true,
        scanner: "material_prompt_injection_risk_v1",
        flags: ["instruction_override", "tool_execution"],
      },
    },
  });
  const right = record(2, {
    title: "公司公告原文",
    kind: "url",
    source_url: "https://example.com/report",
    content: "新正文",
    active: true,
    metadata: {
      source_type: "company_ir",
      event_type: "guidance",
      publisher: "Micron IR",
      published_at: "2026-07-02T09:30:00-04:00",
      final_url: "https://example.com/report/final",
      extraction_method: "html_text",
      symbols: ["US.MU", "US.STX"],
      prompt_injection_risk: {
        flagged: true,
        scanner: "material_prompt_injection_risk_v1",
        flags: ["financial_execution", "instruction_override"],
      },
    },
  });

  const diff = buildMaterialVersionDiff(left, right);

  assert.equal(diff.changed, true);
  assert.deepEqual(
    diff.fieldChanges.map((change) => change.key),
    [
      "title", "kind", "source_url", "content", "source_type", "event_type",
      "publisher", "published_at", "final_url", "extraction_method",
    ],
  );
  assert.deepEqual(diff.symbols.added, ["US.STX"]);
  assert.deepEqual(diff.symbols.removed, ["US.WDC"]);
  assert.deepEqual(diff.riskFlags.added, ["financial_execution"]);
  assert.deepEqual(diff.riskFlags.removed, ["tool_execution"]);
});

test("treats symbol and risk-label order as semantic sets and accepts API envelopes", () => {
  const snapshot = {
    title: "同一资料",
    kind: "note",
    content: "同一正文",
    active: true,
    metadata: {
      symbols: ["US.MU", "US.STX"],
      prompt_injection_risk: {
        flagged: true,
        scanner: "material_prompt_injection_risk_v1",
        flags: ["instruction_override", "tool_execution"],
      },
    },
  };
  const envelope = { material: snapshot };
  const direct = {
    ...snapshot,
    metadata: {
      ...snapshot.metadata,
      symbols: ["US.STX", "US.MU", "US.MU"],
      prompt_injection_risk: {
        ...snapshot.metadata.prompt_injection_risk,
        flags: ["tool_execution", "instruction_override", "tool_execution"],
      },
    },
  };

  assert.equal(materialVersionSnapshot(envelope).title, "同一资料");
  assert.equal(buildMaterialVersionDiff(envelope, direct).changed, false);
});

test("formats invalid or missing history timestamps safely", () => {
  assert.equal(formatMaterialVersionTime(""), "时间未记录");
  assert.equal(formatMaterialVersionTime("not-a-date"), "时间未记录");
  assert.match(formatMaterialVersionTime(1_700_000_000_000), /2023/);
});

