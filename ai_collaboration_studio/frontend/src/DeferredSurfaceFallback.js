import { createElement, useRef } from "react";
import { useModalFocus } from "./useModalFocus.js";
import "./styles/deferred-surface-polish.css";

function loadingStatus(label) {
  return createElement(
    "div",
    { className: "deferred-surface-status", role: "status", "aria-live": "polite" },
    createElement(
      "span",
      { className: "deferred-surface-signal", "aria-hidden": "true" },
      createElement("i"),
      createElement("i"),
      createElement("i"),
    ),
    createElement(
      "span",
      { className: "deferred-surface-copy" },
      createElement("small", null, "界面准备中"),
      createElement("strong", null, `正在加载${label}…`),
    ),
    createElement(
      "span",
      { className: "deferred-surface-bars", "aria-hidden": "true" },
      createElement("i"),
      createElement("i"),
      createElement("i"),
    ),
  );
}

export function DeferredSurfaceFallback({
  label,
  dialog = false,
  open = true,
  onClose,
  restoreFocusRef,
}) {
  const fallbackRef = useRef(null);
  const closeButtonRef = useRef(null);
  const ownsModalFocus = dialog && typeof onClose === "function";
  useModalFocus({
    open: ownsModalFocus && open,
    containerRef: fallbackRef,
    initialFocusRef: onClose ? closeButtonRef : fallbackRef,
    restoreFallbackRef: restoreFocusRef,
    onClose: dialog ? onClose : null,
  });
  if (ownsModalFocus && !open) return null;
  const content = createElement(
    "div",
    { className: "deferred-surface-fallback", "aria-busy": "true" },
    loadingStatus(label),
  );
  if (!dialog) return content;
  if (!ownsModalFocus) {
    return createElement(
      "div",
      { className: "dialog-backdrop deferred-dialog-backdrop" },
      content,
    );
  }
  return createElement(
    "div",
    { className: "dialog-backdrop deferred-dialog-backdrop", role: "presentation" },
    createElement(
      "div",
      {
        ref: fallbackRef,
        className: "deferred-surface-fallback",
        role: "dialog",
        "aria-modal": "true",
        "aria-label": `${label}加载中`,
        "aria-busy": "true",
        tabIndex: -1,
      },
      loadingStatus(label),
      createElement(
        "button",
        {
          ref: closeButtonRef,
          type: "button",
          className: "secondary compact",
          onClick: onClose,
        },
        "取消加载",
      ),
    ),
  );
}
