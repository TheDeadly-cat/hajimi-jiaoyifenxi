const LEGACY_STATUSES = new Set([
  "legacy_unverifiable",
  "legacy_verified",
]);


export function walkForwardIntegrityState(run) {
  const status = String(run?.integrity_status || "").trim().toLowerCase();
  const issues = Array.isArray(run?.integrity_issues)
    ? run.integrity_issues.filter((issue) => typeof issue === "string" && issue)
    : [];
  const verified = run?.integrity_ok === true
    && run?.fully_verified === true
    && status === "verified";
  if (verified) {
    return {
      status,
      metricsVisible: true,
      label: "完整性已验证",
      detail: "结果、冻结输入与版本绑定均通过校验。",
    };
  }
  if (
    LEGACY_STATUSES.has(status)
    || issues.includes("WALK_FORWARD_INPUT_SNAPSHOT_LEGACY_UNVERIFIABLE")
  ) {
    return {
      status: status || "legacy_unverifiable",
      metricsVisible: false,
      label: "旧格式未完全可验",
      detail: "该 v1 历史记录缺少当前版本的完整冻结输入验证，收益与充分性指标已隐藏。",
    };
  }
  return {
    status: status || "failed",
    metricsVisible: false,
    label: "完整性校验失败",
    detail: "记录未通过结果、冻结输入或版本绑定校验，收益与充分性指标已隐藏。",
  };
}
