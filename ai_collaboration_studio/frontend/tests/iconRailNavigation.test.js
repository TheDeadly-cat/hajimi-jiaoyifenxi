import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../src/components/IconRail.jsx", import.meta.url),
  "utf8",
);
const styles = readFileSync(
  new URL("../src/styles/icon-rail-polish.css", import.meta.url),
  "utf8",
);

test("icon rail binds list identity to sections and keeps activation explicit", () => {
  assert.match(source, /key=\{section\}/);
  assert.doesNotMatch(source, /key=\{label\}/);
  assert.match(source, /aria-current=\{activeSection === section \? "page" : undefined\}/);
  assert.match(source, /data-section=\{section\}/);
  assert.match(source, /onClick=\{\(event\) => onNavigate\(section, event\.currentTarget\)\}/);
  assert.match(source, /onKeyDown=\{moveRailFocus\}/);
  assert.match(source, /tabIndex=\{activeSection === section \? 0 : -1\}/);
});

test("rail focus wraps across vertical and compact horizontal orientations", () => {
  for (const key of ["ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Home", "End"]) {
    assert.match(source, new RegExp(`"${key}"`));
  }
  assert.match(source, /querySelectorAll\("\.rail-button:not\(:disabled\)"\)/);
  assert.match(source, /event\.key === "ArrowDown" \|\| event\.key === "ArrowRight"/);
  assert.match(source, /event\.key === "ArrowUp" \|\| event\.key === "ArrowLeft"/);
  assert.match(source, /\(currentIndex \+ 1\) % buttons\.length/);
  assert.match(source, /\(currentIndex - 1 \+ buttons\.length\) % buttons\.length/);
  assert.match(source, /event\.preventDefault\(\);\s*buttons\[nextIndex\]\?\.focus\(\);/);
  const helper = source.slice(source.indexOf("function moveRailFocus"), source.indexOf("export function IconRail"));
  assert.doesNotMatch(helper, /onNavigate/);
});

test("rail visual focus feedback remains restrained under reduced motion", () => {
  assert.match(styles, /\.icon-rail\s*\{[\s\S]*z-index:\s*40;[\s\S]*overflow:\s*visible;/);
  assert.match(styles, /\.icon-rail \.rail-actions::before\s*\{[\s\S]*linear-gradient/);
  assert.match(
    styles,
    /\.icon-rail \.rail-button::after\s*\{[\s\S]*z-index:\s*90;[\s\S]*pointer-events:\s*none;/,
  );
  assert.match(
    styles,
    /\.rail-actions:has\(\.rail-button:focus-visible\)[\s\S]*\.rail-button:hover:not\(:focus-visible\)::after\s*\{[\s\S]*opacity:\s*0;/,
  );
  assert.match(styles, /\.rail-button:focus-visible svg\s*\{\s*transform:\s*scale\(1\.06\);/);
  assert.match(styles, /\.rail-button\.active svg\s*\{\s*filter:\s*drop-shadow/);
  assert.match(
    styles,
    /@media \(max-width: 760px\)[\s\S]*\.rail-button\.active::before\s*\{[\s\S]*top:\s*-1px;[\s\S]*height:\s*3px;[\s\S]*linear-gradient\(90deg/,
  );
  assert.match(
    styles,
    /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.rail-button svg\s*\{\s*transition:\s*none;/,
  );
  assert.match(
    styles,
    /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.rail-button:focus-visible svg\s*\{\s*transform:\s*none;/,
  );
});
