import assert from "node:assert/strict";
import test from "node:test";

import {
  buildArtifactEvidenceCandidates,
  evidenceLocatorLabel,
  evidenceMessageId,
  evidencePreviewAllowsVerification,
  evidenceRelationFlags,
  evidenceSourceDetailRequired,
  evidenceSourceIdentityMatches,
  messageDomId,
  normalizeArtifactEvidenceResponse,
  normalizeArtifactEvidenceDetailResponse,
  normalizeArtifactEvidenceSource,
  replyTargetMessageId,
  safeExternalUrl,
  summarizeEvidenceRelations,
} from "../src/artifactEvidenceSources.js";

test("normalizes authoritative v2 material, message, snapshot, and unresolved sources", () => {
  const response = normalizeArtifactEvidenceResponse({
    version: "artifact_evidence_sources_v2",
    artifact_id: "artifact_1",
    round_id: "round_1",
    authoritative: true,
    sources: [
      {
        type: "material",
        source_id: "material_1",
        version: 2,
        status: "superseded",
        exact: true,
        source_identity_exact: true,
        title: "历史公告",
        preview: "v2 原文",
        preview_complete: true,
        source_url: "https://example.com/filing",
        source_meta: {
          kind: "official",
          changed_at: 1785749400000,
          current_version: 4,
          current_active: true,
          metadata: { publisher: "SEC", symbols: ["MU"] },
        },
        locator: { material_id: "material_1", material_version: 2 },
      },
      {
        type: "message",
        id: "message_1",
        version: 3,
        exact: true,
        source_identity_exact: true,
        sender_name: "反证分析师",
        content: "该结论仍有样本偏差。",
        preview_complete: true,
        source_meta: {
          sender_type: "ai",
          identity: "反证分析师",
          member_version: 3,
          statement_scope: "member_statement_only",
        },
        locator: { message_id: "message_1" },
      },
      {
        type: "round_market_snapshot",
        snapshot_id: "snapshot_1",
        exact: true,
        source_identity_exact: true,
        preview: { symbol: "MU", last: 123.45 },
        preview_complete: true,
        source_revision: "market_evidence_v6",
        source_snapshot_sha256: "a".repeat(64),
        source_meta: {
          source: "futu_readonly",
          state: "ready",
          captured_at: "2026-08-03T09:30:00Z",
          execution_capability: "none",
          live_trading_allowed: false,
        },
      },
    ],
    unresolved: [{
      type: "message",
      id: "message_missing",
      status: "unavailable",
      exact: false,
      locator: { message_id: "message_missing" },
    }, {
      type: "material",
      id: "",
      code: "MATERIAL_SOURCE_LIMIT_EXCEEDED",
      message: "冻结资料数量超过响应上限。",
      exact: false,
    }],
  });

  assert.equal(response.authoritative, true);
  assert.equal(response.sources.length, 3);
  assert.equal(response.unresolved.length, 2);
  assert.equal(response.sources[0].id, "material_1");
  assert.equal(response.sources[0].preview, "v2 原文");
  assert.equal(response.sources[0].previewExact, true);
  assert.equal(response.sources[0].previewComplete, true);
  assert.equal(response.sources[0].sourceIdentityExact, true);
  assert.equal(response.sources[0].latestVersion, 4);
  assert.equal(response.sources[0].versionStatus, "superseded");
  assert.equal(response.sources[0].kind, "official");
  assert.equal(response.sources[0].changedAt, 1785749400000);
  assert.deepEqual(response.sources[0].metadata, { publisher: "SEC", symbols: ["MU"] });
  assert.match(response.sources[0].sourceMeta, /最新版 v4/);
  assert.doesNotMatch(response.sources[0].sourceMeta, /\[object Object\]/);
  assert.equal(response.sources[0].sourceUrl, "https://example.com/filing");
  assert.deepEqual(response.sources[1].locator, { message_id: "message_1" });
  assert.match(response.sources[1].sourceMeta, /反证分析师.*仅证明该成员曾作此陈述/);
  assert.equal(response.sources[1].identity, "反证分析师");
  assert.equal(response.sources[1].statementScope, "member_statement_only");
  assert.doesNotMatch(response.sources[1].sourceMeta, /\[object Object\]/);
  assert.match(response.sources[2].preview, /"symbol": "MU"/);
  assert.match(response.sources[2].sourceMeta, /futu_readonly.*无执行能力/);
  assert.equal(response.sources[2].executionCapability, "none");
  assert.equal(response.sources[2].liveTradingAllowed, false);
  assert.doesNotMatch(response.sources[2].sourceMeta, /\[object Object\]/);
  assert.equal(response.unresolved[0].selectable, false);
  assert.equal(response.unresolved[0].unresolved, true);
  assert.match(response.unresolved[1].id, /^gap_material_source_limit_exceeded_/);
  assert.equal(response.unresolved[1].sourceMeta, "冻结资料数量超过响应上限。");
});

