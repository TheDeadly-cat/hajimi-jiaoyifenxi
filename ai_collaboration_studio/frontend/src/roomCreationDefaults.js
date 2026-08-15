export function roomCreationCapabilityPackIds(template) {
  const creationDefaults = template?.creation_default_capability_pack_ids;
  const templateDefaults = template?.capability_pack_ids;
  const selected = Array.isArray(creationDefaults)
    ? creationDefaults
    : (Array.isArray(templateDefaults) ? templateDefaults : []);
  return [...new Set(selected.map((value) => String(value || "").trim()).filter(Boolean))];
}
