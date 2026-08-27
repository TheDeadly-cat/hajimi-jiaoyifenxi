import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  MANUAL_CHATGPT_MODES,
  CHATGPT_CONTINUATION_URL,
  formatManualChatGPTContextSize,
  formatManualChatGPTCostEstimate,
  manualChatGPTIndependenceLabel,
  manualChatGPTPrimaryAction,
  manualChatGPTReviewClientRequestId,
  manualChatGPTStateView,
  normalizeManualChatGPT,
  shortManualChatGPTHash,
} from "../src/manualChatGPT.js";

function session(state = "BUNDLE_READY") {
  return {
    id: "mcg_one",
    room_id: "room_one",
    round_id: "round_one",
    mode: "standard",
    state,
    objective: "A bounded objective",
    bundle_sha256: "a".repeat(64),
    context_sha256: "b".repeat(64),
    task_prompt: "frozen prompt",
    declared_model: "claimed model",
    declared_model_trusted: true,
    bundle: {
      context: {
        roles: [{ role_id: "role_one" }, { role_id: "role_two" }],
        evidence_index: [{ evidence_id: "evidence_one" }],
        candidate_matrix: [{ artifact_id: "artifact_one" }],
      },
      budget: {
        chatgpt_panel_calls: 2,
        independent_api_reviews: 3,
      },
      planning: {
        version: "manual_chatgpt_planning_v1",
        context_size: {
          characters: 5000,
          utf8_bytes: 8192,
          estimated_tokens: 2048,
          token_estimation_method: "cjk_one_ascii_four_v1",
        },
        workload: {
          estimated_api_review_input_tokens: 6144,
          api_review_output_token_budget: 4200,
        },
        estimated_api_cost: {
          status: "estimated",
          currency: "USD",
          amount_usd: "0.004321",
          rate_card_label: "test-rate-v1",
          manual_chatgpt_cost_included: true,
        },
      },
    },
    integrity: { ok: true },
    validation_issues: [],
  };
}

test("manual ChatGPT presets preserve the requested 1/2, 2/3, and 3/4 call shape", () => {
  assert.deepEqual(
    MANUAL_CHATGPT_MODES.map(({ id, panels, reviews }) => [id, panels, reviews]),
    [["quick", 1, 2], ["standard", 2, 3], ["deep", 3, 4]],
  );
});

test("manual ChatGPT projection never promotes a declared model to trusted evidence", () => {
  const projected = normalizeManualChatGPT(session());
  assert.equal(projected.declaredModel, "claimed model");
  assert.equal(projected.declaredModelTrusted, false);
  assert.equal(projected.roleCount, 2);
  assert.equal(projected.evidenceCount, 1);
  assert.equal(projected.chatGPTPanels, 2);
  assert.equal(projected.apiReviews, 3);
  assert.equal(projected.contextUtf8Bytes, 8192);
  assert.equal(projected.estimatedContextTokens, 2048);
  assert.equal(projected.estimatedApiReviewInputTokens, 6144);
  assert.equal(projected.apiReviewOutputTokenBudget, 4200);
  assert.equal(projected.manualChatGPTCostIncluded, false);
  assert.equal(formatManualChatGPTContextSize(projected), "8.0 KB · 约 2,048 Token");
  assert.equal(formatManualChatGPTCostEstimate(projected), "约 US$0.0043");
  assert.equal(
    formatManualChatGPTCostEstimate(normalizeManualChatGPT({
      ...session(),
      bundle: { ...session().bundle, planning: {} },
    })),
    "待配置",
  );
  assert.equal(shortManualChatGPTHash("b".repeat(64)), "bbbbbbbb…bbbbbb");
  assert.equal(
    manualChatGPTIndependenceLabel("same_answer_multi_role_views"),
    "同一回答中的多角色视角",
  );
});

test("manual ChatGPT projection drops malformed imported panels before rendering", () => {
  const projected = normalizeManualChatGPT({
    ...session("READY_FOR_DECISION"),
    result: {
      panels: [
        null,
        "not-an-object",
        {
          panel_id: "panel_one",
          panel_kind: "analysis",
          call_index: 1,
          declared_independence: "same_answer_multi_role_views",
          summary: "Bounded summary",
          conclusion: "Bounded conclusion",
          role_views: [
            null,
            { role_id: "role_one", assessment: "Assessment", evidence_refs: "not-an-array" },
          ],
        },
      ],
    },
  });

  assert.equal(projected.result.panels.length, 1);
  assert.equal(projected.result.panels[0].panel_id, "panel_one");
  assert.deepEqual(projected.result.panels[0].role_views, [{
    role_id: "role_one",
    assessment: "Assessment",
    evidence_refs: [],
    uncertainty: "",
  }]);
});

