export const ROUND_CONTEXT_AUTHORIZATION_SET_VERSION = "round_context_authorization_set_v1";
export const ROUND_CONTEXT_AUTHORIZATION_ENTRY_VERSION = "round_context_authorization_entry_v1";

const MAX_ROUND_CONTEXTS = 64;
const PROVIDER_KEY_SEPARATOR = "\u001f";

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function cloneJson(value) {
  return typeof globalThis.structuredClone === "function"
    ? globalThis.structuredClone(value)
    : JSON.parse(JSON.stringify(value));
}

function providerIdentity(value, maximum) {
  const normalized = typeof value === "string" ? value.trim() : "";
  return normalized
    && normalized.length <= maximum
    && !normalized.includes(PROVIDER_KEY_SEPARATOR)
    ? normalized
    : "";
}

export function roundContextAuthorizationEntry(ownerPackId, portId, request) {
  const owner = providerIdentity(ownerPackId, 160);
  const port = providerIdentity(portId, 200);
  if (!owner || !port || !record(request)) {
    throw new TypeError("Round context authorization entry is invalid.");
  }
  return {
    version: ROUND_CONTEXT_AUTHORIZATION_ENTRY_VERSION,
    owner_pack_id: owner,
    port_id: port,
    request: cloneJson(request),
  };
}

export function buildRoundContextAuthorizationSet(entries = []) {
  if (!Array.isArray(entries)) {
    throw new TypeError("Round context authorizations must be an array.");
  }
  if (entries.length > MAX_ROUND_CONTEXTS) {
    throw new TypeError("Round context authorizations exceed the supported capacity.");
  }
  const seen = new Set();
  const contexts = entries.map((raw) => {
    const entry = record(raw);
    if (
      !entry
      || Object.keys(entry).sort().join("|")
        !== "owner_pack_id|port_id|request|version"
      || entry.version !== ROUND_CONTEXT_AUTHORIZATION_ENTRY_VERSION
    ) {
      throw new TypeError("Round context authorization entry has an invalid closed shape.");
    }
    const normalized = roundContextAuthorizationEntry(
      entry.owner_pack_id,
      entry.port_id,
      entry.request,
    );
    const key = `${normalized.owner_pack_id}\u001f${normalized.port_id}`;
    if (seen.has(key)) throw new TypeError("Round context authorization provider is duplicated.");
    seen.add(key);
    return normalized;
  }).sort((left, right) => (
    left.owner_pack_id.localeCompare(right.owner_pack_id)
    || left.port_id.localeCompare(right.port_id)
  ));
  return {
    version: ROUND_CONTEXT_AUTHORIZATION_SET_VERSION,
    contexts,
  };
}

export function normalizeRoundContextAuthorizationSet(value) {
  try {
    const source = record(value);
    if (
      !source
      || Object.keys(source).sort().join("|") !== "contexts|version"
      || source.version !== ROUND_CONTEXT_AUTHORIZATION_SET_VERSION
      || !Array.isArray(source.contexts)
    ) throw new TypeError("Round context authorization set has an invalid closed shape.");
    const normalized = buildRoundContextAuthorizationSet(source.contexts);
    const sourceKeys = source.contexts.map((entry) => (
      `${String(entry?.owner_pack_id || "").trim()}${PROVIDER_KEY_SEPARATOR}`
      + String(entry?.port_id || "").trim()
    ));
    const normalizedKeys = normalized.contexts.map((entry) => (
      `${entry.owner_pack_id}${PROVIDER_KEY_SEPARATOR}${entry.port_id}`
    ));
    if (JSON.stringify(sourceKeys) !== JSON.stringify(normalizedKeys)) {
      throw new TypeError("Round context authorization set is not canonical.");
    }
    return { valid: true, value: normalized, error: "" };
  } catch (error) {
    return {
      valid: false,
      value: buildRoundContextAuthorizationSet([]),
      error: error?.message || "Round context authorization set is invalid.",
    };
  }
}
