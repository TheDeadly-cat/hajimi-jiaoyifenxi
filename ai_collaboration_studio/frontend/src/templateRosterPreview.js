const DEFAULT_AVATAR_COLOR = "#64748b";
const HEX_COLOR_PATTERN = /^#[0-9a-f]{6}$/i;

function cleanText(value) {
  return typeof value === "string" ? value.trim() : "";
}

function cleanCount(value) {
  if (typeof value === "boolean" || value === null || value === undefined) return null;
  if (typeof value === "string" && !value.trim()) return null;
  const count = Number(value);
  return Number.isInteger(count) && count >= 0 ? count : null;
}

function cleanCapabilities(value) {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map(cleanText).filter(Boolean))];
}

function cleanMember(value, index) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const name = cleanText(value.name) || "未命名成员";
  const identity = cleanText(value.identity) || "身份定位未说明";
  const avatarColor = cleanText(value.avatar_color);
  return {
    key: `${name}:${identity}:${index}`,
    name,
    identity,
    responsibilities: cleanText(value.responsibilities),
    boundaries: cleanText(value.boundaries),
    stance: cleanText(value.stance),
    workflowStage: cleanText(value.workflow_stage),
    capabilities: cleanCapabilities(value.capabilities),
    avatarColor: HEX_COLOR_PATTERN.test(avatarColor) ? avatarColor : DEFAULT_AVATAR_COLOR,
  };
}

export function templateRosterPreview(template) {
  if (!template || typeof template !== "object" || Array.isArray(template)) {
    return {
      available: false,
      previewAvailable: false,
      count: null,
      members: [],
      partial: false,
    };
  }

  const declaredCount = cleanCount(template.member_count);
  const previewAvailable = Array.isArray(template.member_preview);
  const members = previewAvailable
    ? template.member_preview
        .map(cleanMember)
        .filter(Boolean)
    : [];
  const available = declaredCount !== null || previewAvailable;
  const count = available ? Math.max(declaredCount ?? members.length, members.length) : null;

  return {
    available,
    previewAvailable,
    count,
    members,
    partial: count !== null && count > members.length,
  };
}
