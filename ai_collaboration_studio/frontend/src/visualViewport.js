const CSS_VARIABLES = [
  "--visual-viewport-width",
  "--visual-viewport-height",
  "--visual-viewport-offset-left",
  "--visual-viewport-offset-top",
  "--visual-viewport-scale",
  "--keyboard-inset-bottom",
];

function nonNegativeNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, number) : fallback;
}

function pixelValue(value) {
  return `${Math.round(value)}px`;
}

export function bindVisualViewportCssVars({
  root = globalThis.document?.documentElement,
  windowObject = globalThis.window,
} = {}) {
  if (!root || !windowObject) return () => {};

  const viewport = windowObject.visualViewport;
  let animationFrame = 0;
  const commit = () => {
    animationFrame = 0;
    const layoutWidth = nonNegativeNumber(windowObject.innerWidth);
    const layoutHeight = nonNegativeNumber(windowObject.innerHeight);
    const viewportWidth = nonNegativeNumber(viewport?.width, layoutWidth);
    const viewportHeight = nonNegativeNumber(viewport?.height, layoutHeight);
    const offsetLeft = nonNegativeNumber(viewport?.offsetLeft);
    const offsetTop = nonNegativeNumber(viewport?.offsetTop);
    const scale = Math.max(0.01, nonNegativeNumber(viewport?.scale, 1));
    // Pinch zoom also shrinks visualViewport.height. Treating that delta as a
    // keyboard would double-shift fixed controls while the user is zoomed in.
    const isUnscaledViewport = Math.abs(scale - 1) <= 0.01;
    const keyboardInset = isUnscaledViewport
      ? Math.max(0, layoutHeight - viewportHeight - offsetTop)
      : 0;

    root.style.setProperty("--visual-viewport-width", pixelValue(viewportWidth));
    root.style.setProperty("--visual-viewport-height", pixelValue(viewportHeight));
    root.style.setProperty("--visual-viewport-offset-left", pixelValue(offsetLeft));
    root.style.setProperty("--visual-viewport-offset-top", pixelValue(offsetTop));
    root.style.setProperty("--visual-viewport-scale", String(Math.round(scale * 1000) / 1000));
    root.style.setProperty("--keyboard-inset-bottom", pixelValue(keyboardInset));
  };
  const schedule = () => {
    if (animationFrame) return;
    animationFrame = windowObject.requestAnimationFrame?.(commit) || 0;
    if (!animationFrame) commit();
  };

  commit();
  viewport?.addEventListener("resize", schedule, { passive: true });
  viewport?.addEventListener("scroll", schedule, { passive: true });
  windowObject.addEventListener?.("resize", schedule, { passive: true });

  return () => {
    if (animationFrame) windowObject.cancelAnimationFrame?.(animationFrame);
    viewport?.removeEventListener("resize", schedule);
    viewport?.removeEventListener("scroll", schedule);
    windowObject.removeEventListener?.("resize", schedule);
    CSS_VARIABLES.forEach((name) => root.style.removeProperty(name));
  };
}