test("API_REVIEW exposes the real next step and import errors preserve exact paths", () => {
  const review = manualChatGPTStateView(session("API_REVIEW"));
  assert.match(review.detail, /运行独立 API 审查/);
  assert.equal(review.tone, "review");

  const rejected = manualChatGPTStateView({
    ...session("IMPORT_REJECTED"),
    validation_issues: [{
      path: "$.panels[0].conclusion",
      code: "REQUIRED",
      message: "不能为空。",
    }],
  });
  assert.deepEqual(rejected.issues[0], {
    path: "$.panels[0].conclusion",
    code: "REQUIRED",
    message: "不能为空。",
  });
});

test("waiting state explains the same-chat multi-turn single-import protocol", () => {
  const waiting = manualChatGPTStateView(session("WAITING_FOR_CHATGPT"));
  assert.match(waiting.detail, /同一会话/);
  assert.match(waiting.detail, /2 次回复/);
  assert.match(waiting.detail, /唯一 JSON/);
  assert.match(waiting.detail, /若新页未出现/);
});

test("ChatGPT continuation uses the fixed external destination", () => {
  assert.equal(CHATGPT_CONTINUATION_URL, "https://chatgpt.com/");
});

test("primary action follows rejected, blocked, frozen, and integrity-failure states", () => {
  const rejected = manualChatGPTStateView({
    ...session("IMPORT_REJECTED"),
    repair_prompt: "repair this exact object",
  });
  assert.deepEqual(
    manualChatGPTPrimaryAction(rejected),
    { id: "copy_repair_prompt", label: "复制修复提示", enabled: true },
  );
  assert.deepEqual(
    manualChatGPTPrimaryAction(rejected, { hasImportText: true }),
    { id: "reimport_fixed_json", label: "重新校验修复结果", enabled: true },
  );

  for (const state of ["BUDGET_BLOCKED", "NEEDS_USER_ACTION", "DRAFT"]) {
    assert.deepEqual(
      manualChatGPTPrimaryAction(manualChatGPTStateView(session(state))),
      { id: "reset_for_new_bundle", label: "创建新任务", enabled: true },
    );
  }
  assert.deepEqual(
    manualChatGPTPrimaryAction(manualChatGPTStateView(session("FROZEN"))),
    { id: "reset_for_new_bundle", label: "开始新任务", enabled: true },
  );
  assert.deepEqual(
    manualChatGPTPrimaryAction(manualChatGPTStateView({
      ...session("IMPORT_REJECTED"),
      integrity: { ok: false },
    })),
    { id: "reset_for_new_bundle", label: "创建新任务", enabled: true },
  );
});

test("review and freeze primary actions expose their real enablement gates", () => {
  const review = manualChatGPTStateView({
    ...session("API_REVIEW"),
    api_review: { available: true },
  });
  assert.equal(manualChatGPTPrimaryAction(review).enabled, false);
  assert.deepEqual(
    manualChatGPTPrimaryAction(review, { hasReviewRoute: true }),
    { id: "run_api_review", label: "运行 3 次独立 API 审查", enabled: true },
  );

  const ready = manualChatGPTStateView(session("READY_FOR_DECISION"));
  assert.equal(manualChatGPTPrimaryAction(ready, { hasDecision: true }).enabled, false);
  assert.deepEqual(
    manualChatGPTPrimaryAction(ready, {
      hasDecision: true,
      freezeAcknowledged: true,
    }),
    { id: "confirm_freeze", label: "确认并冻结决定", enabled: true },
  );
});

test("orphan review recovery is exposed only for an integrity-valid eligible zero-call review", () => {
  const eligibleSource = {
    ...session("API_REVIEW"),
    review_recovery: {
      available: true,
      eligible: true,
      reason_code: "ORPHANED_ZERO_CALL_REVIEW",
      acknowledgement: "REAUTHORIZE_ZERO_CALL_ORPHANED_REVIEW",
      recovery_count: 0,
    },
  };
  const eligible = manualChatGPTStateView(eligibleSource);
  assert.equal(eligible.reviewRecoveryEligible, true);
  assert.deepEqual(
    manualChatGPTPrimaryAction(eligible),
    { id: "recover_api_review", label: "重新授权并恢复零调用审查", enabled: true },
  );

  const ineligibleVariants = [
    { review_recovery: { ...eligibleSource.review_recovery, eligible: false } },
    { review_recovery: { ...eligibleSource.review_recovery, available: false } },
    { review_recovery: { ...eligibleSource.review_recovery, reason_code: "REVIEW_HAS_CALL_ACTIVITY" } },
    { review_recovery: { ...eligibleSource.review_recovery, acknowledgement: "" } },
    { state: "READY_FOR_DECISION" },
    { integrity: { ok: false } },
  ];
  for (const variant of ineligibleVariants) {
    const projected = manualChatGPTStateView({ ...eligibleSource, ...variant });
    assert.equal(projected.reviewRecoveryEligible, false);
    assert.notEqual(manualChatGPTPrimaryAction(projected).id, "recover_api_review");
  }
});