test("legacy source arrays remain parseable but fail closed without exactness flags", () => {
  const response = normalizeArtifactEvidenceResponse([{
    type: "round_market_snapshot",
    id: "snapshot_legacy",
    payload: { evidence_version: "v1" },
  }], { roundId: "round_legacy" });

  assert.equal(response.version, "artifact_evidence_sources_v1");
  assert.equal(response.authoritative, true);
  assert.equal(response.sources[0].round_id, "round_legacy");
  assert.equal(response.sources[0].exact, false);
  assert.equal(response.sources[0].previewComplete, false);
  assert.equal(response.sources[0].selectable, false);
});

test("round-bound candidates use only authoritative API results", () => {
  const apiEvidence = normalizeArtifactEvidenceResponse({
    round_id: "round_1",
    authoritative: true,
    sources: [{
      type: "message",
      id: "message_round",
      exact: true,
      preview: "本轮发言",
      preview_complete: true,
    }],
    unresolved: [{ type: "material", id: "material_gap", exact: false }],
  });
  const candidates = buildArtifactEvidenceCandidates({
    roundId: "round_1",
    apiEvidence,
    materials: [{ id: "material_room", title: "房间当前资料", content: "不得混入" }],
    messages: [{ id: "message_other", content: "非本轮消息" }],
    referencedEvidence: [{ type: "message", id: "message_unknown" }],
  });

  assert.deepEqual(candidates.map((source) => source.id), ["message_round", "material_gap"]);
  assert.equal(candidates.some((source) => source.id === "material_room"), false);
  assert.equal(candidates.some((source) => source.id === "message_other"), false);
  assert.equal(candidates.some((source) => source.id === "message_unknown"), false);
});

test("non-round drafts may use current material and message props", () => {
  const candidates = buildArtifactEvidenceCandidates({
    materials: [{ id: "material_current", version: 2, title: "当前资料", content: "原文" }],
    messages: [{ id: "message_current", member_version: 4, sender_name: "研究员", content: "当前草稿发言" }],
  });

  assert.deepEqual(candidates.map((source) => source.type), ["material", "message"]);
  assert.equal(candidates[0].exact, true);
  assert.equal(candidates[0].previewComplete, true);
  assert.equal(candidates[1].version, 4);
});

test("relation status deterministically exposes support, counter, conflict, and gaps", () => {
  const candidates = [
    { type: "message", id: "support", exact: true, previewComplete: true, status: "available" },
    { type: "message", id: "counter", exact: true, previewComplete: true, status: "available" },
    { type: "material", id: "gap", exact: false, status: "missing", unresolved: true },
  ];
  const reviews = {
    "message:support": { evidence_role: "support", verification_status: "source_checked", version: 0 },
    "message:counter": { evidence_role: "counter", verification_status: "disputed", version: 0 },
  };
  const summary = summarizeEvidenceRelations(
    candidates,
    ["message:support", "message:counter", "material:gap"],
    reviews,
  );

  assert.deepEqual(summary, { selected: 3, support: 1, counter: 1, conflict: 1, gap: 1 });
  assert.deepEqual(evidenceRelationFlags(candidates[1], reviews["message:counter"]), {
    support: false,
    counter: true,
    conflict: true,
    gap: false,
  });
});

