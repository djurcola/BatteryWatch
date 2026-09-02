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
