const NAME_CONTINUATION = /[\p{L}\p{N}\p{M}_\-.·]/u;
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/u;
const MEMBER_ID_LIMIT = 120;
const MEMBER_NAME_LIMIT = 120;
const DEFAULT_MENTION_LIMIT = 8;
const MAX_MENTION_LIMIT = 64;

function cleanText(value, limit) {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized && normalized.length <= limit && !CONTROL_CHARACTERS.test(normalized)
    ? normalized
    : null;
}

function positiveVersion(value) {
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

function normalizeMember(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) || value.enabled !== true) return null;
  const id = cleanText(value.id, MEMBER_ID_LIMIT);
  const name = cleanText(value.name, MEMBER_NAME_LIMIT);
  const version = value.version === undefined || value.version === null
    ? 1
    : positiveVersion(value.version);
  return id && name && version !== null ? { id, name, version } : null;
}

function lexicalOrder(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

function nextCodePoint(text, offset) {
  const codePoint = text.codePointAt(offset);
  return codePoint === undefined ? "" : String.fromCodePoint(codePoint);
}

function mentionNamePositions(text, names) {
  const candidates = [...new Set(names)]
    .sort((left, right) => right.length - left.length || lexicalOrder(left, right));
  const positions = new Map();

  for (let offset = text.indexOf("@"); offset >= 0; offset = text.indexOf("@", offset + 1)) {
    const nameStart = offset + 1;
    const matchedName = candidates.find((name) => {
      if (!text.startsWith(name, nameStart)) return false;
      const nextCharacter = nextCodePoint(text, nameStart + name.length);
      return !nextCharacter || !NAME_CONTINUATION.test(nextCharacter);
    });
    if (matchedName && !positions.has(matchedName)) positions.set(matchedName, offset);
  }

  return positions;
}

function normalizeSelectedMention(value, currentById, namePositions) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const memberId = cleanText(value.member_id, MEMBER_ID_LIMIT);
  const name = cleanText(value.name, MEMBER_NAME_LIMIT);
  const expectedMemberVersion = positiveVersion(value.expected_member_version);
  const member = memberId ? currentById.get(memberId) : null;
  if (
    !member
    || !name
    || member.name !== name
    || expectedMemberVersion === null
    || !namePositions.has(name)
  ) return null;
  return {
    member_id: memberId,
    name,
    expected_member_version: expectedMemberVersion,
  };
}

function boundedMentionLimit(value) {
  return Number.isSafeInteger(value)
    ? Math.min(MAX_MENTION_LIMIT, Math.max(0, value))
    : DEFAULT_MENTION_LIMIT;
}

export function resolveComposerMentions(content, members, selectedMentions, limit = DEFAULT_MENTION_LIMIT) {
  const text = typeof content === "string" ? content : "";
  const normalizedMembers = (Array.isArray(members) ? members : [])
    .map(normalizeMember)
    .filter(Boolean);
  const memberIdCounts = new Map();
  normalizedMembers.forEach((member) => {
    memberIdCounts.set(member.id, (memberIdCounts.get(member.id) || 0) + 1);
  });
  const duplicateIdentityNames = new Set(
    normalizedMembers
      .filter((member) => memberIdCounts.get(member.id) > 1)
      .map((member) => member.name),
  );
  const enabledMembers = normalizedMembers.filter((member) => memberIdCounts.get(member.id) === 1);
  const currentById = new Map(enabledMembers.map((member) => [member.id, member]));
  const namePositions = mentionNamePositions(text, normalizedMembers.map((member) => member.name));

  const selectedById = new Map();
  const conflictingSelectedIds = new Set();
  for (const value of Array.isArray(selectedMentions) ? selectedMentions : []) {
    const mention = normalizeSelectedMention(value, currentById, namePositions);
    if (!mention) continue;
    const existing = selectedById.get(mention.member_id);
    if (existing && (
      existing.name !== mention.name
      || existing.expected_member_version !== mention.expected_member_version
    )) {
      conflictingSelectedIds.add(mention.member_id);
      continue;
    }
    if (!existing) selectedById.set(mention.member_id, mention);
  }
  conflictingSelectedIds.forEach((memberId) => selectedById.delete(memberId));

  const selectedByName = new Map();
  for (const mention of selectedById.values()) {
    selectedByName.set(mention.name, [...(selectedByName.get(mention.name) || []), mention]);
  }
  const ambiguousNames = new Set(
    [...duplicateIdentityNames].filter((name) => namePositions.has(name)),
  );
  const selected = [];
  for (const [name, matches] of selectedByName.entries()) {
    if (matches.length === 1) selected.push(matches[0]);
    else ambiguousNames.add(name);
  }

  const selectedIds = new Set(selected.map((mention) => mention.member_id));
  const selectedNames = new Set(selected.map((mention) => mention.name));
  const membersByName = new Map();
  for (const member of enabledMembers) {
    membersByName.set(member.name, [...(membersByName.get(member.name) || []), member]);
  }

  const manual = [];
  for (const [name, matches] of membersByName.entries()) {
    if (selectedNames.has(name) || !namePositions.has(name)) continue;
    if (duplicateIdentityNames.has(name) || matches.length > 1) {
      ambiguousNames.add(name);
      continue;
    }
    const member = matches[0];
    if (selectedIds.has(member.id)) continue;
    manual.push({
      member_id: member.id,
      name,
      expected_member_version: member.version,
    });
  }

  const mentions = [...selected, ...manual]
    .sort((left, right) => (
      namePositions.get(left.name) - namePositions.get(right.name)
      || lexicalOrder(left.member_id, right.member_id)
    ))
    .slice(0, boundedMentionLimit(limit))
    .map(({ member_id, expected_member_version }) => ({ member_id, expected_member_version }));
  return {
    mentions,
    ambiguousNames: [...ambiguousNames].sort((left, right) => (
      namePositions.get(left) - namePositions.get(right) || lexicalOrder(left, right)
    )),
  };
}
