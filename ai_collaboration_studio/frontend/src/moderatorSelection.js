export function isModeratorSelectionMissing(moderatorMemberId, members = []) {
  const selectedId = String(moderatorMemberId || "");
  return Boolean(
    selectedId
      && !members.some((member) => String(member?.id || "") === selectedId),
  );
}
