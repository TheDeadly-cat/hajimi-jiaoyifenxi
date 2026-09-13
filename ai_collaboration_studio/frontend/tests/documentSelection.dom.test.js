import assert from "node:assert/strict";
import test from "node:test";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";
import { fileURLToPath } from "node:url";

const dom = new JSDOM("<!doctype html><html><body></body></html>", { url: "http://selection.test/" });
for (const key of ["window", "document", "navigator", "HTMLElement", "Event", "MouseEvent", "Node"]) {
  Object.defineProperty(globalThis, key, { configurable: true, value: key === "window" ? dom.window : dom.window[key] });
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), appType: "custom", logLevel: "silent", server: { middlewareMode: true, hmr: false } });
const { createRoot } = await import("react-dom/client");
const { DocumentSelection } = await vite.ssrLoadModule("/src/components/DocumentSelection.jsx");
const { DocumentEvidence } = await vite.ssrLoadModule("/src/components/DocumentEvidence.jsx");
const item = { id: "source_item_fixture", stateVersion: 2, serverFingerprint: "a".repeat(64), acknowledged: true };
const version = { id: "document_old", item_id: item.id, item_fingerprint: item.serverFingerprint,
  fetched_at: 1788868800000, body_located: true, scope: "Fixture only", warnings: ["附件未读取"],
  paragraphs: [{ id: "document_old:p0001", text: "Opening paragraph" }, { id: "document_old:p0002", text: "TAIL RISK <img src=x>" }] };
const rooms = [{ id: "room_one", title: "研究房间一" }, { id: "room_two", title: "研究房间二" }];
const preview = (payload, overrides = {}) => ({ room_id: payload.room_id, room_title: "研究房间一", document_version_id: payload.document_version_id,
  total_paragraphs: 2, selected_paragraphs: 1, omitted_paragraphs: 1, selected_characters: 21,
  packaged_characters: 900, package_characters: 900, excerpt_limit: 1600, fits: true, acknowledged: true,
  can_save: true, content: "Exact selected text and references: TAIL RISK <img src=x>",
  source_url: "https://investors.micron.com/fixture", preview_sha256: "c".repeat(64), ...overrides });
let root, host;
const originalFetch = globalThis.fetch;
async function mount(props = {}, Component = DocumentSelection) {
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  const render = async (extra = {}) => { await act(async () => root.render(React.createElement(Component,
    { item, version, rooms, roomId: "room_one", onRoomChange() {}, ...props, ...extra }))); };
  await render(); return render;
}
async function click(target) { assert.ok(target); await act(async () => target.click()); }
const button = (text) => [...host.querySelectorAll("button")].find((node) => node.textContent === text);
async function chooseAndPreview() {
  await click(host.querySelector('[aria-label="选择段落 2"]'));
  await click(button("预览选段与研究包范围"));
}
test.afterEach(async () => { if (root) await act(async () => root.unmount()); root = null; document.body.replaceChildren(); globalThis.fetch = originalFetch; });
test.after(async () => { await vite.close(); dom.window.close(); });

test("opening chooses nothing and only an explicit preview and confirmation can save", async () => {
  const calls = [];
  globalThis.fetch = async (path, options) => {
    const payload = JSON.parse(options.body); calls.push({ path, payload });
    return { ok: true, json: async () => path.endsWith("/preview") ? { ok: true, selection: preview(payload) } : { ok: true, material: { active: true } } };
  };
  await mount(); assert.equal(calls.length, 0);
  assert.equal(host.querySelectorAll('input:checked').length, 0);
  assert.equal(button("预览选段与研究包范围").disabled, true);
  await chooseAndPreview();
  assert.equal(button("确认加入研究房间").disabled, true);
  assert.match(host.textContent, /未选/);
  assert.equal(host.querySelector("pre").textContent, preview({}).content);
  assert.equal(host.querySelector("img"), null);
  await click(host.querySelector(".document-selection-confirm input"));
  await click(button("确认加入研究房间"));
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[0].payload.paragraph_ids, ["document_old:p0002"]);
  assert.deepEqual(Object.keys(calls[1].payload).sort(), ["confirmation", "document_version_id", "expected_state_version", "paragraph_ids", "preview_sha256", "room_id"].sort());
  assert.match(host.textContent, /选段已保存/);
  assert.ok(calls.every(({ path }) => path.includes("/document/selection")));
});

test("oversized packaging and unread gate clearly disable saving", async () => {
  globalThis.fetch = async (_path, options) => ({ ok: true, json: async () => ({ selection: preview(JSON.parse(options.body), {
    fits: false, acknowledged: false, can_save: false, packaged_characters: 1800, package_characters: 0,
  }) }) });
  await mount(); await chooseAndPreview();
  assert.match(host.textContent, /超过 1,600 字符/);
  assert.match(host.textContent, /点击“记录已阅”/);
  assert.equal(button("确认加入研究房间").disabled, true);
  assert.equal(host.querySelector(".document-selection-confirm input").disabled, true);
});

test("changing selection or room invalidates the previous preview and confirmation", async () => {
  globalThis.fetch = async (_path, options) => ({ ok: true, json: async () => ({ selection: preview(JSON.parse(options.body)) }) });
  const render = await mount(); await chooseAndPreview();
  await click(host.querySelector(".document-selection-confirm input"));
  await click(host.querySelector('[aria-label="选择段落 1"]'));
  assert.equal(button("确认加入研究房间"), undefined);
  await click(button("预览选段与研究包范围"));
  await render({ roomId: "room_two" });
  assert.equal(button("确认加入研究房间"), undefined);
});

test("late preview from an old room is ignored and aborted", async () => {
  let resolve, signal;
  globalThis.fetch = (_path, options) => { signal = options.signal; return new Promise((done) => { resolve = done; }); };
  const render = await mount();
  await click(host.querySelector('[aria-label="选择段落 2"]'));
  await click(button("预览选段与研究包范围"));
  await render({ roomId: "room_two" });
  assert.equal(signal.aborted, true);
  await act(async () => resolve({ ok: true, json: async () => ({ selection: preview({ room_id: "room_one" }) }) }));
  assert.equal(button("确认加入研究房间"), undefined);
});

test("document selection pins the chosen version while background updates add a newer version", async () => {
  let versions = [version];
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ document: { format: "official_document_evidence_v1", eligible: true, versions, status: "partial" } }) });
  const render = await mount({}, DocumentEvidence);
  await click(button("选择段落加入研究房间"));
  versions = [version, { ...version, id: "document_new", paragraphs: [{ id: "document_new:p0001", text: "NEW REVISION ONLY" }] }];
  await render({ refreshToken: 1 });
  assert.equal(host.querySelector('[aria-label="正文证据版本"]').value, "document_old");
  assert.equal(host.querySelectorAll(".document-selection-options input").length, 2);
  assert.doesNotMatch(host.querySelector(".document-selection").textContent, /NEW REVISION ONLY/);
  versions = versions.slice(1);
  await render({ refreshToken: 2 });
  assert.match(host.textContent, /未自动替换为新版/);
  assert.equal(button("确认加入研究房间"), undefined);
});