test("review idempotency key changes after recovery even at the session id length limit", () => {
  const source = {
    ...session("API_REVIEW"),
    id: "m".repeat(80),
    result_sha256: "c".repeat(64),
    review_recovery: {
      available: true,
      eligible: true,
      reason_code: "ORPHANED_ZERO_CALL_REVIEW",
      acknowledgement: "REAUTHORIZE_ZERO_CALL_ORPHANED_REVIEW",
      recovery_count: 0,
    },
  };
  const beforeRecovery = manualChatGPTReviewClientRequestId(normalizeManualChatGPT(source));
  const afterRecovery = manualChatGPTReviewClientRequestId(normalizeManualChatGPT({
    ...source,
    review_recovery: { ...source.review_recovery, eligible: false, recovery_count: 1 },
  }));

  assert.match(beforeRecovery, /^manual-review-r0-/);
  assert.match(afterRecovery, /^manual-review-r1-/);
  assert.notEqual(afterRecovery, beforeRecovery);
  assert.ok(beforeRecovery.length <= 160);
  assert.ok(afterRecovery.length <= 160);
});

test("independent reviews and the deterministic decision card stay provenance-first", () => {
  const projected = normalizeManualChatGPT({
    ...session("READY_FOR_DECISION"),
    result_sha256: "c".repeat(64),
    api_review: {
      available: true,
      status: "COMPLETED",
      expected_calls: 3,
      completed_calls: 3,
      all_calls_are_distinct: true,
      provider: "openai",
      requested_model: "gpt-test",
      reviews: [{
        review_index: 1,
        review_kind: "fact_check",
        verdict: "pass",
        summary: "Bounded review passed.",
        provider: "openai",
        requested_model: "gpt-test",
        response_model: "gpt-test-2026",
        independence_classification: "same_model_independent_call",
        findings: [],
      }],
    },
    decision_card_sha256: "d".repeat(64),
    decision_card: {
      ready_for_user_decision: true,
      summary: "Decision summary.",
      imported_recommended_option_id: "option_1",
      decision_options: [{
        option_id: "option_1",
        title: "Bounded option",
        rationale: "Preserves the boundary.",
        risks: [],
      }],
      blocking_findings: [],
      nonblocking_findings: [],
      open_questions: [],
    },
  });
  assert.equal(projected.apiReviewCompletedCalls, 3);
  assert.equal(projected.apiReviewDistinctCalls, true);
  assert.equal(projected.apiReviewRecords[0].reviewKind, "fact_check");
  assert.equal(projected.decisionCard.ready, true);
  assert.equal(projected.decisionCard.options[0].optionId, "option_1");
  assert.equal(projected.decisionCard.importedRecommendedOptionId, "option_1");
});

