import { useEffect, useRef, useState } from "react";

const retryDelays = [2_000, 4_000, 8_000, 16_000, 30_000];
const pollInterval = 10_000;
const initialReadWindow = 90_000;

export class StatusProtocolError extends Error {}

function transient(error) {
  if (error instanceof StatusProtocolError || error instanceof SyntaxError
    || error?.name === "AbortError" || error?.code) return false;
  return !error?.status || error.status === 408 || error.status === 429 || error.status >= 500;
}

// Only the supplied read-only GET is retried. Actions and authorization are outside this hook.
export function useNewsReviewPolling(identity, refresh, readStatus, validate, windowUntil) {
  const [snapshot, setSnapshot] = useState({ identity, data: null, error: "", lastSuccessAt: null, phase: "reading" });
  const confirmedWindow = useRef(null);
  useEffect(() => {
    const abort = new AbortController();
    let timer, deadlineTimer;
    let deadline = Date.now() + initialReadWindow;
    const remembered = confirmedWindow.current?.identity === identity ? confirmedWindow.current : null;
    if (remembered?.deadline > Date.now()) deadline = Math.min(deadline, remembered.deadline);
    let validated = false;
    let previous = null;
    let failures = 0;
    const update = (value) => setSnapshot((old) => ({
      ...(old.identity === identity ? old : { data: null, lastSuccessAt: null }), identity, ...value,
    }));
    const expire = () => {
      abort.abort();
      clearTimeout(timer);
      update({ phase: validated ? "expired" : "exhausted" });
    };
    const armDeadline = () => {
      clearTimeout(deadlineTimer);
      deadlineTimer = setTimeout(expire, Math.max(0, deadline - Date.now()));
    };
    const schedule = (delay) => {
      // The independent deadline timer also cancels an in-flight read.
      if (Date.now() + delay < deadline) timer = setTimeout(read, delay);
    };
    const read = async () => {
      if (abort.signal.aborted || Date.now() >= deadline) return;
      try {
        const response = await readStatus(identity, abort.signal);
        if (abort.signal.aborted) return;
        const next = validate(response, identity, previous);
        const until = windowUntil(next);
        previous = next;
        failures = 0;
        // Bind polling to the first confirmed window; a GET cannot extend it.
        deadline = validated ? Math.min(deadline, until) : until;
        validated = true;
        confirmedWindow.current = { identity, deadline };
        const now = Date.now();
        update({ data: next, error: "", lastSuccessAt: now,
          phase: deadline > now ? "observing" : deadline > 0 ? "expired" : "idle" });
        clearTimeout(deadlineTimer);
        if (deadline > now) { armDeadline(); schedule(pollInterval); }
      } catch (error) {
        if (abort.signal.aborted) return;
        if (!validated && remembered && remembered.deadline <= Date.now()) {
          clearTimeout(deadlineTimer);
          update({ error: error.message || "状态读取失败。", phase: remembered.deadline > 0 ? "expired" : "idle" });
          return;
        }
        if (error?.name === "AbortError") {
          clearTimeout(deadlineTimer);
          update({ error: "状态读取已取消。", phase: "cancelled" });
          return;
        }
        const retry = transient(error) && failures < retryDelays.length;
        update({ error: error.message || "状态读取失败。", phase: retry ? "retrying" : transient(error) ? "exhausted" : "error" });
        if (retry) schedule(retryDelays[failures++]);
        else clearTimeout(deadlineTimer);
      }
    };
    update({ error: "", phase: "reading" });
    armDeadline();
    void read();
    return () => { abort.abort(); clearTimeout(timer); clearTimeout(deadlineTimer); };
  }, [identity, refresh, readStatus, validate, windowUntil]);
  return snapshot.identity === identity ? snapshot : { data: null, error: "", lastSuccessAt: null, phase: "reading" };
}
