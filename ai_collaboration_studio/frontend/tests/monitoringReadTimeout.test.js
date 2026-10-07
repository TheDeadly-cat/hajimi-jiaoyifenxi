import assert from "node:assert/strict";
import test from "node:test";
import { api } from "../src/api.js";

function fakeTimers(t) {
  const pending = new Map();
  let sequence = 0;
  t.mock.method(globalThis, "setTimeout", (callback, delay) => {
    const token = ++sequence;
    pending.set(token, { callback, delay });
    return token;
  });
  t.mock.method(globalThis, "clearTimeout", (token) => pending.delete(token));
  return {
    pending,
    expire() {
      assert.equal(pending.size, 1);
      const timer = [...pending.values()][0];
      assert.equal(timer.delay, 15_000);
      timer.callback();
    },
  };
}

function response(payload = { ok: true }) {
  return { ok: true, status: 200, json: async () => payload };
}

const reads = [
  ["inbox list", (signal) => api.listSourceInbox({ signal })],
  ["monitoring health", (signal) => api.sourceMonitoringHealth(signal)],
  ["adapter control", (signal) => api.sourceMonitoringOperatorControl(signal)],
  ["notification feed", (signal) => api.sourceInboxNotifications({ signal })],
  ["event detail", (signal) => api.sourceInboxItem("event", signal)],
  ["document detail", (signal) => api.sourceDocument("event", signal)],
  ["document control", (signal) => api.sourceDocumentControl(signal)],
  ["import prompt", (signal) => api.sourceMonitoringPromptTemplate(signal)],
];

for (const [name, read] of reads) {
  test(`${name} releases a hung request at the monitoring deadline`, async (t) => {
    const timers = fakeTimers(t);
    let wireSignal;
    t.mock.method(globalThis, "fetch", (_path, options) => {
      wireSignal = options.signal;
      return new Promise(() => {});
    });
    const caller = new AbortController();
    const result = read(caller.signal);
    const rejected = assert.rejects(result, { name: "StatusRequestTimeout", status: 408 });
    timers.expire();
    await rejected;
    assert.equal(wireSignal.aborted, true);
    assert.equal(caller.signal.aborted, false);
    assert.equal(timers.pending.size, 0);
  });
}

test("a response whose JSON body never completes is also bounded", async (t) => {
  const timers = fakeTimers(t);
  t.mock.method(globalThis, "fetch", async () => ({
    ok: true, status: 200, json: () => new Promise(() => {}),
  }));
  const result = api.sourceMonitoringHealth();
  const rejected = assert.rejects(result, { name: "StatusRequestTimeout" });
  await Promise.resolve();
  timers.expire();
  await rejected;
  assert.equal(timers.pending.size, 0);
});

test("caller cancellation is forwarded and does not wait for the deadline", async (t) => {
  const timers = fakeTimers(t);
  let wireSignal;
  t.mock.method(globalThis, "fetch", (_path, options) => {
    wireSignal = options.signal;
    return new Promise(() => {});
  });
  const caller = new AbortController();
  const result = api.sourceInboxNotifications({ signal: caller.signal });
  const rejected = assert.rejects(result, { name: "AbortError" });
  caller.abort();
  await rejected;
  assert.equal(wireSignal.aborted, true);
  assert.equal(timers.pending.size, 0);
});

test("an already cancelled caller starts neither a request nor a timer", async (t) => {
  const timers = fakeTimers(t);
  let calls = 0;
  t.mock.method(globalThis, "fetch", () => { calls += 1; return response(); });
  const caller = new AbortController();
  caller.abort();
  await assert.rejects(api.sourceInboxNotifications({ signal: caller.signal }), { name: "AbortError" });
  assert.equal(calls, 0);
  assert.equal(timers.pending.size, 0);
});

test("a timed-out response cannot replace the active session token", async (t) => {
  const timers = fakeTimers(t);
  const headers = [];
  let finishLate;
  let phase = "bootstrap";
  t.mock.method(globalThis, "fetch", (_path, options) => {
    headers.push(options.headers);
    if (phase === "hung") return new Promise((resolve) => { finishLate = resolve; });
    return Promise.resolve(response(phase === "bootstrap"
      ? { ok: true, session_token: "synthetic-current-token" } : { ok: true }));
  });
  await api.bootstrap();
  phase = "hung";
  const result = api.sourceInboxNotifications();
  const rejected = assert.rejects(result, { name: "StatusRequestTimeout" });
  timers.expire();
  await rejected;
  finishLate(response({ ok: true, session_token: "synthetic-stale-token" }));
  await Promise.resolve();
  await Promise.resolve();
  phase = "next";
  await api.bootstrap();
  assert.equal(headers.at(-1)["X-AI-Studio-Token"], "synthetic-current-token");
  assert.equal(timers.pending.size, 0);
});

test("later polling recovers after a timeout without retaining old timers", async (t) => {
  const timers = fakeTimers(t);
  let hang = true;
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {
    calls += 1;
    return hang ? new Promise(() => {}) : response({ ok: true, cursor: "recovered" });
  });
  const first = api.sourceInboxNotifications({ after: "original" });
  const rejected = assert.rejects(first, { name: "StatusRequestTimeout" });
  timers.expire();
  await rejected;
  hang = false;
  const next = await api.sourceInboxNotifications({ after: "original" });
  assert.equal(next.cursor, "recovered");
  assert.equal(calls, 2);
  assert.equal(timers.pending.size, 0);
});

test("repeated monitoring reads keep request resources bounded through failures", async (t) => {
  const timers = fakeTimers(t);
  let hang = false;
  t.mock.method(globalThis, "fetch", async () => hang ? new Promise(() => {}) : response());
  for (let index = 0; index < 1_000; index += 1) {
    hang = index % 20 === 0;
    const result = api.sourceInboxNotifications({ after: String(index) });
    if (hang) {
      const rejected = assert.rejects(result, { name: "StatusRequestTimeout" });
      timers.expire();
      await rejected;
    } else {
      await result;
    }
    assert.equal(timers.pending.size, 0);
  }
});

test("a mutation retains its caller signal and is never timed out or retried", async (t) => {
  const timers = fakeTimers(t);
  const calls = [];
  t.mock.method(globalThis, "fetch", async (path, options) => {
    calls.push({ path, options });
    return response();
  });
  const caller = new AbortController();
  await api.acknowledgeSourceInboxItem("event", 3, caller.signal);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.signal, caller.signal);
  assert.equal(timers.pending.size, 0);
});
