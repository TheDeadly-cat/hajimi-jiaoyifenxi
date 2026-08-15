import { useEffect, useRef } from "react";

const activeModalSurfaces = [];
let modalSurfaceSequence = 0;

export const MODAL_FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

export function modalFocusIndex(controlCount, activeIndex, shiftKey) {
  if (!Number.isInteger(controlCount) || controlCount <= 0) return -1;
  if (!Number.isInteger(activeIndex) || activeIndex < 0 || activeIndex >= controlCount) {
    return shiftKey ? controlCount - 1 : 0;
  }
  if (shiftKey && activeIndex === 0) return controlCount - 1;
  if (!shiftKey && activeIndex === controlCount - 1) return 0;
  return -1;
}

function visibleControls(container) {
  return Array.from(container.querySelectorAll(MODAL_FOCUSABLE_SELECTOR))
    .filter(visibleFocusTarget);
}

function visibleFocusTarget(target) {
  if (
    !target?.isConnected
    || target.disabled === true
    || typeof target.focus !== "function"
    || target.getClientRects().length === 0
  ) return false;
  const style = globalThis.getComputedStyle?.(target);
  return !style || (
    style.display !== "none"
    && style.visibility !== "hidden"
    && style.visibility !== "collapse"
  );
}

function registerModalSurface(token, container) {
  const existingIndex = activeModalSurfaces.findIndex((entry) => entry.token === token);
  if (existingIndex >= 0) activeModalSurfaces.splice(existingIndex, 1);
  activeModalSurfaces.push({
    container,
    sequence: ++modalSurfaceSequence,
    token,
  });
}

function unregisterModalSurface(token) {
  const index = activeModalSurfaces.findIndex((entry) => entry.token === token);
  if (index >= 0) activeModalSurfaces.splice(index, 1);
}

function topModalSurfaceIs(token) {
  const connected = activeModalSurfaces.filter((entry) => entry.container?.isConnected);
  const current = connected.find((entry) => entry.token === token);
  if (!current) return false;
  return !connected.some((entry) => {
    if (entry === current) return false;
    if (current.container.contains(entry.container)) return true;
    if (entry.container.contains(current.container)) return false;
    const currentZIndex = Number.parseFloat(globalThis.getComputedStyle?.(current.container)?.zIndex);
    const entryZIndex = Number.parseFloat(globalThis.getComputedStyle?.(entry.container)?.zIndex);
    if (Number.isFinite(currentZIndex) && Number.isFinite(entryZIndex) && currentZIndex !== entryZIndex) {
      return entryZIndex > currentZIndex;
    }
    const position = current.container.compareDocumentPosition(entry.container);
    if (position & globalThis.Node.DOCUMENT_POSITION_FOLLOWING) return true;
    if (position & globalThis.Node.DOCUMENT_POSITION_PRECEDING) return false;
    return entry.sequence > current.sequence;
  });
}

export function useModalFocus({
  open,
  containerRef,
  initialFocusRef,
  restoreFallbackRef,
  onClose,
}) {
  const closeRef = useRef(onClose);
  const restoreTargetRef = useRef(null);
  const restoreFrameRef = useRef(null);
  const surfaceTokenRef = useRef(Symbol("modal-focus-surface"));
  closeRef.current = onClose;

  useEffect(() => {
    if (!open) return undefined;

    const container = containerRef.current;
    if (!container) return undefined;
    if (restoreFrameRef.current !== null) {
      globalThis.cancelAnimationFrame(restoreFrameRef.current);
      restoreFrameRef.current = null;
    }
    const surfaceToken = surfaceTokenRef.current;
    registerModalSurface(surfaceToken, container);
    const activeElement = document.activeElement;
    if (activeElement && activeElement !== document.body && !container.contains(activeElement) && typeof activeElement.focus === "function") {
      restoreTargetRef.current = activeElement;
    }

    const initialFocusFrame = globalThis.requestAnimationFrame(() => {
      const controls = visibleControls(container);
      const preferredTarget = initialFocusRef?.current;
      const target = visibleFocusTarget(preferredTarget) ? preferredTarget : controls[0];
      target?.focus({ preventScroll: true });
    });
    const handleKeyDown = (event) => {
      if (!topModalSurfaceIs(surfaceToken)) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();
        if (typeof closeRef.current === "function") closeRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const controls = visibleControls(container);
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      if (!controls.length) {
        if (visibleFocusTarget(container)) container.focus({ preventScroll: true });
        return;
      }
      const activeIndex = controls.indexOf(document.activeElement);
      const wrappedIndex = modalFocusIndex(
        controls.length,
        activeIndex,
        event.shiftKey,
      );
      const nextIndex = wrappedIndex >= 0
        ? wrappedIndex
        : activeIndex + (event.shiftKey ? -1 : 1);
      controls[nextIndex].focus({ preventScroll: true });
    };

    document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      globalThis.cancelAnimationFrame(initialFocusFrame);
      document.removeEventListener("keydown", handleKeyDown, true);
      unregisterModalSurface(surfaceToken);
      const capturedTarget = restoreTargetRef.current;
      restoreTargetRef.current = null;
      restoreFrameRef.current = globalThis.requestAnimationFrame(() => {
        restoreFrameRef.current = null;
        const fallbackTarget = restoreFallbackRef?.current;
        const restoreTarget = visibleFocusTarget(capturedTarget)
          ? capturedTarget
          : fallbackTarget;
        if (visibleFocusTarget(restoreTarget)) {
          restoreTarget.focus({ preventScroll: true });
        }
      });
    };
  }, [open]);
}
