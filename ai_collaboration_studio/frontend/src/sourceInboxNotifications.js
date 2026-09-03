import { isSourceInboxEventId } from "./sourceInboxDeepLink.js";

export const SOURCE_INBOX_NOTIFICATION_PREFERENCE_KEY = "ai_studio_source_inbox_notifications_v1";
export const SOURCE_INBOX_NOTIFICATION_PREFERENCE_VERSION = "source_inbox_browser_notifications_v1";
export const SOURCE_INBOX_NOTIFICATION_TITLE = "AI 共创室";
export const SOURCE_INBOX_NOTIFICATION_BODY = "来源收件箱有新的待审阅外部信息。";

const KNOWN_PERMISSIONS = new Set(["default", "denied", "granted"]);

function browserStorage(storage) {
  if (storage !== undefined) return storage;
  try {
    return globalThis.localStorage;
  } catch {
    return null;
  }
}

function notificationPermission(notificationApi) {
  try {
    return KNOWN_PERMISSIONS.has(notificationApi?.permission)
      ? notificationApi.permission
      : "default";
  } catch {
    return "default";
  }
}

export function sourceInboxNotificationCapability(
  notificationApi = globalThis.Notification,
) {
  const supported = typeof notificationApi === "function";
  return {
    supported,
    permission: supported ? notificationPermission(notificationApi) : "unsupported",
  };
}

export function readSourceInboxNotificationPreference(
  storage,
) {
  try {
    const target = browserStorage(storage);
    const parsed = JSON.parse(target?.getItem(SOURCE_INBOX_NOTIFICATION_PREFERENCE_KEY) || "null");
    return parsed?.version === SOURCE_INBOX_NOTIFICATION_PREFERENCE_VERSION
      && parsed.enabled === true;
  } catch {
    return false;
  }
}

export function writeSourceInboxNotificationPreference(
  enabled,
  storage,
) {
  if (typeof enabled !== "boolean") {
    throw new TypeError("Notification preference must be a boolean.");
  }
  try {
    const target = browserStorage(storage);
    if (!target || typeof target.setItem !== "function") return false;
    target.setItem(SOURCE_INBOX_NOTIFICATION_PREFERENCE_KEY, JSON.stringify({
      version: SOURCE_INBOX_NOTIFICATION_PREFERENCE_VERSION,
      enabled,
    }));
    return true;
  } catch {
    return false;
  }
}

// This function is intentionally the only permission-requesting path. Call it
// directly from an explicit user activation such as a settings button click.
export async function requestSourceInboxNotificationPermissionFromUserGesture(
  notificationApi = globalThis.Notification,
) {
  const capability = sourceInboxNotificationCapability(notificationApi);
  if (!capability.supported || typeof notificationApi.requestPermission !== "function") {
    return "unsupported";
  }
  try {
    const result = await notificationApi.requestPermission();
    return KNOWN_PERMISSIONS.has(result) ? result : notificationPermission(notificationApi);
  } catch {
    return notificationPermission(notificationApi);
  }
}

export function createSourceInboxNotification({
  eventId,
  notificationApi = globalThis.Notification,
  onOpen,
  windowObject = globalThis.window,
} = {}) {
  const capability = sourceInboxNotificationCapability(notificationApi);
  if (
    !capability.supported
    || capability.permission !== "granted"
    || !isSourceInboxEventId(eventId)
  ) return null;

  let notification;
  try {
    notification = new notificationApi(SOURCE_INBOX_NOTIFICATION_TITLE, {
      body: SOURCE_INBOX_NOTIFICATION_BODY,
      renotify: false,
      silent: true,
      tag: "ai-studio-source-inbox",
    });
  } catch {
    return null;
  }

  notification.onclick = () => {
    try {
      notification.close?.();
    } catch {
      // Notification dismissal is best-effort and must not block navigation.
    }
    try {
      windowObject?.focus?.();
    } catch {
      // Window focus can be denied by the browser.
    }
    try {
      if (typeof onOpen === "function") onOpen(eventId);
    } catch {
      // Notification callbacks must not surface errors into the polling loop.
    }
  };
  return notification;
}
