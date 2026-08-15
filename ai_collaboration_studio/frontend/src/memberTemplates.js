const IDENTITY_TEMPLATE_FIELDS = [
  "name",
  "identity",
  "responsibilities",
  "boundaries",
  "instructions",
  "stance",
  "workflow_stage",
  "avatar_color",
];

export function applyMemberTemplate(current, template) {
  if (!template || typeof template !== "object") return current;
  const next = { ...current };
  IDENTITY_TEMPLATE_FIELDS.forEach((field) => {
    if (Object.hasOwn(template, field)) next[field] = template[field];
  });
  next.capabilities = Array.isArray(template.capabilities)
    ? [...template.capabilities]
    : [];
  return next;
}

export function groupMemberTemplates(templates = []) {
  const groups = new Map();
  templates.forEach((template) => {
    const label = String(template?.source_category || template?.source_template_name || "通用角色");
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(template);
  });
  return [...groups.entries()].map(([label, items]) => ({ label, items }));
}
