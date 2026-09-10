export const SOURCE_INBOX_EVENT_QUERY_KEY = "source_event";

const SOURCE_INBOX_EVENT_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
const FALLBACK_URL = "http://localhost/";

function locationUrl(locationLike) {
  let url;
  if (locationLike instanceof URL) url = new URL(locationLike.href);
  else if (typeof locationLike === "string" && locationLike) {
    url = new URL(locationLike, FALLBACK_URL);
  } else if (locationLike && typeof locationLike.href === "string" && locationLike.href) {
    url = new URL(locationLike.href, FALLBACK_URL);
  } else {
    throw new TypeError("A URL or location with an href is required.");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new TypeError("Source Inbox deep links require an HTTP(S) location.");
  }
  return url;
}

export function isSourceInboxEventId(value) {
  return typeof value === "string" && SOURCE_INBOX_EVENT_ID_RE.test(value);
}

export function parseSourceInboxDeepLink(locationLike) {
  let url;
  try {
    url = locationUrl(locationLike);
  } catch {
    return "";
  }
  const values = url.searchParams.getAll(SOURCE_INBOX_EVENT_QUERY_KEY);
  if (values.length !== 1 || !isSourceInboxEventId(values[0])) return "";
  return values[0];
}

export function buildSourceInboxDeepLink(locationLike, eventId = "") {
  const url = locationUrl(locationLike);
  if (eventId !== "" && !isSourceInboxEventId(eventId)) {
    throw new TypeError("source_event is not a valid Source Inbox event id.");
  }
  url.searchParams.delete(SOURCE_INBOX_EVENT_QUERY_KEY);
  if (eventId) url.searchParams.set(SOURCE_INBOX_EVENT_QUERY_KEY, eventId);
  return `${url.pathname}${url.search}${url.hash}`;
}

export function updateSourceInboxDeepLink({
  eventId = "",
  historyObject = globalThis.history,
  locationLike = globalThis.location,
  mode = "replace",
} = {}) {
  if (mode !== "push" && mode !== "replace") {
    throw new TypeError("Deep-link history mode must be push or replace.");
  }
  const method = mode === "push" ? "pushState" : "replaceState";
  if (!historyObject || typeof historyObject[method] !== "function") {
    throw new TypeError(`History does not support ${method}.`);
  }
  const target = buildSourceInboxDeepLink(locationLike, eventId);
  historyObject[method](historyObject.state ?? null, "", target);
  return target;
}
