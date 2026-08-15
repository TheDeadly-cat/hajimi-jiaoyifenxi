import assert from "node:assert/strict";
import test from "node:test";

import { createLatestRequestCoordinator } from "../src/latestRequest.js";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

test("a forced readiness refresh wins even when the older request returns last", async () => {
  const coordinator = createLatestRequestCoordinator();
  const first = deferred();
  const second = deferred();
  const applied = [];
  const loading = [];
  let firstSignal;

  const firstRun = coordinator.run({
    request: (signal) => {
      firstSignal = signal;
      return first.promise;
    },
    onSuccess: (value) => applied.push(value),
    onLoadingChange: (value) => loading.push(value),
  });
  const secondRun = coordinator.run({
    forceRequest: true,
    request: () => second.promise,
    onSuccess: (value) => applied.push(value),
    onLoadingChange: (value) => loading.push(value),
  });

  assert.equal(firstSignal.aborted, true);
  second.resolve("new readiness");
  assert.equal((await secondRun).status, "applied");
  first.resolve("stale readiness");
  assert.equal((await firstRun).status, "stale");
  assert.deepEqual(applied, ["new readiness"]);
  assert.equal(loading.at(-1), false);
  assert.equal(coordinator.inFlight, false);
});

test("a confirmation-style forced refresh starts while a normal duplicate is skipped", async () => {
  const coordinator = createLatestRequestCoordinator();
  const initial = deferred();
  const confirmed = deferred();
  let requestCount = 0;

  const initialRun = coordinator.run({
    request: () => {
      requestCount += 1;
      return initial.promise;
    },
  });
  const duplicate = await coordinator.run({
    request: () => {
      requestCount += 1;
      return Promise.resolve("duplicate");
    },
  });
  const confirmationRun = coordinator.run({
    forceRequest: true,
    request: () => {
      requestCount += 1;
      return confirmed.promise;
    },
  });

  assert.equal(duplicate.status, "skipped");
  assert.equal(requestCount, 2);
  confirmed.resolve("confirmed readiness");
  assert.equal((await confirmationRun).status, "applied");
  initial.resolve("old readiness");
  assert.equal((await initialRun).status, "stale");
});
