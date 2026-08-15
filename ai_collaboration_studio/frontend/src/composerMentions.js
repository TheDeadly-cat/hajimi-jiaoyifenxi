const NAME_CONTINUATION = /[\p{L}\p{N}\p{M}_\-.·]/u;

function mentionNamePositions(text, names) {
  const candidates = [...new Set(names.filter(Boolean))]
    .sort((left, right) => right.length - left.length || left.localeCompare(right));
  const positions = new Map();

  for (let offset = text.indexOf("@"); offset >= 0; offset = text.indexOf("@", offset + 1)) {
    const nameStart = offset + 1;
    const matchedName = candidates.find((name) => {
      if (!text.startsWith(name, nameStart)) return false;
      const nextCharacter = text.slice(nameStart + name.length, nameStart + name.length + 1);
      return !nextCharacter || !NAME_CONTINUATION.test(nextCharacter);
    });
    if (matchedName && !positions.has(matchedName)) positions.set(matchedName, offset);
  }

  return positions;
}

export function resolveComposerMentions(content, members, selectedMentions, limit = 8) {
  const text = String(content || "");
  const enabledMembers = (members || []).filter((member) => member.enabled);
  const currentById = new Map(enabledMembers.map((member) => [member.id, member]));
  const memberNames = enabledMembers.map((member) => String(member.name || "")).filter(Boolean);
  const namePositions = mentionNamePositions(text, memberNames);
  const selected = (selectedMentions || [])
    .filter((mention) => {
      const member = currentById.get(mention.member_id);
      return member && namePositions.has(String(mention.name || ""));
    });
  const selectedIds = new Set(selected.map((mention) => mention.member_id));
  const selectedNames = new Set(selected.map((mention) => mention.name));
  const membersByName = new Map();
  for (const member of enabledMembers) {
    const name = String(member.name || "");
    if (!name) continue;
    membersByName.set(name, [...(membersByName.get(name) || []), member]);
  }

  const ambiguousNames = [];
  const manual = [];
  for (const [name, matches] of membersByName.entries()) {
    if (selectedNames.has(name) || !namePositions.has(name)) continue;
    if (matches.length > 1) {
      ambiguousNames.push(name);
      continue;
    }
    const member = matches[0];
    if (selectedIds.has(member.id)) continue;
    manual.push({
      member_id: member.id,
      name,
      expected_member_version: Number(member.version) || 1,
    });
  }

  const mentions = [...selected, ...manual]
    .sort((left, right) => namePositions.get(left.name) - namePositions.get(right.name))
    .slice(0, limit)
    .map(({ member_id, expected_member_version }) => ({ member_id, expected_member_version }));
  return { mentions, ambiguousNames: [...new Set(ambiguousNames)] };
}
