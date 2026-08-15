import assert from "node:assert/strict";
import test from "node:test";
import { bindVisualViewportCssVars } from "../src/visualViewport.js";

function eventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type, listener) {
      if (listeners.get(type) === listener) listeners.delete(type);
    },
    emit(type) { listeners.get(type)?.(); },
    listenerCount() { return listeners.size; },
  };
}

test("visual viewport binding publishes keyboard-safe geometry and cleans up", () => {
  const viewport = Object.assign(eventTarget(), {
    width: 390,
    height: 520,
    offsetLeft: 4,
    offsetTop: 12,
    scale: 1,
  });
  const windowEvents = eventTarget();
  const properties = new Map();
  const root = {
    style: {
      setProperty(name, value) { properties.set(name, value); },
      removeProperty(name) { properties.delete(name); },
    },
  };
  const frames = [];
  const windowObject = Object.assign(windowEvents, {
    innerWidth: 400,
    innerHeight: 800,
    visualViewport: viewport,
    requestAnimationFrame(callback) { frames.push(callback); return frames.length; },
    cancelAnimationFrame() {},
  });

  const cleanup = bindVisualViewportCssVars({ root, windowObject });
  assert.equal(properties.get("--visual-viewport-width"), "390px");
  assert.equal(properties.get("--visual-viewport-height"), "520px");
  assert.equal(properties.get("--visual-viewport-offset-left"), "4px");
  assert.equal(properties.get("--visual-viewport-offset-top"), "12px");
  assert.equal(properties.get("--visual-viewport-scale"), "1");
  assert.equal(properties.get("--keyboard-inset-bottom"), "268px");

  viewport.height = 430;
  viewport.offsetTop = 20;
  viewport.emit("resize");
  frames.shift()?.();
  assert.equal(properties.get("--visual-viewport-height"), "430px");
  assert.equal(properties.get("--visual-viewport-offset-top"), "20px");
  assert.equal(properties.get("--keyboard-inset-bottom"), "350px");

  viewport.width = 200;
  viewport.height = 300;
  viewport.offsetLeft = 64;
  viewport.offsetTop = 80;
  viewport.scale = 2;
  viewport.emit("scroll");
  frames.shift()?.();
  assert.equal(properties.get("--visual-viewport-width"), "200px");
  assert.equal(properties.get("--visual-viewport-height"), "300px");
  assert.equal(properties.get("--visual-viewport-offset-left"), "64px");
  assert.equal(properties.get("--visual-viewport-offset-top"), "80px");
  assert.equal(properties.get("--visual-viewport-scale"), "2");
  assert.equal(properties.get("--keyboard-inset-bottom"), "0px");

  cleanup();
  assert.equal(properties.size, 0);
  assert.equal(viewport.listenerCount(), 0);
  assert.equal(windowEvents.listenerCount(), 0);
});

test("visual viewport binding falls back to the layout viewport", () => {
  const windowEvents = eventTarget();
  const properties = new Map();
  const root = {
    style: {
      setProperty(name, value) { properties.set(name, value); },
      removeProperty(name) { properties.delete(name); },
    },
  };
  const windowObject = Object.assign(windowEvents, {
    innerWidth: 1024,
    innerHeight: 768,
    requestAnimationFrame(callback) { callback(); return 0; },
    cancelAnimationFrame() {},
  });

  const cleanup = bindVisualViewportCssVars({ root, windowObject });
  assert.deepEqual(Object.fromEntries(properties), {
    "--visual-viewport-width": "1024px",
    "--visual-viewport-height": "768px",
    "--visual-viewport-offset-left": "0px",
    "--visual-viewport-offset-top": "0px",
    "--visual-viewport-scale": "1",
    "--keyboard-inset-bottom": "0px",
  });

  cleanup();
  assert.equal(properties.size, 0);
  assert.equal(windowEvents.listenerCount(), 0);
});
