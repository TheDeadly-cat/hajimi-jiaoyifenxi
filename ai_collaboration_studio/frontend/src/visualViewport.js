const CSS_VARIABLES = [
  "--visual-viewport-width",
  "--visual-viewport-height",
  "--visual-viewport-offset-left",
  "--visual-viewport-offset-top",
  "--visual-viewport-scale",
  "--keyboard-inset-bottom",
];

function nonNegativeNumber(value, fallback = 0) {
  const number = typeof value === "number" ? value : Number.NaN;
  return Number.isFinite(number) ? Math.max(0, number) : fallback;
}

function positiveNumber(value, fallback = 0) {
  const number = nonNegativeNumber(value, fallback);
  return number > 0 ? number : fallback;
}

function roundedNumber(value) {
  const rounded = Math.round(value * 1000) / 1000;
  return Object.is(rounded, -0) ? 0 : rounded;
}

function pixelValue(value) {
  return `${roundedNumber(value)}px`;
}

export function bindVisualViewportCssVars({
  root = globalThis.document?.documentElement,
  windowObject = globalThis.window,
} = {}) {
  if (!root || !windowObject) return () => {};

  const viewport = windowObject.visualViewport;
  const pendingFrame = Symbol("visual-viewport-frame-pending");
  const appliedValues = new Map();
  let animationFrame = null;
  let disposed = false;
  const publish = (name, value) => {
    if (appliedValues.get(name) === value) return;
    root.style.setProperty(name, value);
    appliedValues.set(name, value);
  };
  const commit = () => {
    if (disposed) return;
    const layoutWidth = positiveNumber(windowObject.innerWidth, positiveNumber(root.clientWidth));
    const layoutHeight = positiveNumber(windowObject.innerHeight, positiveNumber(root.clientHeight));
    const viewportWidth = positiveNumber(viewport?.width, layoutWidth);
    const viewportHeight = positiveNumber(viewport?.height, layoutHeight);
    const offsetLeft = nonNegativeNumber(viewport?.offsetLeft);
    const offsetTop = nonNegativeNumber(viewport?.offsetTop);
    const scale = Math.min(100, Math.max(0.01, positiveNumber(viewport?.scale, 1)));
    // Pinch zoom also shrinks visualViewport.height. Treating that delta as a
    // keyboard would double-shift fixed controls while the user is zoomed in.
    const isUnscaledViewport = Math.abs(scale - 1) <= 0.01;
    const keyboardInset = isUnscaledViewport
      ? Math.min(layoutHeight, Math.max(0, layoutHeight - viewportHeight - offsetTop))
      : 0;

    publish("--visual-viewport-width", pixelValue(viewportWidth));
    publish("--visual-viewport-height", pixelValue(viewportHeight));
    publish("--visual-viewport-offset-left", pixelValue(offsetLeft));
    publish("--visual-viewport-offset-top", pixelValue(offsetTop));
    publish("--visual-viewport-scale", String(roundedNumber(scale)));
    publish("--keyboard-inset-bottom", pixelValue(keyboardInset));
  };
  const schedule = () => {
    if (disposed || animationFrame !== null) return;
    if (typeof windowObject.requestAnimationFrame !== "function") {
      commit();
      return;
    }
    animationFrame = pendingFrame;
    const frameId = windowObject.requestAnimationFrame(() => {
      animationFrame = null;
      commit();
    });
    // Some test or compatibility shims invoke callbacks synchronously. Do not
    // overwrite the callback's cleared state in that case; zero is a valid ID.
    if (animationFrame === pendingFrame) animationFrame = frameId;
  };

  commit();
  viewport?.addEventListener("resize", schedule, { passive: true });
  viewport?.addEventListener("scroll", schedule, { passive: true });
  windowObject.addEventListener?.("resize", schedule, { passive: true });

  return () => {
    disposed = true;
    if (animationFrame !== null && animationFrame !== pendingFrame) {
      windowObject.cancelAnimationFrame?.(animationFrame);
    }
    animationFrame = null;
    viewport?.removeEventListener("resize", schedule);
    viewport?.removeEventListener("scroll", schedule);
    windowObject.removeEventListener?.("resize", schedule);
    CSS_VARIABLES.forEach((name) => root.style.removeProperty(name));
    appliedValues.clear();
  };
}