test("message locators are stable and external links allow only http or https", () => {
  assert.equal(messageDomId("message/一"), "message-message%2F%E4%B8%80");
  assert.equal(evidenceMessageId({ type: "message", locator: { message_id: "message_1" } }), "message_1");
  assert.equal(evidenceMessageId({ type: "material", id: "message_1" }), "");
  assert.equal(replyTargetMessageId({ reply_to: "分析师", reply_to_message_id: "message_1" }), "message_1");
  assert.equal(replyTargetMessageId({ reply_to: "message_looks_like_id" }), "");
  assert.equal(evidenceLocatorLabel({ material_id: "material_1", material_version: 7 }), "资料 material_1 · v7");
  assert.equal(safeExternalUrl("https://example.com/source"), "https://example.com/source");
  assert.equal(safeExternalUrl("http://example.com"), "http://example.com/");
  assert.equal(
    safeExternalUrl("https://user:pass@example.com/source?api_key=secret#fragment"),
    "https://example.com/source?api_key=%5BREDACTED%5D",
  );
  const sensitiveUrl = safeExternalUrl(
    "https://example.com/source?token=TOPSECRET&auth=AUTHSECRET&key=KEYSECRET"
      + "&sig=SIGSECRET&session=SESSIONSECRET&jwt=JWTSECRET&code=CODESECRET"
      + "&X-Amz-Signature=AWSSECRET&monkey=banana&tokenization=model"
      + "&postcode=100000&codec=h264&sessionize=true",
  );
  assert.doesNotMatch(
    sensitiveUrl,
    /TOPSECRET|AUTHSECRET|KEYSECRET|SIGSECRET|SESSIONSECRET|JWTSECRET|CODESECRET|AWSSECRET/,
  );
  assert.match(sensitiveUrl, /token=%5BREDACTED%5D/);
  assert.match(sensitiveUrl, /auth=%5BREDACTED%5D/);
  assert.match(sensitiveUrl, /key=%5BREDACTED%5D/);
  assert.match(sensitiveUrl, /sig=%5BREDACTED%5D/);
  assert.match(sensitiveUrl, /session=%5BREDACTED%5D/);
  assert.match(sensitiveUrl, /jwt=%5BREDACTED%5D/);
  assert.match(sensitiveUrl, /code=%5BREDACTED%5D/);
  assert.match(sensitiveUrl, /X-Amz-Signature=%5BREDACTED%5D/);
  assert.match(sensitiveUrl, /monkey=banana/);
  assert.match(sensitiveUrl, /tokenization=model/);
  assert.match(sensitiveUrl, /postcode=100000/);
  assert.match(sensitiveUrl, /codec=h264/);
  assert.match(sensitiveUrl, /sessionize=true/);
  assert.equal(safeExternalUrl("javascript:alert(1)"), "");
  assert.equal(safeExternalUrl("file:///C:/secret.txt"), "");
});

test("preview completeness is distinct from exact source identity and fails closed", () => {
  const truncated = normalizeArtifactEvidenceSource({
    type: "message",
    id: "message_truncated",
    version: 3,
    exact: true,
    preview: "截断内容",
    preview_complete: true,
    preview_truncated: true,
    source_url: "https://example.com/full",
  });
  const redacted = normalizeArtifactEvidenceSource({
    type: "message",
    id: "message_redacted",
    version: 3,
    source_identity_exact: true,
    preview: "token=[REDACTED]",
    preview_exact: true,
    preview_redacted: true,
  });
  const emptyFromBudget = normalizeArtifactEvidenceSource({
    type: "round_market_snapshot",
    id: "snapshot_budget",
    exact: true,
    preview: "",
    preview_exact: true,
    preview_budget_exhausted: true,
    source_url: "https://example.com/full-snapshot",
  });
  const complete = normalizeArtifactEvidenceSource({
    type: "message",
    id: "message_complete",
    version: 3,
    exact: true,
    preview: "完整短内容",
    preview_exact: true,
  });

  for (const source of [truncated, redacted, emptyFromBudget]) {
    assert.equal(source.sourceIdentityExact, true);
    assert.equal(source.previewComplete, false);
    assert.equal(source.previewExact, false);
    assert.equal(evidenceRelationFlags(source, {
      evidence_role: "support",
      verification_status: "source_checked",
    }).gap, true);
  }
  assert.equal(complete.previewComplete, true);
  assert.equal(evidencePreviewAllowsVerification(complete, { version: 3 }), true);
  assert.equal(evidenceRelationFlags(complete, {
    evidence_role: "support",
    verification_status: "source_checked",
    version: 3,
  }).gap, false);
  assert.equal(evidencePreviewAllowsVerification(truncated, { version: 3 }), false);
  assert.equal(evidencePreviewAllowsVerification(emptyFromBudget, { version: 0 }), false);
});

