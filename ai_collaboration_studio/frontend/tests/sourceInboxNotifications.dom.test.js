import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import test from "node:test";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";

const h = React.createElement;
const originalNotification = Object.getOwnPropertyDescriptor(globalThis, "Notification");
let permissionRequests = 0;
const dom = new JSDOM("<!doctype html><html><body><div id=host></div></body></html>", {
  pretendToBeVisual: true,
  url: "http://notification-settings.test/",
});
Object.defineProperties(globalThis, {
  window: { configurable: true, value: dom.window },
  document: { configurable: true, value: dom.window.document },
  navigator: { configurable: true, value: dom.window.navigator },
  HTMLElement: { configurable: true, value: dom.window.HTMLElement },
  Notification: {
    configurable: true,
    value: Object.assign(function UnexpectedNotification() {
      assert.fail("the settings component must not create notifications");
    }, {
      requestPermission() { permissionRequests += 1; return Promise.resolve("granted"); },
    }),
  },
});
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const frontendRoot = fileURLToPath(new URL("../", import.meta.url));
const vite = await createServer({
  appType: "custom",
  logLevel: "silent",
  root: frontendRoot,
  server: { hmr: false, middlewareMode: true },
});
const { createRoot } = await import("react-dom/client");
const { SourceInboxNotifications } = await vite.ssrLoadModule("/src/components/SourceInboxNotifications.jsx");
const host = document.getElementById("host");
const root = createRoot(host);
let instance = 0;

async function render(notificationState) {
  const choices = [];
  await act(async () => {
    root.render(h(SourceInboxNotifications, {
      key: ++instance,
      notificationState,
      onNotificationPreferenceChange(enabled) { choices.push(enabled); },
    }));
  });
  return { choices, button: host.querySelector("button") };
}

test.afterEach(() => {
  assert.equal(permissionRequests, 0, "only the app's explicit user-gesture handler may request permission");
});

test.after(async () => {
  await act(async () => root.unmount());
  await vite.close();
  dom.window.close();
  if (originalNotification) Object.defineProperty(globalThis, "Notification", originalNotification);
  else delete globalThis.Notification;
});

for (const scenario of [
  { name: "default permission", supported: true, permission: "default", enabled: false, label: "启用桌面提醒", disabled: false, choice: true },
  { name: "granted permission with application preference off", supported: true, permission: "granted", enabled: false, label: "启用桌面提醒", disabled: false, choice: true },
  { name: "granted permission with application preference on", supported: true, permission: "granted", enabled: true, label: "关闭桌面提醒", disabled: false, choice: false },
  { name: "denied permission", supported: true, permission: "denied", enabled: false, label: "已被浏览器阻止", disabled: true },
  { name: "unsupported browser", supported: false, permission: "unsupported", enabled: false, label: "浏览器不支持", disabled: true },
]) {
  test(`notification setting preserves explicit user choice for ${scenario.name}`, async () => {
    const { button, choices } = await render(scenario);
    assert.equal(button.textContent, scenario.label);
    assert.equal(button.disabled, scenario.disabled);
    assert.deepEqual(choices, [], "mounting the setting cannot change the preference");
    assert.equal(button.closest("details"), null, "the switch must remain visible without expanding help");
    await act(async () => button.click());
    assert.deepEqual(choices, scenario.disabled ? [] : [scenario.choice]);
    if (scenario.permission === "denied") assert.match(host.textContent, /本站点权限.*允许通知.*刷新页面/);
    if (!scenario.supported) assert.match(host.textContent, /来源收件箱查看全部未读消息/);
  });
}

test("collapsed help exposes notification limits without requesting permission", async () => {
  const { choices } = await render({ supported: true, permission: "default", enabled: false });
  const help = host.querySelector("details");
  const summary = help.querySelector("summary");
  assert.equal(help.open, false);
  assert.equal(summary.textContent, "提醒设置说明");
  await act(async () => summary.click());
  assert.equal(help.open, true);
  assert.match(help.textContent, /浏览器权限和应用开关都启用后/);
  assert.match(help.textContent, /历史未读不会补发/);
  assert.match(help.textContent, /不会立即产生测试提醒/);
  assert.match(help.textContent, /关闭页面后不再推送/);
  assert.match(help.textContent, /应用无法确认系统弹窗是否出现/);
  assert.doesNotMatch(help.textContent, /下次触发.*申请|自动授权|授权成功|已送达 Windows/);
  assert.deepEqual(choices, []);
});

test("an inconsistent enabled preference never promises deferred automatic permission", async () => {
  const { button, choices } = await render({ supported: true, permission: "default", enabled: true });
  assert.equal(host.querySelector(".source-inbox-notify-status").textContent, "未启用");
  assert.match(host.textContent, /尚未取得浏览器权限/);
  assert.match(host.textContent, /先关闭提醒，再重新启用/);
  assert.doesNotMatch(host.textContent, /下次触发.*申请|自动授权|授权成功/);
  assert.equal(button.textContent, "关闭桌面提醒");
  await act(async () => button.click());
  assert.deepEqual(choices, [false]);
});
