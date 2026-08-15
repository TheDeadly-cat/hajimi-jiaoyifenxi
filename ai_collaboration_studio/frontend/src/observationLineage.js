export function bindableAiProposals(observations, artifactRoundId) {
  const exactRoundId = String(artifactRoundId || "");
  if (!exactRoundId) return [];
  return (observations || []).filter((item) => (
    String(item?.round_id || "") === exactRoundId
    && String(item?.status || "").toUpperCase() === "PROPOSED"
    && item?.user_confirmed !== true
    && String(item?.confidence_source || "").toLowerCase() === "ai"
    && String(item?.created_by || "") !== "user"
  ));
}
