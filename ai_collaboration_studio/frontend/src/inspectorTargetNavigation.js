export const INSPECTOR_TARGET_NAVIGATION_LIFETIME_MS = 4000;

function runtimeMethod(runtime, name, fallback) {
  return typeof runtime?.[name] === "function"
    ? runtime[name].bind(runtime)
    : fallback;
}

export function bindInspectorTargetNavigation(
  inspector,
  targetId,
  runtime = globalThis,
) {
  const normalizedTargetId = String(targetId || "").trim();
  if (!inspector || !normalizedTargetId) return undefined;

  const requestFrame = runtimeMethod(runtime, "requestAnimationFrame", (callback) => {
    callback();
    return 0;
  });
  const cancelFrame = runtimeMethod(runtime, "cancelAnimationFrame", () => {});
  const setTimer = runtimeMethod(runtime, "setTimeout", globalThis.setTimeout.bind(globalThis));
  const clearTimer = runtimeMethod(runtime, "clearTimeout", globalThis.clearTimeout.bind(globalThis));
  const ResizeObserverClass = runtime?.ResizeObserver;
  const MutationObserverClass = runtime?.MutationObserver;

  let animationFrame = 0;
  let lifetimeTimer = 0;
  let stopped = false;
  let target = null;
  let focusedTarget = null;
  let resizeObserver = null;
  let mutationObserver = null;

  const stop = () => {
    if (stopped) return;
    stopped = true;
    cancelFrame(animationFrame);
    clearTimer(lifetimeTimer);
    resizeObserver?.disconnect();
    mutationObserver?.disconnect();
  };
  const resolveTarget = () => {
    if (target?.isConnected && inspector.contains(target)) return target;
    const candidate = inspector.ownerDocument?.getElementById(normalizedTargetId) || null;
    target = candidate && inspector.contains(candidate) ? candidate : null;
    return target;
  };
  const align = () => {
    if (stopped) return;
    const resolvedTarget = resolveTarget();
    if (!resolvedTarget) return;
    cancelFrame(animationFrame);
    animationFrame = requestFrame(() => {
      if (
        stopped
        || !inspector.isConnected
        || !resolvedTarget.isConnected
        || !inspector.contains(resolvedTarget)
      ) return;
      const inspectorTop = inspector.getBoundingClientRect().top;
      const targetTop = resolvedTarget.getBoundingClientRect().top;
      const offset = targetTop - inspectorTop;
      if (Number.isFinite(offset) && Math.abs(offset) > 0.5) {
        inspector.scrollTop += offset;
      }
      if (focusedTarget !== resolvedTarget) {
        resolvedTarget.focus({ preventScroll: true });
        focusedTarget = resolvedTarget;
      }
    });
  };
  const observeChildren = () => {
    if (!resizeObserver) return;
    Array.from(inspector.children).forEach((child) => resizeObserver.observe(child));
  };

  if (typeof ResizeObserverClass === "function") {
    resizeObserver = new ResizeObserverClass(align);
    observeChildren();
  }
  if (typeof MutationObserverClass === "function") {
    mutationObserver = new MutationObserverClass(() => {
      observeChildren();
      align();
    });
    mutationObserver.observe(inspector, { childList: true, subtree: true });
  }
  align();
  lifetimeTimer = setTimer(stop, INSPECTOR_TARGET_NAVIGATION_LIFETIME_MS);
  return stop;
}
