import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const source = await readFile(new URL("../src/main.tsx", import.meta.url), "utf8");

test("range controls expose accessible 24h, 7d, and 30d presets", () => {
  assert.match(source, /history-range\.mjs/);
  assert.match(source, /24h/);
  assert.match(source, /7d/);
  assert.match(source, /30d/);
  assert.match(source, /Previous/);
  assert.match(source, /Next/);
  assert.match(source, /Latest/);
  assert.match(source, /aria-label/);
});

test("range requests use explicit bounds and cancel obsolete fetches", () => {
  assert.match(source, /new AbortController\(\)/);
  assert.match(source, /signal/);
  assert.match(source, /start: range\.start/);
  assert.match(source, /end: range\.end/);
  assert.match(source, /AbortError/);
});

test("server range changes reset the chart zoom and retain five-minute power gaps", () => {
  assert.match(source, /dispatchAction\([\s\S]*type:\s*["']dataZoom["']/);
  assert.match(source, /start:\s*0/);
  assert.match(source, /end:\s*100/);
  assert.match(source, /name: "Battery Power \(MW\)"[\s\S]*?connectNulls: false/);
  assert.match(source, /showSymbol:\s*range\.preset === "24h"/);
});

test("range-specific labels and local window display replace a fixed preceding day", () => {
  assert.doesNotMatch(source, /preceding 24 hours/);
  assert.match(source, /formatTimestamp/);
  assert.match(source, /range\.start/);
  assert.match(source, /range\.end/);
  assert.match(source, /selected range/i);
});

test("entering custom mode preserves valid bounds until explicit dates are applied", () => {
  assert.match(source, /customRange\(range\.start, range\.end\)/);
  assert.match(source, /customRange\(customStart, customEnd\)/);
  assert.doesNotMatch(source, /selectPreset\(option\.value\)/);
});

test("fixed presets load an older gutter and plan server-backed adjacent pages", () => {
  assert.match(source, /history-buffer\.mjs/);
  assert.match(source, /initialGutterChunk/);
  assert.match(source, /planAdjacentChunk/);
  assert.match(source, /chunk\.start/);
  assert.match(source, /chunk\.end/);
});

test("adjacent pages are cancellable, bounded, merged, and retryable", () => {
  assert.match(source, /adjacentRequests/);
  assert.match(source, /generation/);
  assert.match(source, /mergePoints/);
  assert.match(source, /retainBuffer/);
  assert.match(source, /fetchSeries\([\s\S]*signal/);
  assert.match(source, /Unable to load adjacent history/);
});

test("paging updates data without clearing the current chart and reapplies absolute bounds", () => {
  assert.match(source, /chart\.setOption/);
  assert.match(source, /startValue/);
  assert.match(source, /endValue/);
  assert.match(source, /visibleBounds/);
  assert.match(source, /setSeries\(\(current\) => current \? \{\.\.\.current, points:/);
});

test("custom ranges stay bounded while fixed paging is capped at request-time now", () => {
  assert.match(source, /range\.preset !== "custom"/);
  assert.match(source, /Math\.min\([\s\S]*Date\.now/);
});

test("adjacent-load errors expose an accessible retry action through the current visible bounds", () => {
  assert.match(source, /error === ADJACENT_LOAD_ERROR/);
  assert.match(source, /aria-label=["']Retry adjacent history["']/);
  assert.match(source, /requestAdjacentRef\.current\(visibleBoundsRef\.current/);
});
