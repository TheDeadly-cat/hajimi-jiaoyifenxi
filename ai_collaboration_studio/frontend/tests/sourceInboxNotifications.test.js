import assert from "node:assert/strict";
import test from "node:test";

import {
  createSourceInboxNotification,
  readSourceInboxNotificationPreference,
  requestSourceInboxNotificationPermissionFromUserGesture,
  SOURCE_INBOX_NOTIFICATION_BODY,
  SOURCE_INBOX_NOTIFICATION_PREFERENCE_KEY,
  SOURCE_INBOX_NOTIFICATION_PREFERENCE_VERSION,
  SOURCE_INBOX_NOTIFICATION_TITLE,
  sourceInboxNotificationCapability,
  writeSourceInboxNotificationPreference,
} from "../src/sourceInboxNotifications.js";

function memoryStorage(initial = new Map()) {
  return {
    values: initial,
    getItem(key) { return this.values.get(key) ?? null; },
    setItem(key, value) { this.values.set(key, value); },
  };
}

function fakeNotifications(permission = "default") {
  const created = [];
  class FakeNotification {
    static permission = permission;
    static requestCalls = 0;
    static async requestPermission() {
      FakeNotification.requestCalls += 1;
      return FakeNotification.permission;
    }

    constructor(title, options) {
      this.title = title;
      this.options = options;
      this.closed = false;
      created.push(this);
    }

    close() { this.closed = true; }
  }
  return { created, NotificationApi: FakeNotification };
}

test("notification capability and versioned preference reads fail closed", () => {
  assert.deepEqual(sourceInboxNotificationCapability(undefined), {
    supported: false,
    permission: "unsupported",
  });
  const { NotificationApi } = fakeNotifications("denied");
  assert.deepEqual(sourceInboxNotificationCapability(NotificationApi), {
    supported: true,
    permission: "denied",
  });

  const storage = memoryStorage();
  assert.equal(readSourceInboxNotificationPreference(storage), false);
  assert.equal(writeSourceInboxNotificationPreference(true, storage), true);
  assert.deepEqual(JSON.parse(storage.getItem(SOURCE_INBOX_NOTIFICATION_PREFERENCE_KEY)), {
    version: SOURCE_INBOX_NOTIFICATION_PREFERENCE_VERSION,
    enabled: true,
  });
  assert.equal(readSourceInboxNotificationPreference(storage), true);
  storage.setItem(SOURCE_INBOX_NOTIFICATION_PREFERENCE_KEY, "{bad json");
  assert.equal(readSourceInboxNotificationPreference(storage), false);
  assert.throws(() => writeSourceInboxNotificationPreference("true", storage), /must be a boolean/);
  assert.equal(writeSourceInboxNotificationPreference(true, {}), false);
});

test("permission is requested only through the explicit user-gesture helper", async () => {
  const { NotificationApi } = fakeNotifications("granted");
  const storage = memoryStorage();
  sourceInboxNotificationCapability(NotificationApi);
  readSourceInboxNotificationPreference(storage);
  createSourceInboxNotification({ eventId: "event.one", notificationApi: NotificationApi });
  assert.equal(NotificationApi.requestCalls, 0);

  assert.equal(
    await requestSourceInboxNotificationPermissionFromUserGesture(NotificationApi),
    "granted",
  );
  assert.equal(NotificationApi.requestCalls, 1);
  assert.equal(await requestSourceInboxNotificationPermissionFromUserGesture(undefined), "unsupported");
});

test("generic notifications expose no event content and open only the validated id", () => {
  const { created, NotificationApi } = fakeNotifications("granted");
  const opened = [];
  let focusCalls = 0;
  const notification = createSourceInboxNotification({
    eventId: "source_item.one",
    notificationApi: NotificationApi,
    onOpen: (eventId) => opened.push(eventId),
    windowObject: { focus() { focusCalls += 1; } },
  });

  assert.equal(created.length, 1);
  assert.equal(notification.title, SOURCE_INBOX_NOTIFICATION_TITLE);
  assert.deepEqual(notification.options, {
    body: SOURCE_INBOX_NOTIFICATION_BODY,
    renotify: false,
    silent: true,
    tag: "ai-studio-source-inbox",
  });
  assert.doesNotMatch(JSON.stringify(notification.options), /source_item|headline|summary|https?:/i);
  notification.onclick();
  assert.equal(notification.closed, true);
  assert.equal(focusCalls, 1);
  assert.deepEqual(opened, ["source_item.one"]);

  assert.equal(createSourceInboxNotification({
    eventId: "source/item",
    notificationApi: NotificationApi,
  }), null);
  const denied = fakeNotifications("denied");
  assert.equal(createSourceInboxNotification({
    eventId: "source_item.two",
    notificationApi: denied.NotificationApi,
  }), null);
  assert.equal(denied.created.length, 0);
});