test("ChatGPT collaboration surface is lazy, clipboard-driven, and owns responsive styles", () => {
  const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const composerSource = readFileSync(new URL("../src/components/Composer.jsx", import.meta.url), "utf8");
  const componentSource = readFileSync(
    new URL("../src/components/ChatGPTCollaborationDialog.jsx", import.meta.url),
    "utf8",
  );
  const manualSource = readFileSync(new URL("../src/manualChatGPT.js", import.meta.url), "utf8");
  const cssSource = readFileSync(new URL("../src/styles/manual-chatgpt.css", import.meta.url), "utf8");

  assert.match(appSource, /lazy\(\(\) => import\("\.\/components\/ChatGPTCollaborationDialog\.jsx"\)/);
  assert.match(composerSource, /ChatGPT 协作/);
  assert.match(composerSource, /disabled=\{disabled \|\| chatGPTDisabled\}/);
  assert.doesNotMatch(composerSource, /chatGPTDisabled \|\| !value\.trim\(\)/);
  assert.match(composerSource, /不会自动调用 Provider/);
  assert.match(appSource, /if \(!room \|\| roundBusy \|\| chatGPTCollaborationOpen\) return;/);
  assert.doesNotMatch(appSource, /roundBusy \|\| !composer\.trim\(\)/);
  assert.match(componentSource, /navigator\.clipboard\.writeText/);
  assert.match(componentSource, /navigator\.clipboard\.readText/);
  assert.match(componentSource, /人工复制与导入，不会自动调用 ChatGPT Provider/);
  assert.match(componentSource, /manual-chatgpt-prompt-fallback/);
  assert.match(componentSource, /value=\{view\.taskPrompt\}[\s\S]*readOnly/);
  assert.match(componentSource, /onClick=\{confirmManualCopy\}/);
  assert.match(componentSource, /我已手动复制，进入导入/);
  assert.match(manualSource, /https:\/\/chatgpt\.com\//);
  assert.match(componentSource, /href=\{CHATGPT_CONTINUATION_URL\}/);
  assert.match(componentSource, /target="_blank"/);
  assert.match(componentSource, /rel="noopener noreferrer"/);
  assert.doesNotMatch(componentSource, /globalThis\.open/);
  assert.doesNotMatch(componentSource, /重新校验已粘贴内容/);
  assert.match(componentSource, /独立审查授权/);
  assert.match(componentSource, /独立性为声明元数据，未由手工导入证明/);
  assert.match(componentSource, /\["READY_FOR_DECISION", "FROZEN"\]\.includes\(view\.state\)/);
  assert.match(componentSource, /manual-chatgpt-audit-details/);
  assert.match(componentSource, /manual-chatgpt-audit-summary-content/);
  assert.match(componentSource, /决定卡是当前主视图；展开后可核对完整来源链/);
  assert.match(componentSource, /manual-chatgpt-review-findings/);
  assert.match(componentSource, /manual-chatgpt-option-risks/);
  assert.match(componentSource, /manual-chatgpt-decision-notes/);
  assert.match(componentSource, /证据引用/);
  assert.doesNotMatch(componentSource, /<details[^>]+defaultOpen/);
  assert.match(componentSource, /panel\.role_views/);
  assert.match(manualSource, /运行 .* 次独立 API 审查/);
  assert.match(componentSource, /RESEARCH_ONLY_USER_DECISION/);
  assert.match(manualSource, /确认并冻结决定/);
  assert.match(componentSource, /上下文大小/);
  assert.match(componentSource, /预计 API 成本/);
  assert.match(componentSource, /不会显示为零成本/);
  assert.match(componentSource, /ChatGPT 回合 \/ Panel/);
  assert.match(componentSource, /ChatGPT 回合数是人工操作协议/);
  assert.match(componentSource, /第 \{panel\.call_index\}\/\{view\.chatGPTPanels\} 回合/);
  assert.match(componentSource, /manualChatGPTPrimaryAction/);
  assert.match(componentSource, /api\.listManualChatGPT\(roomId\)/);
  assert.doesNotMatch(componentSource, /api\.latestManualChatGPT\(roomId\)/);
  assert.match(componentSource, /manualChatGPTReviewClientRequestId\(current\)/);
  assert.match(componentSource, /operationRef\.current !== sequence/);
  assert.match(componentSource, /setObjective\(initialObjective\.trim\(\) \|\| view\?\.objective \|\| ""\)/);
  assert.match(manualSource, /重新校验修复结果/);
  assert.match(componentSource, /view\.repairPrompt && importText\.trim\(\)/);
  assert.match(componentSource, /className="secondary"[\s\S]*onClick=\{copyRepairPrompt\}[\s\S]*复制修复提示/);
  assert.match(componentSource, /独立审查通道（技术 ID）/);
  assert.doesNotMatch(componentSource, />\s*Provider\s*</);
  assert.doesNotMatch(componentSource, /使用 Provider 已配置默认值/);
  assert.match(componentSource, /useModalFocus/);
  assert.match(componentSource, /styles\/manual-chatgpt\.css/);
  assert.match(cssSource, /max-height:\s*calc\(var\(--visual-viewport-height, 100dvh\) - 24px\)/);
  assert.match(cssSource, /env\(safe-area-inset-bottom\)/);
  assert.doesNotMatch(cssSource, /\.manual-chatgpt-audit-details > summary\s*\{[^}]*display:\s*flex/s);
  assert.doesNotMatch(cssSource, /\.manual-chatgpt-audit-details\s*\{[^}]*overflow:\s*hidden/s);
  assert.match(cssSource, /\.manual-chatgpt-audit-summary-content\s*\{[^}]*display:\s*flex/s);
  assert.match(cssSource, /\.manual-chatgpt-audit-details > summary:focus-visible/);
  assert.match(cssSource, /footer a\.primary:focus-visible/);
  assert.match(cssSource, /@media \(forced-colors: active\)/);
});