test("authoritative identity matching includes frozen material and message versions", () => {
  const material = {
    type: "material",
    id: "material_1",
    version: 1,
    exact: true,
    sourceIdentityExact: true,
  };
  const message = {
    type: "message",
    id: "message_1",
    version: 3,
    memberVersion: 3,
    exact: true,
    sourceIdentityExact: true,
  };

  assert.equal(evidenceSourceIdentityMatches(material, { version: 1 }), true);
  assert.equal(evidenceSourceIdentityMatches(material, { version: 2 }), false);
  assert.equal(evidenceSourceIdentityMatches(message, { version: 3 }), true);
  assert.equal(evidenceSourceIdentityMatches(message, { version: 4 }), false);
  assert.equal(evidenceSourceIdentityMatches(message, {}), false);
  assert.equal(evidencePreviewAllowsVerification({
    ...material,
    previewComplete: false,
  }, { version: 1 }, {
    status: "ready",
    type: "material",
    id: "material_1",
    version: 1,
    exact: true,
    sourceIdentityExact: true,
    preview: "完整历史原文",
    previewComplete: true,
    previewTruncated: false,
    previewRedacted: false,
  }), true);
  assert.equal(evidencePreviewAllowsVerification({
    ...material,
    previewComplete: false,
  }, { version: 2 }, {
    status: "ready",
    type: "material",
    id: "material_1",
    version: 2,
    exact: true,
    sourceIdentityExact: true,
    preview: "不是冻结版本",
    previewComplete: true,
  }), false);
});

test("normalizes artifact-bound full frozen source detail before verification", () => {
  const envelope = normalizeArtifactEvidenceDetailResponse({
    version: "artifact_evidence_source_detail_v1",
    artifact_id: "artifact_1",
    round_id: "round_1",
    authoritative: true,
    source: {
      type: "round_market_snapshot",
      id: "snapshot_1",
      snapshot_id: "snapshot_1",
      round_id: "round_1",
      exact: true,
      source_identity_exact: true,
      preview: "完整冻结市场快照",
      preview_complete: true,
      preview_exact: true,
      preview_truncated: false,
      preview_redacted: false,
      preview_budget_exhausted: false,
      detail_bytes: new TextEncoder().encode("完整冻结市场快照").length,
      detail_max_bytes: 300 * 1024,
      source_revision: "storage_market_evidence_v6",
      source_snapshot_sha256: "a".repeat(64),
    },
  }, {
    artifactId: "artifact_1",
    roundId: "round_1",
    type: "round_market_snapshot",
    id: "snapshot_1",
  });

  assert.ok(envelope);
  assert.equal(envelope.source.previewComplete, true);
  assert.equal(evidenceSourceDetailRequired({
    ...envelope.source,
    previewComplete: false,
  }, { version: 0 }), true);
  assert.equal(evidencePreviewAllowsVerification({
    ...envelope.source,
    previewComplete: false,
  }, { version: 0 }, { ...envelope.source, status: "ready" }), true);
  assert.equal(evidenceRelationFlags({
    ...envelope.source,
    previewComplete: false,
  }, { version: 0 }, { ...envelope.source, status: "ready" }).gap, false);
  assert.equal(normalizeArtifactEvidenceDetailResponse({
    ...envelope,
    artifact_id: "artifact_other",
    source: envelope.source,
  }, { artifactId: "artifact_1" }), null);

  const redacted = normalizeArtifactEvidenceDetailResponse({
    version: "artifact_evidence_source_detail_v1",
    artifact_id: "artifact_1",
    round_id: "round_1",
    authoritative: true,
    source: {
      type: "message",
      id: "message_1",
      round_id: "round_1",
      version: 3,
      exact: true,
      source_identity_exact: true,
      preview: "OPENAI_API_KEY=[REDACTED]",
      preview_complete: false,
      preview_exact: false,
      preview_redacted: true,
      detail_bytes: new TextEncoder().encode("OPENAI_API_KEY=[REDACTED]").length,
      detail_max_bytes: 300 * 1024,
    },
  }, {
    artifactId: "artifact_1",
    roundId: "round_1",
    type: "message",
    id: "message_1",
  });
  assert.ok(redacted);
  assert.equal(redacted.source.previewComplete, false);
  assert.equal(evidencePreviewAllowsVerification(
    { ...redacted.source, previewComplete: false },
    { version: 3 },
    { ...redacted.source, status: "ready" },
  ), false);
});
